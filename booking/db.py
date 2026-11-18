"""All Postgres connection code for the Booking service."""
import hashlib
import threading
import time
import uuid
from pathlib import Path

import psycopg2

import config

RETRIES = 10          # how many times to try connecting
WAIT_SECONDS = 2      # pause between tries

AVAILABILITY_CHANGED = "seat.availability-changed"
PRODUCER = "booking"


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


# --- Locks (lock order: event row first, then seat rows) ---------------------

def _lock_event_row(cur, event_id):
    """Lock the event's row until the transaction ends. True if the row exists.

    This is the per-event "sequence lock" of contract 4a: every path that
    changes seats of an event with a record takes it BEFORE touching seats.
    """
    cur.execute("SELECT 1 FROM events WHERE event_id = %s FOR UPDATE", (event_id,))
    return cur.fetchone() is not None


def _lock_event_creation(cur, event_id):
    """Per-event lock that only matters while an event has no record yet.

    A row lock cannot be taken on a row that does not exist yet, so a hold on a
    seat without an event record and the first event.changed for that event
    could otherwise run side by side: the hold would write no snapshot and the
    first snapshot would still list the seat as free. Both paths take this
    lock, so one of them waits for the other. It is released at commit.
    """
    digest = hashlib.sha256(f"booking-event:{event_id}".encode()).digest()
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (int.from_bytes(digest[:8], "big", signed=True),))


def write_outbox(cur, message_type, payload):
    """Write one outgoing message as an outbox row (contract section 3).

    Runs inside the caller's transaction and never commits: the row is saved
    only if the caller's state change is saved too. The complete envelope is
    stored once and never changed. Returns the envelope's message_id.
    """
    from messaging import Envelope  # imported here, like events.py does

    envelope = Envelope.new(message_type, payload, producer=PRODUCER)
    cur.execute(
        "INSERT INTO outbox (message_id, type, envelope) VALUES (%s, %s, %s)",
        (envelope.message_id, envelope.type, envelope.to_json().decode()),
    )
    return envelope.message_id


def write_availability_snapshot(cur, event_id):
    """Write one seat.availability-changed outbox row (contract section 11).

    Must run inside the caller's transaction, after the event row is locked
    (the UPDATE below would take the lock too). Increments the sequence, reads
    the free seats AFTER the lock, and stores the complete envelope. Returns
    the new sequence.
    """
    cur.execute(
        "UPDATE events SET sequence = sequence + 1 WHERE event_id = %s RETURNING sequence",
        (event_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"No event record for {event_id!r}; no snapshot can be written")
    sequence = row[0]
    cur.execute("SELECT seat_id FROM seats WHERE event_id = %s AND status = 'free'", (event_id,))
    free = sorted(seat_id for (seat_id,) in cur.fetchall())
    write_outbox(
        cur,
        AVAILABILITY_CHANGED,
        {
            "event_id": event_id,
            "sequence": sequence,
            "seats_remaining": len(free),
            "available_seat_ids": free,
        },
    )
    return sequence


# --- Holds --------------------------------------------------------------------

def hold_seat(event_id, seat_id, customer_id, customer_email=None, customer_name=None):
    """Try to hold a seat. Returns (outcome, booking_id, booking_status).

    outcome is one of: 'held', 'already_yours', 'taken', 'not_found',
    'event_cancelled'. booking_id (= the seat's booking_ref) and booking_status
    are None unless outcome is 'held' or 'already_yours'.

    A successful hold also creates the booking (contract section 4, row 1), in
    the same transaction as the seat update and the snapshot. customer_email
    and customer_name are optional; if absent, '<customer_id>@example.test' and
    customer_id are stored (section 5).
    """
    new_ref = str(uuid.uuid4())
    if customer_email is None:
        customer_email = f"{customer_id}@example.test"
    if customer_name is None:
        customer_name = customer_id
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            # Lock order: the event row first (if the event has a record), then
            # the seat. The lock only orders writers; it decides nothing.
            has_record = _lock_event_row(cur, event_id)
            if not has_record:
                # No record (yet): wait for a first event.changed that may be
                # creating it right now, then look again.
                _lock_event_creation(cur, event_id)
                has_record = _lock_event_row(cur, event_id)

            # The ONE conditional update: it only succeeds if the seat is free right now
            # and its event is not cancelled. A seat with no event record can still be held.
            # held_until uses the database clock (contract 4a), not this machine's.
            cur.execute(
                """UPDATE seats
                   SET status = 'held', booking_ref = %s, customer_id = %s,
                       held_until = now() + make_interval(mins => %s)
                   WHERE event_id = %s AND seat_id = %s AND status = 'free'
                     AND NOT EXISTS (SELECT 1 FROM events
                                     WHERE events.event_id = seats.event_id
                                       AND events.status = 'cancelled')
                   RETURNING held_until""",
                (new_ref, customer_id, config.HOLD_MINUTES, event_id, seat_id),
            )
            won = cur.fetchone()
            if won is not None:
                # Contract 4a row 1: the booking, then the snapshot, in the same
                # transaction as the seat update. If either fails, the whole hold
                # is rolled back (seat free, no booking, no outbox row).
                # The price is copied from config now and never recalculated (section 8).
                cur.execute(
                    """INSERT INTO bookings (booking_id, event_id, seat_id, customer_id,
                                             customer_email, customer_name, price_minor,
                                             currency, status, held_until)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'held', %s)""",
                    (new_ref, event_id, seat_id, customer_id, customer_email, customer_name,
                     config.BOOKING_PRICE_MINOR, config.BOOKING_CURRENCY, won[0]),
                )
                if has_record:
                    write_availability_snapshot(cur, event_id)
                return "held", new_ref, "held"

            # The update changed nothing. Look things up ONLY to choose the reply.
            cur.execute(
                "SELECT 1 FROM events WHERE event_id = %s AND status = 'cancelled'",
                (event_id,),
            )
            if cur.fetchone() is not None:
                return "event_cancelled", None, None
            cur.execute(
                """SELECT seats.status, seats.customer_id, seats.booking_ref, bookings.status
                   FROM seats LEFT JOIN bookings ON bookings.booking_id = seats.booking_ref
                   WHERE seats.event_id = %s AND seats.seat_id = %s""",
                (event_id, seat_id),
            )
            row = cur.fetchone()
            if row is None:
                return "not_found", None, None
            status, holder, ref, booking_status = row
            # A retry returns the existing booking. A held seat always has one
            # (the startup upgrade rule frees any that do not); if one ever
            # does not, it is treated as taken rather than replying without a
            # booking.
            if status == "held" and holder == customer_id and booking_status is not None:
                return "already_yours", ref, booking_status
            return "taken", None, None
    finally:
        conn.close()


def get_booking(booking_id, customer_id):
    """One booking, only if it belongs to customer_id (contract section 6).

    Returns a dict, or None for an unknown booking AND for a customer_id that
    does not match, so the caller cannot tell the two apart.
    """
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT booking_id, event_id, seat_id, status, held_until, payment_id
                   FROM bookings WHERE booking_id = %s AND customer_id = %s""",
                (booking_id, customer_id),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    booking_id, event_id, seat_id, status, held_until, payment_id = row
    return {
        "booking_id": booking_id,
        "event_id": event_id,
        "seat_id": seat_id,
        "booking_status": status,
        "held_until": held_until.isoformat(),   # TIMESTAMPTZ: offset-aware
        "payment_id": payment_id,
    }


# --- event.changed ------------------------------------------------------------

EVENT_STATUSES = ("scheduled", "cancelled")


def apply_event_changed(message_id, payload):
    """Apply one Catalogue event.changed snapshot (contract sections 3 and 7).

    Everything happens in ONE transaction, including recording message_id and
    any availability outbox row, so a database error rolls it all back and the
    caller's exception makes the shared consumer retry. Returns 'duplicate',
    'stale' or 'applied'.
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

            # Lock order, as in hold_seat: event first, seats after.
            _lock_event_creation(cur, event_id)
            existed = _lock_event_row(cur, event_id)

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

            created_seats = False
            if status == "scheduled":
                cur.execute("SELECT seat_id FROM seats WHERE event_id = %s", (event_id,))
                known = {seat_id for (seat_id,) in cur.fetchall()}
                # Create missing seats as free. On existing seats only fill in a
                # missing label or section; status, booking_ref, customer_id and
                # held_until are never touched.
                for seat in payload.get("seats") or []:
                    if seat["seat_id"] not in known:
                        created_seats = True
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

            # Contract section 11: one snapshot when the record is first created
            # (also for a cancelled-first event: 0 seats), and whenever new free
            # seats appeared.
            if not existed or created_seats:
                write_availability_snapshot(cur, event_id)
            return "applied"
    finally:
        conn.close()


# --- Startup: upgrade rule and backfill ---------------------------------------

# A held seat whose booking_ref has no booking row (made by the hold-only code).
_HELD_WITHOUT_BOOKING = """status = 'held' AND NOT EXISTS
    (SELECT 1 FROM bookings WHERE bookings.booking_id = seats.booking_ref)"""


def free_holds_without_booking():
    """Contract section 4b: set every held seat with no booking row back to free.

    Runs on startup, before backfill_availability. One transaction per event,
    in the hold's lock order (creation lock if the event has no record, then the
    event row, then its seats). Each event with a record that had seats freed
    gets ONE availability snapshot in the same transaction; an event without a
    record gets none (section 11). A second run frees nothing.
    Returns (seats freed, snapshots written).
    """
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT event_id FROM seats WHERE {_HELD_WITHOUT_BOOKING} ORDER BY event_id")
            event_ids = [event_id for (event_id,) in cur.fetchall()]
        freed = snapshots = 0
        for event_id in event_ids:
            with conn, conn.cursor() as cur:
                has_record = _lock_event_row(cur, event_id)
                if not has_record:
                    _lock_event_creation(cur, event_id)
                    has_record = _lock_event_row(cur, event_id)
                # Re-checked under the lock, so only seats still in that state change.
                cur.execute(
                    f"""UPDATE seats
                        SET status = 'free', booking_ref = NULL, customer_id = NULL, held_until = NULL
                        WHERE event_id = %s AND {_HELD_WITHOUT_BOOKING}""",
                    (event_id,),
                )
                count = cur.rowcount
                if count and has_record:
                    write_availability_snapshot(cur, event_id)
                    snapshots += 1
                freed += count
        return freed, snapshots
    finally:
        conn.close()


def backfill_availability():
    """Give every event that has never had a snapshot (sequence 0) exactly one.

    One transaction per event. The row is re-checked under its lock, so a
    second run (or a second start) writes nothing. Returns how many were written.
    """
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT event_id FROM events WHERE sequence = 0 ORDER BY event_id")
            event_ids = [event_id for (event_id,) in cur.fetchall()]
        written = 0
        for event_id in event_ids:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM events WHERE event_id = %s AND sequence = 0 FOR UPDATE",
                    (event_id,),
                )
                if cur.fetchone() is not None:
                    write_availability_snapshot(cur, event_id)
                    written += 1
        return written
    finally:
        conn.close()


# --- Outbox relay queries (used by outbox.py) -----------------------------------
#
# The relay polls about 4 times a second. Opening a new connection for every
# query made ~8 new Postgres connections a second even when idle, so the relay's
# queries below share ONE connection, kept open between polls. Request paths
# (holds, lookup) still use connect_once() per request.
# - Same limits as connect_once() (it opens the connection): connect_timeout 2 s,
#   statement_timeout 2 s.
# - Each call is its own transaction and commits its own work ('with conn').
# - On any error the connection is closed; the next call opens a new one.
# - The lock makes sure two threads never use the connection at the same time.

_relay_lock = threading.Lock()
_relay_conn = None

# The longest error text stored on a set-aside outbox row (contract section 3).
OUTBOX_ERROR_MAX = 500

# Pending = not yet published and not set aside (contract section 3).
_PENDING = "published_at IS NULL AND failed_at IS NULL"


def _relay_query(work):
    """Run work(cur) on the relay's shared connection, in one committed transaction."""
    global _relay_conn
    with _relay_lock:
        if _relay_conn is None or _relay_conn.closed:
            _relay_conn = connect_once()
        try:
            with _relay_conn, _relay_conn.cursor() as cur:   # commits on success, rolls back on error
                return work(cur)
        except Exception:
            # The connection may be broken (database restarted, network lost).
            # Close it; the next call opens a fresh one.
            _close_relay_conn()
            raise


def _close_relay_conn():
    """Close the shared connection, ignoring errors. Caller holds _relay_lock."""
    global _relay_conn
    if _relay_conn is not None:
        try:
            _relay_conn.close()
        except Exception:  # noqa: BLE001 - it may already be broken
            pass
        _relay_conn = None


def close_relay_connection():
    """Close the relay's shared connection (on shutdown). Safe to call any time."""
    with _relay_lock:
        _close_relay_conn()


def pending_outbox(limit=50):
    """Pending outbox rows, oldest first: [(message_id, type, envelope_text), ...]."""
    def work(cur):
        cur.execute(
            f"""SELECT message_id, type, envelope FROM outbox
                WHERE {_PENDING}
                ORDER BY created_at, message_id
                LIMIT %s""",
            (limit,),
        )
        return cur.fetchall()
    return _relay_query(work)


def mark_published(message_id):
    """Called only after the broker confirmed the message."""
    def work(cur):
        cur.execute(
            "UPDATE outbox SET published_at = clock_timestamp() "
            "WHERE message_id = %s AND published_at IS NULL",
            (message_id,),
        )
    _relay_query(work)


def set_aside_outbox(message_id, error):
    """Contract section 3: a permanent publish failure. The relay will not retry the row.

    One UPDATE, only if the row is still unpublished and not already set aside.
    `error` is cut to OUTBOX_ERROR_MAX characters. True if the row was set aside.
    """
    def work(cur):
        cur.execute(
            "UPDATE outbox SET failed_at = clock_timestamp(), error = %s "
            "WHERE message_id = %s AND published_at IS NULL AND failed_at IS NULL",
            (error[:OUTBOX_ERROR_MAX], message_id),
        )
        return cur.rowcount == 1
    return _relay_query(work)


def count_pending_outbox():
    """Rows the relay will still try to publish (set-aside rows do not count)."""
    def work(cur):
        cur.execute(f"SELECT count(*) FROM outbox WHERE {_PENDING}")
        return cur.fetchone()[0]
    return _relay_query(work)


def count_failed_outbox():
    """Rows set aside after a permanent publish failure."""
    def work(cur):
        cur.execute("SELECT count(*) FROM outbox WHERE failed_at IS NOT NULL")
        return cur.fetchone()[0]
    return _relay_query(work)
