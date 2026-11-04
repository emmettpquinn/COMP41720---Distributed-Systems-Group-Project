"""Inspect and replay dead-lettered messages.

    python -m messaging.replay <queue> --list          # show what is parked
    python -m messaging.replay <queue>                 # move every parked message back
    python -m messaging.replay <queue> --limit 5       # move only the first 5

``<queue>`` is the consumer queue, e.g. ``ticketing.booking-updates``. Messages
are read from ``<queue>.dlq`` and republished to ``<queue>`` with their retry
count reset. Consumers are idempotent, so replaying a message that was in fact
handled already is harmless.

Replay only after the cause of the failure is fixed. Otherwise the messages
use up their retries again and come straight back to the DLQ.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import aio_pika

from .consumer import LAST_ERROR_HEADER, RETRIES_HEADER
from .topology import connect_with_retry, get_queue

REPLAYED_HEADER = "x-replayed"


async def replay(url: str, queue_name: str, limit: int | None, list_only: bool) -> int:
    q = get_queue(queue_name)
    connection = await connect_with_retry(url, attempts=3)
    moved = 0
    async with connection:
        channel = await connection.channel()  # publisher confirms on by default
        dlq = await channel.get_queue(q.dlq, ensure=True)
        seen: list[aio_pika.abc.AbstractIncomingMessage] = []

        while limit is None or len(seen) < limit:
            message = await dlq.get(no_ack=False, fail=False)
            if message is None:
                break
            seen.append(message)
            headers = dict(message.headers or {})
            print(
                f"{message.message_id}  type={message.type}  "
                f"retries={headers.get(RETRIES_HEADER, 0)}  "
                f"replayed={headers.get(REPLAYED_HEADER, 0)}  "
                f"error={headers.get(LAST_ERROR_HEADER, '-')}"
            )
            if list_only:
                continue
            headers[RETRIES_HEADER] = 0
            headers[REPLAYED_HEADER] = int(headers.get(REPLAYED_HEADER, 0)) + 1
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=message.body,
                    message_id=message.message_id,
                    type=message.type,
                    content_type=message.content_type,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers=headers,
                ),
                routing_key=q.name,
            )
            await message.ack()
            moved += 1

        if list_only:
            # Return everything to the DLQ untouched, in its original order.
            for message in seen:
                await message.nack(requeue=True)
            print(f"{len(seen)} message(s) parked in {q.dlq}")
        else:
            print(f"Replayed {moved} message(s) from {q.dlq} to {q.name}")
    return moved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("queue", help="consumer queue, e.g. ticketing.booking-updates")
    parser.add_argument("--limit", type=int, default=None, help="replay at most N messages")
    parser.add_argument("--list", action="store_true", help="only list parked messages")
    parser.add_argument("--amqp-url", default=os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/"))
    args = parser.parse_args()
    asyncio.run(replay(args.amqp_url, args.queue, args.limit, args.list))


if __name__ == "__main__":
    main()
