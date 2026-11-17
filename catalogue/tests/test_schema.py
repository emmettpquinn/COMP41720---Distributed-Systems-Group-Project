"""PostgreSQL integration checks; every test owns a temporary schema."""

import asyncio

import asyncpg
import pytest

from app.db import MIGRATIONS_DIR, apply_migrations
from .helpers import connect, seed_event

ALL_MIGRATIONS = ["001_catalogue.sql", "002_outbox.sql", "003_availability.sql"]



def test_initialization_and_repeat_preserve_records(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            assert await apply_migrations(connection) == ALL_MIGRATIONS
            await seed_event(connection)
            await connection.execute("UPDATE events SET title = 'Updated Event' WHERE event_id = 'event-1'")
            assert await apply_migrations(connection) == []
            row = await connection.fetchrow("SELECT * FROM events WHERE event_id = 'event-1'")
            assert row["title"] == "Updated Event"
            assert row["event_version"] == 1 and row["status"] == "scheduled"
            assert row["tags"] == ["music", "evening"]
            assert row["starts_at"].utcoffset().total_seconds() == 0
            assert await connection.fetchval("SELECT count(*) FROM schema_migrations") == len(ALL_MIGRATIONS)

    asyncio.run(scenario())


def test_seat_ids_are_unique_within_each_event(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
            await seed_event(connection)
            await seed_event(connection, "event-2")
            insert = "INSERT INTO event_seats VALUES ($1, 'A1', 'A', 'A1')"
            await connection.execute(insert, "event-1")
            with pytest.raises(asyncpg.UniqueViolationError):
                await connection.execute(insert, "event-1")
            await connection.execute(insert, "event-2")
            assert await connection.fetchval("SELECT count(*) FROM event_seats") == 2

    asyncio.run(scenario())


def test_unknown_venue_and_event_references_are_rejected(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute(
                    """INSERT INTO events (event_id, venue_id, title, starts_at, timezone)
                       VALUES ('event-1', 'missing', 'Demo', now(), 'Europe/Dublin')"""
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute("INSERT INTO event_seats VALUES ('missing', 'A1', 'A', 'A1')")
            assert await connection.fetchval("SELECT count(*) FROM events") == 0

    asyncio.run(scenario())


def test_referenced_venue_cannot_be_deleted(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
            await seed_event(connection)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute("DELETE FROM venues WHERE venue_id = 'venue-1'")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "event_id,version,status,title",
    [
        ("event-1", 0, "scheduled", "Demo"),
        ("event-1", -1, "scheduled", "Demo"),
        ("event-1", 1, "unknown", "Demo"),
        ("event-1", 1, "scheduled", " \t"),
        (" \t", 1, "scheduled", "Demo"),
    ],
)
def test_invalid_event_fields_are_rejected(database_schema, event_id, version, status, title):
    async def scenario():
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
            await connection.execute("INSERT INTO venues (venue_id, name) VALUES ('venue-1', 'Demo Venue')")
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """INSERT INTO events
                       (event_id, venue_id, event_version, status, title, starts_at, timezone)
                       VALUES ($1, 'venue-1', $2, $3, $4, now(), 'Europe/Dublin')""",
                    event_id, version, status, title,
                )

    asyncio.run(scenario())


def test_records_survive_new_connection(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
            await seed_event(connection)
            await connection.execute("INSERT INTO event_seats VALUES ('event-1', 'A1', 'A', 'A1')")
        async with connect(database_schema) as connection:
            assert await connection.fetchval("SELECT title FROM events WHERE event_id = 'event-1'") == "Demo Event"
            assert await connection.fetchval("SELECT count(*) FROM event_seats") == 1

    asyncio.run(scenario())


def test_failed_migration_rolls_back_all_pending_changes(database_schema, tmp_path):
    (tmp_path / "001_catalogue.sql").write_text((MIGRATIONS_DIR / "001_catalogue.sql").read_text())
    (tmp_path / "002_broken.sql").write_text("CREATE TABLE rollback_probe (id INT); SELECT missing_function();")

    async def scenario():
        async with connect(database_schema) as connection:
            with pytest.raises(asyncpg.UndefinedFunctionError):
                await apply_migrations(connection, tmp_path)
            tables = await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
            assert tables == []
            assert await apply_migrations(connection) == ALL_MIGRATIONS

    asyncio.run(scenario())


def test_concurrent_migration_runners_apply_once(database_schema):
    async def run():
        async with connect(database_schema) as connection:
            return await apply_migrations(connection)

    async def scenario():
        results = await asyncio.gather(run(), run())
        assert sorted(results, key=len) == [[], ALL_MIGRATIONS]

    asyncio.run(scenario())
