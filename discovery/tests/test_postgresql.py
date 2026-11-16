"""Only these extra multi-connection checks require a disposable PostgreSQL URL."""
import asyncio
from dataclasses import replace
import os
import pytest
from discovery.app.store import Store
from discovery.app.service import Discovery
from discovery.app.simulator import SimulatedOllama
from discovery.app.seed import demo_messages

pytestmark = pytest.mark.skipif(not os.getenv('DISCOVERY_TEST_DATABASE_URL'), reason='Requires disposable PostgreSQL database')


async def test_two_consumers_keep_one_message_effect(service):
    other_store = Store(os.environ['DISCOVERY_TEST_DATABASE_URL'])
    other = Discovery(other_store, SimulatedOllama())
    message = demo_messages()[0]
    try:
        results = await asyncio.gather(*(instance.handle(message) for instance in [service, other] * 20))
        assert results.count('applied') == 1
        assert results.count('duplicate') == 39
        # Deliver distinct versions through different connections in arbitrary order.
        versions = [10, 3, 7, 20, 2, 12]
        await asyncio.gather(*(instance.handle(replace(message, message_id=f'update-{version}',
            payload={**message.payload, 'event_version': version}))
            for instance, version in zip([service, other] * 3, versions)))
        assert (await service.store.event(message.payload['event_id'])).version == 20
    finally:
        await other_store.close()
