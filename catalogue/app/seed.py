"""Repeatable synthetic Catalogue seeds; existing records are never reset."""

import asyncio
import json
from pathlib import Path

import asyncpg

from .config import Settings
from .repository import create_event, create_venue
from .schemas import EventCreate, VenueCreate

SEED_FILE = Path(__file__).resolve().parents[1] / "seed-data.json"


async def seed_database(connection, data=None):
    data = json.loads(SEED_FILE.read_text()) if data is None else data
    venues = [VenueCreate.model_validate(item) for item in data["venues"]]
    events = [EventCreate.model_validate(item) for item in data["events"]]
    counts = {"venues_created": 0, "events_created": 0}
    async with connection.transaction():
        schema = await connection.fetchval("SELECT current_schema()")
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"catalogue:seed:{schema}"
        )
        for venue in venues:
            if not await connection.fetchval("SELECT 1 FROM venues WHERE venue_id = $1", venue.venue_id):
                await create_venue(connection, venue)
                counts["venues_created"] += 1
        for event in events:
            if not await connection.fetchval("SELECT 1 FROM events WHERE event_id = $1", event.event_id):
                await create_event(connection, event)
                counts["events_created"] += 1
    return counts


async def main():
    settings = Settings.from_env()
    connection = await asyncpg.connect(settings.database_url, timeout=settings.database_timeout_seconds)
    try:
        print(json.dumps(await seed_database(connection)))
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
