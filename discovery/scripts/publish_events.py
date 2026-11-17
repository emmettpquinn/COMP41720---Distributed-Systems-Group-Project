"""Publish synthetic events through the real broker, not an ingestion shortcut."""
import asyncio
import os
from messaging.topology import connect_with_retry, declare_topology
from discovery.app.seed import demo_messages


async def main():
    connection = await connect_with_retry(os.getenv('AMQP_URL', 'amqp://guest:guest@localhost:5672/'))
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        await declare_topology(channel)
        exchange = await channel.get_exchange('events')
        for envelope in demo_messages():
            await exchange.publish(envelope.to_amqp(), routing_key=envelope.type)
            print(envelope.payload['event_id'], envelope.payload['starts_at'])


if __name__ == '__main__':
    asyncio.run(main())
