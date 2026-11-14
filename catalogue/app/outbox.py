"""Publish immutable outbox bodies; only confirmed delivery marks a row sent."""

import json


async def mark_published(connection, message_id):
    await connection.execute(
        "UPDATE outbox SET published_at = clock_timestamp() WHERE message_id = $1", message_id
    )


async def dispatch_once(pool, publish, limit=50):
    sent = 0
    for _ in range(limit):
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """SELECT message_id, envelope FROM outbox
                       WHERE published_at IS NULL
                       ORDER BY occurred_at, event_id, event_version
                       FOR UPDATE SKIP LOCKED LIMIT 1"""
                )
                if row is None:
                    break
                body = json.loads(row["envelope"])
                # The production publish function has a bounded confirmation timeout.
                await publish(body)
                await mark_published(connection, row["message_id"])
        sent += 1
    return sent
