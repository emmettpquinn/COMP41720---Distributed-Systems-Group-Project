"""Catalogue-owned state changes; event writes and outgoing snapshots are atomic."""

import json
import uuid
from datetime import datetime, timezone

from .schemas import EventCreate, EventPatch, VenueCreate


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


async def get_venue(connection, venue_id):
    row = await connection.fetchrow(
        "SELECT venue_id, name, city FROM venues WHERE venue_id = $1", venue_id
    )
    if row is None:
        raise NotFound("Venue not found")
    return dict(row)


async def create_venue(connection, data: VenueCreate):
    return dict(await connection.fetchrow(
        "INSERT INTO venues (venue_id, name, city) VALUES ($1, $2, $3) "
        "RETURNING venue_id, name, city", data.venue_id, data.name, data.city,
    ))


async def list_events(connection):
    rows = await connection.fetch(
        """SELECT e.event_id, e.event_version, e.title, e.starts_at, e.timezone,
                  e.status, v.venue_id, v.name, v.city
           FROM events e JOIN venues v USING (venue_id)
           ORDER BY e.starts_at, e.event_id"""
    )
    return [
        {key: row[key] for key in ("event_id", "event_version", "title", "starts_at", "timezone", "status")}
        | {"venue": {key: row[key] for key in ("venue_id", "name", "city")}}
        for row in rows
    ]


async def event_snapshot(connection, event_id):
    row = await connection.fetchrow(
        """SELECT e.*, v.name AS venue_name, v.city AS venue_city
           FROM events e JOIN venues v USING (venue_id) WHERE e.event_id = $1""", event_id,
    )
    if row is None:
        raise NotFound("Event not found")
    seats = await connection.fetch(
        "SELECT seat_id, section, label FROM event_seats "
        "WHERE event_id = $1 ORDER BY section, seat_id", event_id,
    )
    return {
        "event_id": row["event_id"], "event_version": row["event_version"],
        "status": row["status"], "title": row["title"], "description": row["description"],
        "starts_at": row["starts_at"], "timezone": row["timezone"], "tags": row["tags"],
        "venue": {"venue_id": row["venue_id"], "name": row["venue_name"], "city": row["venue_city"]},
        "seats": [dict(seat) for seat in seats],
    }


async def event_detail(connection, event_id):
    # One snapshot across metadata, seat map and availability reads.
    async with connection.transaction(isolation="repeatable_read", readonly=True):
        event = await event_snapshot(connection, event_id)
        projection = await connection.fetchrow(
            "SELECT sequence, seats_remaining, available_seat_ids, occurred_at, applied_at "
            "FROM availability WHERE event_id = $1", event_id,
        )
        return event | {"availability": dict(projection) if projection else None}


async def append_event(connection, event_id, change):
    snapshot = await event_snapshot(connection, event_id)
    occurred_at = datetime.now(timezone.utc)
    message_id = uuid.uuid4()
    envelope = {
        "message_id": str(message_id), "type": "event.changed",
        "occurred_at": occurred_at.isoformat(), "version": 1,
        "producer": "catalogue", "payload": {"change": change, **snapshot},
    }
    await connection.execute(
        "INSERT INTO outbox (message_id, event_id, event_version, envelope, occurred_at) "
        "VALUES ($1, $2, $3, $4::jsonb, $5)",
        message_id, event_id, snapshot["event_version"],
        json.dumps(envelope, default=lambda value: value.isoformat()), occurred_at,
    )
    return snapshot


async def create_event(connection, data: EventCreate):
    async with connection.transaction():
        if not await connection.fetchval(
            "SELECT venue_id FROM venues WHERE venue_id = $1 FOR KEY SHARE", data.venue_id
        ):
            raise NotFound("Venue not found")
        await connection.execute(
            """INSERT INTO events
               (event_id, venue_id, title, description, starts_at, timezone, tags)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            data.event_id, data.venue_id, data.title, data.description,
            data.starts_at, data.timezone, data.tags,
        )
        await connection.executemany(
            "INSERT INTO event_seats (event_id, seat_id, section, label) VALUES ($1, $2, $3, $4)",
            [(data.event_id, seat.seat_id, seat.section, seat.label) for seat in data.seats],
        )
        result = await append_event(connection, data.event_id, "created")
    return result | {"availability": None}


async def update_event(connection, event_id, data: EventPatch):
    changes = data.model_dump(exclude_unset=True, exclude={"expected_version"})
    async with connection.transaction():
        current = await connection.fetchrow("SELECT * FROM events WHERE event_id = $1 FOR UPDATE", event_id)
        if current is None:
            raise NotFound("Event not found")
        if current["event_version"] != data.expected_version:
            raise Conflict("Event version has changed; reload the event")
        if current["status"] == "cancelled" and changes != {"status": "cancelled"}:
            raise Conflict("Cancelled events cannot be changed")
        if any(current[key] != value for key, value in changes.items()):
            # Field names originate only from EventPatch, never raw request keys.
            allowed = {"title", "description", "starts_at", "timezone", "tags", "status"}
            if not changes.keys() <= allowed:
                raise ValueError("Unsupported update field")
            assignments = ", ".join(f"{key} = ${index}" for index, key in enumerate(changes, 2))
            await connection.execute(
                f"UPDATE events SET {assignments}, event_version = event_version + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE event_id = $1",
                event_id, *changes.values(),
            )
            await append_event(connection, event_id, "cancelled" if changes.get("status") == "cancelled" else "updated")
        result = await event_snapshot(connection, event_id)
        projection = await connection.fetchrow(
            "SELECT sequence, seats_remaining, available_seat_ids, occurred_at, applied_at "
            "FROM availability WHERE event_id = $1", event_id,
        )
    return result | {"availability": dict(projection) if projection else None}
