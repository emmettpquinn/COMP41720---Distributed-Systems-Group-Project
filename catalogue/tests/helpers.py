"""Database helpers for synthetic test fixtures only."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg

@asynccontextmanager
async def connect(database_schema):
    url, schema = database_schema
    connection = await asyncpg.connect(
        url, server_settings={"search_path": schema}, timeout=5, command_timeout=5
    )
    try:
        yield connection
    finally:
        await connection.close()


async def seed_event(connection, event_id="event-1"):
    await connection.execute(
        "INSERT INTO venues (venue_id, name, city) VALUES ('venue-1', 'Demo Venue', 'Dublin') "
        "ON CONFLICT (venue_id) DO NOTHING"
    )
    await connection.execute(
        """INSERT INTO events (event_id, venue_id, title, starts_at, timezone, tags)
           VALUES ($1, 'venue-1', 'Demo Event', $2, 'Europe/Dublin', $3)""",
        event_id,
        datetime(2026, 11, 27, 19, 30, tzinfo=timezone.utc),
        ["music", "evening"],
    )
