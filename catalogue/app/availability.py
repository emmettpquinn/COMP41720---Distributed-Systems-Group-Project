"""Apply proposed full availability snapshots with transactional duplicate checks."""

from .repository import NotFound
from .schemas import AvailabilityMessage


async def apply_snapshot(connection, message: AvailabilityMessage):
    data = message.payload
    async with connection.transaction():
        event = await connection.fetchval(
            "SELECT event_id FROM events WHERE event_id = $1 FOR UPDATE", data.event_id
        )
        if event is None:
            raise NotFound("Availability event is unknown")
        known = {row["seat_id"] for row in await connection.fetch(
            "SELECT seat_id FROM event_seats WHERE event_id = $1", data.event_id
        )}
        if not set(data.available_seat_ids) <= known:
            raise ValueError("Availability contains an unknown seat")
        inserted = await connection.fetchval(
            "INSERT INTO processed_messages (message_id, event_id, sequence) "
            "VALUES ($1, $2, $3) ON CONFLICT (message_id) DO NOTHING RETURNING message_id",
            message.message_id, data.event_id, data.sequence,
        )
        if inserted is None:
            return "duplicate", None
        applied_at = await connection.fetchval(
            """INSERT INTO availability
               (event_id, sequence, seats_remaining, available_seat_ids, occurred_at)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (event_id) DO UPDATE SET
                   sequence = EXCLUDED.sequence,
                   seats_remaining = EXCLUDED.seats_remaining,
                   available_seat_ids = EXCLUDED.available_seat_ids,
                   occurred_at = EXCLUDED.occurred_at,
                   applied_at = clock_timestamp()
               WHERE availability.sequence < EXCLUDED.sequence
               RETURNING applied_at""",
            data.event_id, data.sequence, data.seats_remaining,
            data.available_seat_ids, message.occurred_at,
        )
        if applied_at is None:
            return "stale", None
    return "applied", (applied_at - message.occurred_at).total_seconds()
