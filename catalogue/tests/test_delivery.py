import asyncio
import json

import asyncpg
import pytest
from messaging import Envelope
from pydantic import ValidationError

from app import outbox
from app.availability import apply_snapshot
from app.repository import NotFound, create_event, create_venue, update_event
from app.schemas import AvailabilityMessage, EventCreate, EventPatch, VenueCreate
from app.seed import seed_database
from app.worker import handle_availability
from app.db import apply_migrations
from .helpers import connect
from .test_api import BODY, rows, venue


async def prepare(connection):
    await apply_migrations(connection)
    await create_venue(connection, VenueCreate(venue_id="venue-api", name="Demo Venue"))
    return await create_event(connection, EventCreate.model_validate(BODY | {
        "seats": BODY["seats"] + [{"seat_id": "A2", "section": "A", "label": "A2"}]
    }))


def envelope(sequence=1, seats=None, event_id="event-api"):
    seats = ["A1"] if seats is None else seats
    return Envelope.new("seat.availability-changed", {
        "event_id": event_id, "sequence": sequence,
        "seats_remaining": len(seats), "available_seat_ids": seats,
    }, "booking")


def model(env):
    return AvailabilityMessage.model_validate_json(env.to_json())


async def pool_for(database_schema):
    return await asyncpg.create_pool(
        database_schema[0], server_settings={"search_path": database_schema[1]},
        min_size=0, max_size=4,
    )


def test_duplicates_reordering_and_releases(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await prepare(connection)
            latest = model(envelope(10))
            assert (await apply_snapshot(connection, latest))[0] == "applied"
            assert (await apply_snapshot(connection, latest))[0] == "duplicate"
            assert (await apply_snapshot(connection, model(envelope(9, []))))[0] == "stale"
            assert await connection.fetchval("SELECT sequence FROM availability") == 10
            assert (await apply_snapshot(connection, model(envelope(11, ["A1", "A2"]))))[0] == "applied"
            assert await connection.fetchval("SELECT seats_remaining FROM availability") == 2
            assert await connection.fetchval("SELECT count(*) FROM processed_messages") == 3
    asyncio.run(scenario())


def test_unknown_events_seats_and_failed_apply_leave_no_inbox_record(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await prepare(connection)
            with pytest.raises(NotFound):
                await apply_snapshot(connection, model(envelope(event_id="missing")))
            with pytest.raises(ValueError):
                await apply_snapshot(connection, model(envelope(seats=["missing"])))
            valid = model(envelope())
            # Inject a DB constraint failure after the inbox insert to prove rollback.
            invalid = valid.model_copy(update={"payload": valid.payload.model_copy(update={"seats_remaining": 99})})
            with pytest.raises(asyncpg.CheckViolationError):
                await apply_snapshot(connection, invalid)
            assert await connection.fetchval("SELECT count(*) FROM processed_messages") == 0
            assert await connection.fetchval("SELECT count(*) FROM availability") == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("update", [
    {"sequence": True}, {"sequence": 0}, {"sequence": 2**63},
    {"seats_remaining": 2}, {"available_seat_ids": ["A1", "A1"], "seats_remaining": 2},
])
def test_invalid_availability_is_rejected_before_processing(update):
    data = json.loads(envelope().to_json())
    data["payload"].update(update)
    with pytest.raises(ValidationError):
        AvailabilityMessage.model_validate(data)


def test_concurrent_snapshots_keep_highest_sequence(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await prepare(connection)
        pool = await pool_for(database_schema)
        try:
            async def apply(sequence):
                async with pool.acquire() as connection:
                    await apply_snapshot(connection, model(envelope(sequence, ["A1", "A2"] if sequence == 20 else [])))
            await asyncio.gather(*(apply(sequence) for sequence in range(20, 0, -1)))
            async with pool.acquire() as connection:
                row = await connection.fetchrow("SELECT * FROM availability")
                assert row["sequence"] == 20 and row["seats_remaining"] == 2
        finally:
            await pool.close()
    asyncio.run(scenario())


def test_outbox_failure_and_crash_keep_stable_message_id(database_schema, monkeypatch):
    async def scenario():
        async with connect(database_schema) as connection:
            await prepare(connection)
        pool = await pool_for(database_schema)
        observed = []

        async def fail_publish(body):
            observed.append(body["message_id"])
            raise ConnectionError("synthetic broker outage")

        async def published(body):
            observed.append(body["message_id"])

        async def crash(*args):
            raise RuntimeError("synthetic failure after broker delivery")

        try:
            with pytest.raises(ConnectionError):
                await outbox.dispatch_once(pool, fail_publish)
            original = outbox.mark_published
            monkeypatch.setattr(outbox, "mark_published", crash)
            with pytest.raises(RuntimeError):
                await outbox.dispatch_once(pool, published)
            monkeypatch.setattr(outbox, "mark_published", original)
            assert await outbox.dispatch_once(pool, published) == 1
            assert len(observed) == 3 and len(set(observed)) == 1
            async with pool.acquire() as connection:
                assert await connection.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL") == 0
        finally:
            await pool.close()
    asyncio.run(scenario())


def test_concurrent_dispatchers_do_not_claim_the_same_row(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await prepare(connection)
            await update_event(connection, "event-api", EventPatch(expected_version=1, title="Updated"))
        pool = await pool_for(database_schema)
        observed = []

        async def publish(body):
            observed.append(body["message_id"])
            await asyncio.sleep(0.01)

        try:
            await asyncio.gather(outbox.dispatch_once(pool, publish), outbox.dispatch_once(pool, publish))
            assert len(observed) == len(set(observed)) == 2
        finally:
            await pool.close()
    asyncio.run(scenario())


def test_handler_invalidates_http_cache_and_exports_lag(client, database_schema):
    venue(client)
    assert client.post("/events", json=BODY).status_code == 201
    assert client.get("/events/event-api").json()["availability"] is None
    assert client.get("/events/event-api").json()["availability"] is None

    async def scenario():
        pool = await pool_for(database_schema)
        try:
            await handle_availability(pool, envelope(), client.app.state.cache, client.app.state.metrics)
        finally:
            await pool.close()

    asyncio.run(scenario())
    assert client.get("/events/event-api").json()["availability"]["sequence"] == 1
    metrics = client.get("/metrics").text
    assert 'catalogue_availability_messages_total{outcome="applied"} 1.0' in metrics
    assert 'catalogue_cache_requests_total{result="hit"}' in metrics
    assert 'catalogue_availability_lag_seconds_count' in metrics


def test_metadata_update_invalidates_cache_and_metrics_use_route_templates(client):
    venue(client)
    client.post("/events", json=BODY)
    client.get("/events/event-api")
    client.patch("/events/event-api", json={"expected_version": 1, "title": "Updated"})
    assert client.get("/events/event-api").json()["title"] == "Updated"
    client.get("/events/private-test-identifier")
    metrics = client.get("/metrics").text
    assert 'route="/events/{event_id}"' in metrics
    assert 'private-test-identifier' not in metrics
    assert 'catalogue_outbox_pending 2.0' in metrics
    assert 'catalogue_messaging_enabled 0.0' in metrics


def test_repeatable_seeds_preserve_edits_and_outbox(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
            first = await seed_database(connection)
            assert first == {"venues_created": 1, "events_created": 2}
            await update_event(connection, "event-demo-001", EventPatch(expected_version=1, title="Edited Event"))
            assert await seed_database(connection) == {"venues_created": 0, "events_created": 0}
            assert await connection.fetchval("SELECT title FROM events WHERE event_id = 'event-demo-001'") == "Edited Event"
            assert await connection.fetchval("SELECT count(*) FROM outbox") == 3
    asyncio.run(scenario())


def test_cancelled_metadata_cannot_be_reactivated_by_availability(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await prepare(connection)
            await update_event(connection, "event-api", EventPatch(expected_version=1, status="cancelled"))
            await apply_snapshot(connection, model(envelope(1, ["A1", "A2"])))
            assert await connection.fetchval("SELECT status FROM events") == "cancelled"
            assert await connection.fetchval("SELECT event_version FROM events") == 2
    asyncio.run(scenario())


def test_concurrent_seed_runs_do_not_duplicate_events(database_schema):
    async def scenario():
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
        pool = await pool_for(database_schema)
        try:
            async def seed():
                async with pool.acquire() as connection:
                    return await seed_database(connection)
            results = await asyncio.gather(seed(), seed())
            assert sorted(result["events_created"] for result in results) == [0, 2]
            async with pool.acquire() as connection:
                assert await connection.fetchval("SELECT count(*) FROM events") == 2
                assert await connection.fetchval("SELECT count(*) FROM outbox") == 2
        finally:
            await pool.close()
    asyncio.run(scenario())


def test_seed_failure_rolls_back_the_whole_dataset(database_schema):
    async def scenario():
        from app.seed import SEED_FILE
        data = json.loads(SEED_FILE.read_text())
        data["events"][1]["venue_id"] = "missing-venue"
        async with connect(database_schema) as connection:
            await apply_migrations(connection)
            with pytest.raises(NotFound):
                await seed_database(connection, data)
            for table in ("venues", "events", "event_seats", "outbox"):
                assert await connection.fetchval("SELECT count(*) FROM " + table) == 0
    asyncio.run(scenario())
