import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import asyncpg
import pytest

from app import repository
from .helpers import connect

BODY = {
    "event_id": "event-api", "venue_id": "venue-api", "title": "Demo Event",
    "starts_at": "2026-11-27T19:30:00+00:00", "timezone": "Europe/Dublin",
    "tags": ["music"], "seats": [{"seat_id": "A1", "section": "A", "label": "A1"}],
}



def venue(client):
    return client.post("/venues", json={"venue_id": "venue-api", "name": "Demo Venue", "city": "Dublin"})


def rows(database_schema, query):
    async def run():
        async with connect(database_schema) as connection:
            return [dict(row) for row in await connection.fetch(query)]
    return asyncio.run(run())


def test_browse_and_detail_preserve_unknown_availability(client):
    assert client.get("/events").json() == []
    assert client.get("/events/missing").status_code == 404
    assert client.get("/venues/missing").status_code == 404
    assert venue(client).status_code == 201
    assert client.get("/venues/venue-api").json()["name"] == "Demo Venue"
    created = client.post("/events", json=BODY)
    assert created.status_code == 201, created.text
    detail = client.get("/events/event-api").json()
    assert detail["availability"] is None and detail["seats"][0]["seat_id"] == "A1"
    assert detail["venue"]["venue_id"] == "venue-api"
    summary = client.get("/events").json()[0]
    assert summary["event_id"] == "event-api" and "seats" not in summary


def test_create_is_atomic_with_outbox_and_conflicts(client, database_schema):
    assert client.post("/events", json=BODY).status_code == 404
    assert rows(database_schema, "SELECT * FROM events") == []
    venue(client)
    assert venue(client).status_code == 409
    assert client.post("/events", json=BODY).status_code == 201
    assert client.post("/events", json=BODY).status_code == 409
    outgoing = rows(database_schema, "SELECT envelope FROM outbox")
    assert len(outgoing) == 1
    envelope = json.loads(outgoing[0]["envelope"])
    assert envelope["payload"]["event_version"] == 1
    assert envelope["payload"]["change"] == "created"
    assert envelope["payload"]["seats"][0]["seat_id"] == "A1"


def test_outbox_failure_rolls_back_event_and_seats(client, database_schema):
    venue(client)

    async def fail(*args):
        raise asyncpg.PostgresError("synthetic write failure")

    with patch.object(repository, "append_event", fail):
        assert client.post("/events", json=BODY).status_code == 503
    for table in ("events", "event_seats", "outbox"):
        assert rows(database_schema, "SELECT * FROM " + table) == []


@pytest.mark.parametrize("changes", [
    {"seats": []},
    {"seats": [BODY["seats"][0], BODY["seats"][0]]},
    {"starts_at": "2026-11-27T19:30:00"},
    {"timezone": "Invalid/Timezone"},
    {"title": "  "},
    {"event_id": "x" * 65},
    {"event_version": 99},
])
def test_invalid_event_requests_do_not_write(client, database_schema, changes):
    venue(client)
    assert client.post("/events", json=BODY | changes).status_code == 422
    assert rows(database_schema, "SELECT * FROM events") == []
    assert rows(database_schema, "SELECT * FROM outbox") == []


def test_update_versions_and_cancellation_are_atomic(client, database_schema):
    venue(client)
    client.post("/events", json=BODY)
    changed = client.patch("/events/event-api", json={"expected_version": 1, "title": "Updated Event"})
    assert changed.status_code == 200 and changed.json()["event_version"] == 2
    assert client.patch("/events/event-api", json={"expected_version": 1, "title": "Stale"}).status_code == 409
    assert client.patch("/events/event-api", json={"expected_version": 2, "seats": []}).status_code == 422
    cancelled = client.patch("/events/event-api", json={"expected_version": 2, "status": "cancelled"})
    assert cancelled.status_code == 200 and cancelled.json()["event_version"] == 3
    assert client.patch("/events/event-api", json={"expected_version": 3, "status": "cancelled"}).json()["event_version"] == 3
    assert client.patch("/events/event-api", json={"expected_version": 3, "title": "Resurrect"}).status_code == 409
    assert client.patch("/events/event-api", json={"expected_version": 3, "status": "scheduled"}).status_code == 422
    assert client.get("/events/event-api").json()["status"] == "cancelled"
    messages = rows(database_schema, "SELECT envelope FROM outbox ORDER BY event_version")
    assert [json.loads(row["envelope"])["payload"]["change"] for row in messages] == ["created", "updated", "cancelled"]


def test_update_outbox_failure_preserves_previous_version(client, database_schema):
    venue(client)
    client.post("/events", json=BODY)

    async def fail(*args):
        raise asyncpg.PostgresError("synthetic failure")

    with patch.object(repository, "append_event", fail):
        assert client.patch("/events/event-api", json={"expected_version": 1, "title": "Lost"}).status_code == 503
    result = client.get("/events/event-api").json()
    assert result["event_version"] == 1 and result["title"] == BODY["title"]
    assert len(rows(database_schema, "SELECT * FROM outbox")) == 1


def test_concurrent_updates_have_one_version_winner(client, database_schema):
    venue(client)
    client.post("/events", json=BODY)

    def update(number):
        return client.patch("/events/event-api", json={"expected_version": 1, "title": f"Edit {number}"}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(update, range(8)))
    assert statuses.count(200) == 1 and statuses.count(409) == 7
    assert client.get("/events/event-api").json()["event_version"] == 2
    assert len(rows(database_schema, "SELECT * FROM outbox")) == 2


@pytest.mark.parametrize("body", [
    {"expected_version": 1},
    {"expected_version": 1, "title": None},
    {"expected_version": True, "title": "Invalid version"},
])
def test_invalid_patch_is_rejected_without_version_change(client, database_schema, body):
    venue(client)
    client.post("/events", json=BODY)
    assert client.patch("/events/event-api", json=body).status_code == 422
    assert client.get("/events/event-api").json()["event_version"] == 1
    assert len(rows(database_schema, "SELECT * FROM outbox")) == 1


@pytest.mark.parametrize('invalid', [
    {'title': 't' * 501}, {'description': 'd' * 8001},
    {'tags': ['tag'] * 51}, {'tags': ['t' * 201]},
])
def test_discovery_limits_reject_before_event_or_outbox_write(client, database_schema, invalid):
    venue(client)
    assert client.post('/events', json=BODY | invalid).status_code == 422
    assert rows(database_schema, 'SELECT * FROM events') == []
    assert rows(database_schema, 'SELECT * FROM outbox') == []
    assert client.post('/events', json=BODY).status_code == 201
    assert client.patch('/events/event-api', json={'expected_version': 1} | invalid).status_code == 422
    assert rows(database_schema, 'SELECT event_version FROM events')[0]['event_version'] == 1
    assert len(rows(database_schema, 'SELECT * FROM outbox')) == 1


def test_discovery_limit_boundaries_remain_accepted(client):
    venue(client)
    body = BODY | {'title': 't' * 500, 'description': 'd' * 8000, 'tags': ['t' * 200] * 50}
    created = client.post('/events', json=body)
    assert created.status_code == 201
    assert len(created.json()['tags']) == 50
    result = client.patch('/events/event-api', json={'expected_version': 1, 'tags': ['u' * 200] * 50})
    assert result.status_code == 200 and result.json()['event_version'] == 2


def test_partial_patch_of_legacy_oversized_text_cannot_queue_invalid_snapshot(client, database_schema):
    venue(client)
    assert client.post('/events', json=BODY).status_code == 201
    async def legacy_data():
        async with connect(database_schema) as connection:
            await connection.execute("UPDATE events SET title=$1 WHERE event_id='event-api'", 'x' * 501)
    asyncio.run(legacy_data())
    response = client.patch('/events/event-api', json={'expected_version': 1, 'description': 'A safe new description'})
    assert response.status_code == 409
    saved = rows(database_schema, 'SELECT event_version, description FROM events')[0]
    assert saved['event_version'] == 1 and saved['description'] == ''
    assert len(rows(database_schema, 'SELECT * FROM outbox')) == 1
    repaired = client.patch('/events/event-api', json={'expected_version': 1, 'title': 'Repaired event title'})
    assert repaired.status_code == 200 and repaired.json()['event_version'] == 2
