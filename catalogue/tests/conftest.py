"""Isolated PostgreSQL schemas shared by Catalogue integration tests."""

import asyncio
import os
import uuid

import asyncpg
import pytest

from fastapi.testclient import TestClient
from app.config import Settings
from app.db import apply_migrations
from app.main import create_app
from .helpers import connect

@pytest.fixture
def database_schema():
    url = os.getenv("CATALOGUE_TEST_DB")
    if not url:
        pytest.skip("Set CATALOGUE_TEST_DB to run the PostgreSQL integration checks")
    # The identifier is generated here, never read from a request or environment.
    schema = "catalogue_test_" + uuid.uuid4().hex

    async def create():
        connection = await asyncpg.connect(url, timeout=5)
        try:
            await connection.execute(f'CREATE SCHEMA "{schema}"')
        finally:
            await connection.close()

    async def cleanup():
        connection = await asyncpg.connect(url, timeout=5)
        try:
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await connection.close()

    asyncio.run(create())
    try:
        yield url, schema
    finally:
        asyncio.run(cleanup())





@pytest.fixture
def client(database_schema, monkeypatch):
    async def migrate():
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
    asyncio.run(migrate())
    original = asyncpg.create_pool

    async def create_pool(*args, **kwargs):
        kwargs.setdefault("server_settings", {"search_path": database_schema[1]})
        return await original(*args, **kwargs)

    monkeypatch.setattr("app.main.asyncpg.create_pool", create_pool)
    app = create_app(Settings(database_url=database_schema[0]))
    with TestClient(app) as test_client:
        yield test_client
