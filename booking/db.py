"""All Postgres connection code for the Booking service."""
import time
import uuid
from pathlib import Path

import psycopg2

import config

RETRIES = 10          # how many times to try connecting
WAIT_SECONDS = 2      # pause between tries


def connect_once():
    """Bounded request-time connection; startup has its own retry budget."""
    return psycopg2.connect(config.DATABASE_URL, connect_timeout=2, options="-c statement_timeout=2000")


def connect():
    """Open a connection to Postgres, retrying if it isn't ready yet."""
    for attempt in range(1, RETRIES + 1):
        try:
            return connect_once()
        except psycopg2.OperationalError as error:
            if attempt == RETRIES:
                raise
            print(f"Postgres not ready (try {attempt}/{RETRIES}): {error}")
            time.sleep(WAIT_SECONDS)


def init_schema():
    """Run schema.sql. Safe to call on every start."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    conn = connect()
    try:
        with conn, conn.cursor() as cur:   # 'with conn' commits on success
            cur.execute(sql)
    finally:
        conn.close()


def hold_seat(event_id, seat_id, customer_id):
    """Try to hold a seat. Returns (outcome, booking_ref).

    outcome is one of: 'held', 'already_yours', 'taken', 'not_found',
    'event_cancelled'.
    """
    new_ref = str(uuid.uuid4())
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            # The ONE conditional update: it only succeeds if the seat is free right now
            # and its event is not cancelled. A seat with no event record can still be held.
            cur.execute(
                """UPDATE seats
                   SET status = 'held', booking_ref = %s, customer_id = %s
                   WHERE event_id = %s AND seat_id = %s AND status = 'free'
                     AND NOT EXISTS (SELECT 1 FROM events
                                     WHERE events.event_id = seats.event_id
                                       AND events.status = 'cancelled')""",
                (new_ref, customer_id, event_id, seat_id),
            )
            if cur.rowcount == 1:
                return "held", new_ref

            # The update changed nothing. Look things up ONLY to choose the reply.
            cur.execute(
                "SELECT 1 FROM events WHERE event_id = %s AND status = 'cancelled'",
                (event_id,),
            )
            if cur.fetchone() is not None:
                return "event_cancelled", None
            cur.execute(
                "SELECT status, customer_id, booking_ref FROM seats WHERE event_id = %s AND seat_id = %s",
                (event_id, seat_id),
            )
            row = cur.fetchone()
            if row is None:
                return "not_found", None
            status, holder, ref = row
            if status == "held" and holder == customer_id:
                return "already_yours", ref
            return "taken", None
    finally:
        conn.close()


EVENT_STATUSES = ("scheduled", "cancelled")


def apply_event_changed(message_id, payload):
    """Apply one Catalogue event.changed snapshot (contract sections 3 and 7).

    Everything happens in ONE transaction, including recording message_id, so
    a database error rolls it all back and the caller's exception makes the
    shared consumer retry. Returns 'duplicate', 'stale' or 'applied'.
    """
    event_id = payload["event_id"]
    version = payload["event_version"]
    status = payload["status"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"event_version must be a positive integer, got {version!r}")
    if status not in EVENT_STATUSES:
        raise ValueError(f"Unknown event status {status!r}")

    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            # Recording the ID first also makes a concurrent duplicate wait here
            # until this transaction ends, then do nothing.
            cur.execute(
                "INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (message_id,),
            )
            if cur.rowcount == 0:
                return "duplicate"

            # Only a newer event_version changes anything. Equal or older: the
            # message_id stays recorded and nothing else changes.
            cur.execute(
                """INSERT INTO events (event_id, event_version, status, title, starts_at, timezone)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (event_id) DO UPDATE
                   SET event_version = EXCLUDED.event_version, status = EXCLUDED.status,
                       title = EXCLUDED.title, starts_at = EXCLUDED.starts_at,
                       timezone = EXCLUDED.timezone
                   WHERE events.event_version < EXCLUDED.event_version""",
                (event_id, version, status, payload["title"], payload["starts_at"], payload["timezone"]),
            )
            if cur.rowcount == 0:
                return "stale"

            if status == "scheduled":
                # Create missing seats as free. On existing seats only fill in a
                # missing label or section; status, booking_ref, customer_id and
                # held_until are never touched.
                for seat in payload.get("seats") or []:
                    cur.execute(
                        """INSERT INTO seats (event_id, seat_id, section, label)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (event_id, seat_id) DO UPDATE
                           SET label = COALESCE(seats.label, EXCLUDED.label),
                               section = COALESCE(NULLIF(seats.section, ''), EXCLUDED.section)
                           WHERE seats.label IS NULL OR seats.section = ''""",
                        (event_id, seat["seat_id"], seat["section"], seat.get("label")),
                    )
            # 'cancelled' is stored above and creates no seats. Releasing held
            # seats on cancellation (contract row 13) is a later step.
            return "applied"
    finally:
        conn.close()
