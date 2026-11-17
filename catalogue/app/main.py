"""Event Catalogue HTTP application."""

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import Settings
from .api import router
from .cache import DetailCache
from .metrics import Metrics

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    metrics = Metrics(settings.messaging_enabled)
    cache = DetailCache(settings.cache_ttl_seconds, settings.cache_max_entries)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start without opening a connection: liveness must work during a DB outage.
        pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=0,
            max_size=5,
            timeout=settings.database_timeout_seconds,
            command_timeout=settings.database_timeout_seconds,
        )
        app.state.database_pool = pool
        task = None
        if settings.messaging_enabled:
            from .worker import run_worker
            task = asyncio.create_task(run_worker(pool, settings, cache, metrics))
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await pool.close()

    app = FastAPI(title="Event Catalogue", lifespan=lifespan)
    app.state.settings = settings
    app.state.cache = cache
    app.state.metrics = metrics
    app.include_router(router)

    @app.middleware("http")
    async def http_metrics(request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            if route != "/metrics":
                metrics.requests.labels("catalogue", request.method, route, str(status)).inc()
                metrics.latency.labels("catalogue", request.method, route).observe(time.perf_counter() - start)

    @app.get("/metrics")
    async def prometheus_metrics():
        await metrics.refresh(app.state.database_pool, settings.database_timeout_seconds)
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    async def health() -> dict:
        return {"service": "catalogue", "status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> dict:
        try:
            # Bound acquisition plus query together, not just each step separately.
            async with asyncio.timeout(settings.database_timeout_seconds):
                async with request.app.state.database_pool.acquire() as connection:
                    await connection.fetchval("SELECT 1")
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            # Do not return connection details or credentials in the HTTP response.
            log.warning("Catalogue readiness failed (%s)", type(exc).__name__)
            raise HTTPException(503, "Catalogue database is unavailable") from exc
        return {"service": "catalogue", "status": "ready"}

    return app


app = create_app()
