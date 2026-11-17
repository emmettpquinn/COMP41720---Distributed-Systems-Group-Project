"""HTTP API and lifecycle. One uvicorn worker keeps the bulkhead process-wide."""
import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
import secrets
import time
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from messaging.consumer import run_consumer
from messaging.topology import connect_with_retry, declare_topology
from . import metrics
from .embeddings import Ollama
from .service import Discovery
from .simulator import SimulatedOllama
from .store import Store
from .api_models import SearchResponse

log = logging.getLogger(__name__)


def create_app(store=None, provider=None, broker=None, seed=False):
    provider_kind = os.getenv('EMBEDDING_PROVIDER', 'ollama')
    if provider_kind not in {'ollama', 'simulated', 'simulated_http'}:
        raise ValueError('EMBEDDING_PROVIDER must be ollama, simulated, or simulated_http')
    simulation = provider_kind == 'simulated'
    database_url = os.getenv('DATABASE_URL', 'sqlite+aiosqlite:///./discovery-demo.db')
    store = store or Store(database_url)
    provider = provider or (SimulatedOllama() if simulation else Ollama(
        url=os.getenv('OLLAMA_URL', 'http://localhost:11434'),
        timeout=float(os.getenv('OLLAMA_TIMEOUT_SECONDS', '1.5')),
        cooldown=float(os.getenv('OLLAMA_COOLDOWN_SECONDS', '30')),
        threshold=int(os.getenv('OLLAMA_FAILURE_THRESHOLD', '5')),
        price=float(os.getenv('NOTIONAL_PRICE_PER_TOKEN', '0.000001'))))
    simulated = isinstance(provider, SimulatedOllama)
    provider_label = 'simulated' if simulated else provider_kind
    service = Discovery(store, provider, os.getenv('EVENT_TIMEZONE', 'Europe/Dublin'))
    broker = broker if broker is not None else os.getenv('AMQP_URL', '')
    broker_ready = False

    async def worker():
        while True:
            try:
                await service.index_pending()
            except Exception:
                log.exception('Index worker failed; durable jobs will be retried')
            await asyncio.sleep(1)

    async def consume():
        nonlocal broker_ready
        while True:
            connection = None
            try:
                connection = await connect_with_retry(broker)
                channel = await connection.channel(publisher_confirms=True)
                await declare_topology(channel)
                await run_consumer(channel, 'discovery.event-changes', service.handle,
                                   lambda outcome, kind, error: metrics.MESSAGES.labels(outcome.value).inc())
                broker_ready = True
                while not connection.is_closed:
                    broker_ready = bool(connection.connected.is_set())
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Broker connection failed; retrying')
            finally:
                broker_ready = False
                if connection:
                    await connection.close()
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        await store.initialize(model=os.getenv('EMBEDDING_MODEL', 'nomic-embed-text:latest') if not simulated else 'simulated-hash',
                               model_version=os.getenv('MODEL_VERSION', 'unverified-local-tag') if not simulated else 'simulator-v1',
                               dimensions=int(os.getenv('EMBEDDING_DIMENSIONS', '768')) if not simulated else 64)
        if seed or os.getenv('SEED_DEMO') == '1':
            from .seed import demo_messages
            for envelope in demo_messages():
                await service.handle(envelope)
        tasks = [asyncio.create_task(worker())]
        if broker:
            tasks.append(asyncio.create_task(consume()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await provider.close()
            await store.close()

    app = FastAPI(title='Discovery', lifespan=lifespan,
                  swagger_ui_parameters={'defaultModelsExpandDepth': -1})
    app.state.service = service

    @app.middleware('http')
    async def observe(request, call_next):
        start, status = time.monotonic(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = getattr(request.scope.get('route'), 'path', 'unmatched')
            metrics.HTTP.labels(route, str(status)).inc()
            metrics.LATENCY.labels(route).observe(time.monotonic() - start)

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'broker': 'connected' if broker_ready else ('reconnecting' if broker else 'disabled'),
                'embedding_provider': provider_label}

    @app.get('/ready')
    async def ready():
        try:
            async with asyncio.timeout(2):
                async with store.sessions() as session:
                    await session.execute(text('SELECT 1'))
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise HTTPException(503, 'Discovery database is unavailable') from error
        if broker and not broker_ready:
            raise HTTPException(503, 'Discovery broker is reconnecting')
        # Model outages can serve labelled keyword results; they do not fail readiness.
        return {'status': 'ready', 'broker': 'connected' if broker else 'disabled',
                'embedding_provider': provider_label}

    @app.get('/search', response_model=SearchResponse)
    async def search(q: str = Query(min_length=1, max_length=1000), limit: int = Query(default=10, ge=1, le=50)):
        try:
            result = await service.search(q, limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**result, 'embedding_provider': provider_label}

    @app.get('/events/{event_id}/similar', response_model=SearchResponse)
    async def similar(event_id: str, limit: int = Query(default=10, ge=1, le=50)):
        event = await store.event(event_id)
        if not event or event.cancelled:
            raise HTTPException(404, 'Event not found')
        # Treat event content as ranking text, never as date instructions.
        result = await service.similar(event, limit)
        return {**result, 'embedding_provider': provider_label}

    @app.get('/metrics')
    async def prometheus():
        return Response(generate_latest(), headers={'Content-Type': CONTENT_TYPE_LATEST})

    def require_admin(token):
        configured = os.getenv('DISCOVERY_ADMIN_TOKEN', '')
        if not configured or not token or not secrets.compare_digest(token, configured):
            raise HTTPException(403, 'Configure DISCOVERY_ADMIN_TOKEN and pass X-Admin-Token')

    @app.post('/admin/indexes', status_code=201)
    async def add_index(body: NewIndex, x_admin_token: str | None = Header(default=None)):
        require_admin(x_admin_token)
        try:
            await store.add_index(body.version, body.model, body.model_version, body.dimensions)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'status': 'building', 'index_version': body.version}

    @app.post('/admin/indexes/{version}/activate')
    async def activate(version: str, x_admin_token: str | None = Header(default=None)):
        require_admin(x_admin_token)
        try:
            await store.activate(version)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'active': version}

    @app.get('/admin/indexes')
    async def indexes(x_admin_token: str | None = Header(default=None)):
        require_admin(x_admin_token)
        return await store.index_status()

    if simulated:
        @app.post('/demo/outage/{enabled}')
        async def outage(enabled: bool, x_admin_token: str | None = Header(default=None)):
            require_admin(x_admin_token)
            provider.failing = enabled
            return {'simulated_outage': enabled}

    return app


class NewIndex(BaseModel):
    version: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    model_version: str = Field(min_length=1, max_length=200)
    dimensions: int = Field(ge=1, le=4096)


app = create_app()
