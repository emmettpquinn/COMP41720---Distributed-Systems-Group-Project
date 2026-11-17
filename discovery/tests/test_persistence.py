import asyncio
from datetime import datetime, timezone
from discovery.app.seed import demo_messages
from discovery.app.store import Store
from discovery.app.service import Discovery
from discovery.app.simulator import SimulatedOllama


async def test_concurrent_duplicate_deliveries_apply_once(service):
    message = demo_messages()[0]
    results = await asyncio.gather(*(service.handle(message) for _ in range(100)))
    assert results.count('applied') == 1
    assert results.count('duplicate') == 99


async def test_projection_and_pending_work_survive_restart(tmp_path):
    url = f'sqlite+aiosqlite:///{tmp_path}/restart.db'
    first = Store(url)
    await first.initialize(model='simulated', model_version='test', dimensions=64)
    message = demo_messages()[0]
    await Discovery(first, SimulatedOllama()).handle(message)
    await first.close()
    second = Store(url)
    await second.initialize()
    service = Discovery(second, SimulatedOllama())
    assert await service.handle(message) == 'duplicate'
    assert len(await second.pending()) == 1
    assert await service.index_pending() == 1
    assert not await second.pending()
    await second.close()
