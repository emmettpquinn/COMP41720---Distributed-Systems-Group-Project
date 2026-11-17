import os
import pytest_asyncio
from discovery.app.store import Store
from discovery.app.service import Discovery
from discovery.app.simulator import SimulatedOllama


@pytest_asyncio.fixture
async def service(tmp_path):
    store = Store(os.getenv('DISCOVERY_TEST_DATABASE_URL', f'sqlite+aiosqlite:///{tmp_path}/test.db'))
    # PostgreSQL test URL must point to a disposable database; tests clear these tables.
    if os.getenv('DISCOVERY_TEST_DATABASE_URL'):
        from discovery.app.store import Base
        async with store.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
    await store.initialize(model='simulated-hash', model_version='simulator-v1', dimensions=64)
    instance = Discovery(store, SimulatedOllama())
    yield instance
    await store.close()
