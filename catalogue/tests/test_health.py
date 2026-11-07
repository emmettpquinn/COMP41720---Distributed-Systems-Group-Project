"""Liveness must stay available even when the database cannot serve requests."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeConnection:
    def __init__(self, outcome):
        self.outcome = outcome

    async def fetchval(self, query):
        assert query == "SELECT 1"
        if self.outcome == "slow":
            await asyncio.sleep(60)
        return 1


class FakePool:
    def __init__(self, outcome):
        self.outcome = outcome
        self.acquisitions = 0
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        self.acquisitions += 1
        if self.outcome == "down":
            raise ConnectionRefusedError("sensitive connection details")
        if self.outcome == "slow_acquire":
            await asyncio.sleep(60)
        yield FakeConnection(self.outcome)

    async def close(self):
        self.closed = True


@pytest.fixture
def make_client(monkeypatch):
    def factory(outcome="ok"):
        pool = FakePool(outcome)

        async def create_pool(**kwargs):
            assert kwargs["min_size"] == 0
            return pool

        monkeypatch.setattr("app.main.asyncpg.create_pool", create_pool)
        app = create_app(Settings(database_url="postgresql://unused", database_timeout_seconds=0.05))
        return TestClient(app), pool

    return factory


def test_liveness_does_not_acquire_database_connection(make_client):
    client, pool = make_client("down")
    with client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"service": "catalogue", "status": "ok"}
        assert pool.acquisitions == 0
    assert pool.closed


def test_readiness_queries_database(make_client):
    client, pool = make_client()
    with client:
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"service": "catalogue", "status": "ready"}
        assert pool.acquisitions == 1


@pytest.mark.parametrize("outcome", ["down", "slow", "slow_acquire"])
def test_unavailable_database_returns_503_but_liveness_stays_ok(make_client, outcome):
    client, pool = make_client(outcome)
    with client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Catalogue database is unavailable"}
        assert client.get("/health").status_code == 200
    assert pool.closed
