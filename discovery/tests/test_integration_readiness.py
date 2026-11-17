"""Integration readiness is independent from model inference availability."""
from contextlib import asynccontextmanager

import httpx
from sqlalchemy.exc import SQLAlchemyError

from discovery.app.main import create_app


async def test_liveness_survives_database_failure_and_readiness_is_safe(service):
    @asynccontextmanager
    async def broken_session():
        raise SQLAlchemyError('private connection data')
        yield

    service.store.sessions = broken_session
    app = create_app(store=service.store, provider=service.ollama, broker='')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/health')).status_code == 200
        response = await client.get('/ready')
        assert response.status_code == 503
        assert 'private' not in response.text


async def test_readiness_allows_labelled_fallback_during_model_outage(service):
    service.ollama.failing = True
    app = create_app(store=service.store, provider=service.ollama, broker='')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/ready')
        assert response.status_code == 200
        assert response.json()['broker'] == 'disabled'


async def test_configured_disconnected_broker_is_not_ready(service):
    app = create_app(store=service.store, provider=service.ollama, broker='amqp://unused')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/health')).status_code == 200
        assert (await client.get('/ready')).status_code == 503
