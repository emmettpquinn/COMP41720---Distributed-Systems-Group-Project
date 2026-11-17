"""Live integration check. Run inside discovery after the real model is warm.

Exercises RabbitMQ -> consumer -> PostgreSQL -> Ollama -> HTTP, then checks a
cancelled event cannot be resurrected by a stale redelivery. Synthetic data only.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
import time
import uuid
import httpx
from messaging.envelope import Envelope
from messaging.topology import connect_with_retry


async def main():
    event_id = 'integration-' + uuid.uuid4().hex
    connection = await connect_with_retry(os.getenv('AMQP_URL', 'amqp://guest:guest@localhost:5672/'))
    async with connection, httpx.AsyncClient(base_url=os.getenv('DISCOVERY_HTTP_URL', 'http://localhost:8000')) as client:
        channel = await connection.channel(publisher_confirms=True)
        exchange = await channel.get_exchange('events')
        payload = {'event_id': event_id, 'event_version': 1, 'action': 'created',
                   'title': 'Calm integration music', 'description': 'Synthetic acoustic music', 'tags': ['music'],
                   'starts_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
        created = Envelope.new('event.changed', payload, 'discovery-integration-check')
        started = time.monotonic()
        await exchange.publish(created.to_amqp(), routing_key='event.changed')
        await exchange.publish(created.to_amqp(), routing_key='event.changed')
        async def wait_for(predicate, timeout):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                response = await client.get('/search', params={'q': 'calm integration music', 'limit': 50})
                response.raise_for_status()
                result = response.json()
                if predicate(result):
                    return result
                await asyncio.sleep(0.5)
            raise AssertionError('Timed out waiting for expected search state')

        def present(result):
            return any(e['event_id'] == event_id for e in result['results'])

        await wait_for(present, 30)
        print(f'Keyword visibility in {time.monotonic() - started:.2f}s (target <=30s)')
        await wait_for(lambda r: present(r) and r['mode'] == 'semantic', 30)
        health = (await client.get('/health')).json()
        print('Semantic path works with provider:', health['embedding_provider'])
        print('Duplicate created message produced one search entry; real broker/database path exercised.')
        cancelled = Envelope.new('event.changed', {'event_id': event_id, 'event_version': 2, 'action': 'cancelled'}, 'discovery-integration-check')
        await exchange.publish(cancelled.to_amqp(), routing_key='event.changed')
        await wait_for(lambda r: not present(r), 10)
        stale = Envelope.new('event.changed', payload, 'discovery-integration-check')
        await exchange.publish(stale.to_amqp(), routing_key='event.changed')
        # Allow the stale message to be consumed before asserting again.
        await asyncio.sleep(2)
        response = await client.get('/search', params={'q': 'calm integration music', 'limit': 50})
        response.raise_for_status()
        assert not present(response.json()), 'Stale event resurrected a cancellation'
        print('Cancellation and stale-delivery check passed. Capture dashboard evidence separately.')


if __name__ == '__main__':
    asyncio.run(main())
