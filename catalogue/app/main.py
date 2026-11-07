"""Event Catalogue startup shell; business endpoints follow separately."""

import asyncio
import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request

from .config import Settings

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

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
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title="Event Catalogue", lifespan=lifespan)

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
