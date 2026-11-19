"""All Postgres connection code for the Booking service."""
import hashlib
import threading
import time
import uuid
from pathlib import Path

import psycopg2

import config
import metrics

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


# --- Checkout and Payment's answer (contract sections 4, 6, 9 and 12) -----------

BOOKING_CONFIRMED = "booking.confirmed"
BOOKING_CANCELLED = "booking.cancelled"
REFUND_REQUESTED = "refund.requested"

# Payment's HTTP 200 answers (contract section 9). 'refunded' is handled like 'failed' (section 8).
PAYMENT_STATUSES = ("paid", "failed", "refunded")

# Length caps for every message Booking publishes (contract section 1).
EVENT_TITLE_MAX = 200
SEAT_LABEL_MAX = 64


def start_checkout(booking_id, customer_id, breaker_accepts):
    """Contract section 6 (rows 2 and 3). Returns the outcome:

    'not_found', 'repeat' (already payment_pending), 'event_cancelled',
    'not_checkoutable', 'breaker_open' (nothing written), or 'started'
    (now payment_pending and committed; only then may Payment be called).

    Locks ONLY the booking row (lock order, section 4a). The event's status is
    read without a lock; a cancellation that lands after this commit is caught
    when Payment's answer is applied (row 6).
    """
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT b.status, b.held_until > now(), e.status
                   FROM bookings b LEFT JOIN events e ON e.event_id = b.event_id
                   WHERE b.booking_id = %s AND b.customer_id = %s
                   FOR UPDATE OF b""",
                (booking_id, customer_id),
            )
            row = cur.fetchone()
            # The checks, in the contract's order (section 6).
            if row is None:
                return "not_found"
            status, not_expired, event_status = row
            if status == "payment_pending":
                return "repeat"
            if event_status == "cancelled":
                return "event_cancelled"
            if status != "held" or not not_expired or event_status is None:
                return "not_checkoutable"
            if not breaker_accepts:
                return "breaker_open"      # row 3: the booking stays held
            # Row 2: the ONE conditional update (section 4a).
            cur.execute(
                """UPDATE bookings
                   SET status = 'payment_pending', checkout_started_at = now(), updated_at = now()
                   WHERE booking_id = %s AND status = 'held' AND held_until > now()
                     AND EXISTS (SELECT 1 FROM events
                                 WHERE events.event_id = bookings.event_id
                                   AND events.status <> 'cancelled')""",
                (booking_id,),
            )
            if cur.rowcount != 1:
                # The booking row is locked and now() is fixed for the
                # transaction, so only a cancellation committed between the two
                # statements can get here.
                return "event_cancelled"
            return "started"
    finally:
        conn.close()


def payment_request(booking_id):
    """What the background call sends to Payment: (price_minor, currency, customer_email)."""
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT price_minor, currency, customer_email FROM bookings WHERE booking_id = %s",
                        (booking_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError(f"No booking {booking_id!r}")
    return row


def _record_message(cur, message_id):
    """Record a consumed message_id in the caller's transaction (contract section 3).

    True if it is new; False if it was already recorded (a repeat: do nothing).
    A second transaction inserting the same ID at the same moment waits here
    until the first one ends, then gets False.
    """
    cur.execute(
        "INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (message_id,),
    )
    return cur.rowcount == 1


LATE_SUCCESS = "late_success"


def apply_payment_answer(booking_id, payment_status, payment_id, message_id=None):
    """Apply Payment's answer: row 5, 6, 7 or 9 (contract section 4). See _apply_payment_answer."""
    result = _apply_payment_answer(booking_id, payment_status, payment_id, message_id)
    # Row 9, counted only after its transaction committed.
    if result == LATE_SUCCESS:
        metrics.LATE_SUCCESSES.inc()
    return result


def _apply_payment_answer(booking_id, payment_status, payment_id, message_id=None):
    """Apply Payment's answer to a payment_pending booking: row 5, 6 or 7 (contract section 4).

    Two callers, same rows (contract section 9): the HTTP reply (message_id
    None) and a payment.result message (message_id set). Whichever comes
    second finds the booking no longer payment_pending and does nothing.

    One transaction, lock order event row -> booking row -> seat row:
    - paid, event not cancelled -> confirmed, seat booked, booking.confirmed
    - paid, event cancelled -> refund_required, seat free, availability,
      booking.cancelled (event_cancelled), refund.requested
    - failed or refunded -> released, seat free, availability,
      booking.cancelled (payment_failed)
    payment_id is stored in every case. Returns 'confirmed', 'refund_required',
    'released', or 'noop' (the booking was no longer payment_pending: nothing
    written). Any error rolls back everything.

    Row 9 (late success): 'paid' for a booking that is 'released' (normally by
    the safety net, row 8) -> refund_required, payment_id stored, one
    refund.requested. The seat is NOT touched: it may already belong to someone
    else. Returns 'late_success'. 'failed'/'refunded' for a released booking,
    and 'paid' for any other state, stay 'noop'. The event row is locked here
    too (it is taken before the booking's state is known); it changes nothing.

    Message path only (message_id set): the message_id is recorded first, in
    the same transaction, also for a noop. Returns 'duplicate' if it was
    already recorded, and 'unknown_booking' (recorded, nothing else) for a
    booking_id Booking does not know. The HTTP path records nothing and still
    raises LookupError for an unknown booking.
    """
    if payment_status not in PAYMENT_STATUSES:
        raise ValueError(f"Unknown payment status {payment_status!r}")
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            if message_id is not None and not _record_message(cur, message_id):
                return "duplicate"
            cur.execute("SELECT event_id, seat_id FROM bookings WHERE booking_id = %s", (booking_id,))
            row = cur.fetchone()
            if row is None:
                if message_id is not None:
                    return "unknown_booking"     # commits the processed row only
                raise LookupError(f"No booking {booking_id!r}")
            event_id, seat_id = row

            # 1. The event row. Checkout refuses a booking without an event
            # record, so the record must exist (no creation lock is needed).
            if not _lock_event_row(cur, event_id):
                raise LookupError(f"No event record for {event_id!r}")
            cur.execute("SELECT status, title, starts_at FROM events WHERE event_id = %s", (event_id,))
            event_status, title, starts_at = cur.fetchone()

            if payment_status == "paid":
                new_status = "refund_required" if event_status == "cancelled" else "confirmed"
            else:
                new_status = "released"

            # 2. The booking row: the deciding conditional update (section 4a).
            cur.execute(
                """UPDATE bookings SET status = %s, payment_id = %s, updated_at = now()
                   WHERE booking_id = %s AND status = 'payment_pending'
                   RETURNING customer_email, customer_name""",
                (new_status, payment_id, booking_id),
            )
            won = cur.fetchone()
            if won is None:
                if payment_status == "paid":
                    return _late_success(cur, booking_id, payment_id)
                return "noop"
            customer_email, customer_name = won

            # 3. The seat row, keyed on the booking; exactly one must change.
            if new_status == "confirmed":
                cur.execute(
                    """UPDATE seats SET status = 'booked'
                       WHERE event_id = %s AND seat_id = %s AND booking_ref = %s AND status = 'held'
                       RETURNING label""",
                    (event_id, seat_id, booking_id),
                )
            else:
                cur.execute(
                    """UPDATE seats SET status = 'free', booking_ref = NULL, customer_id = NULL, held_until = NULL
                       WHERE event_id = %s AND seat_id = %s AND booking_ref = %s AND status = 'held'
                       RETURNING label""",
                    (event_id, seat_id, booking_id),
                )
            seat = cur.fetchone()
            if seat is None:
                raise LookupError(f"Seat {seat_id!r} is not held by booking {booking_id!r}")
            event_title = title[:EVENT_TITLE_MAX]

            # 4. The outbox rows, in the same transaction.
            if new_status == "confirmed":
                write_outbox(cur, BOOKING_CONFIRMED, {
                    "booking_id": booking_id,
                    "customer_email": customer_email,
                    "customer_name": customer_name,
                    "event_id": event_id,
                    "event_title": event_title,
                    "event_starts_at": starts_at,
                    "seat_label": (seat[0] or seat_id)[:SEAT_LABEL_MAX],   # no label: use the seat ID
                })
                return new_status

            write_availability_snapshot(cur, event_id)
            write_outbox(cur, BOOKING_CANCELLED, {
                "booking_id": booking_id,
                "reason": "event_cancelled" if new_status == "refund_required" else "payment_failed",
                "customer_email": customer_email,
                "customer_name": customer_name,
                "event_title": event_title,
            })
            if new_status == "refund_required":
                write_outbox(cur, REFUND_REQUESTED, {
                    "booking_id": booking_id,
                    "payment_id": payment_id,
                    "customer_email": customer_email,
                })
            return new_status
    finally:
        conn.close()


def _late_success(cur, booking_id, payment_id):
    """Row 9, inside _apply_payment_answer's transaction: released -> refund_required.

    ONE conditional UPDATE on the booking row (section 4a), then refund.requested
    in the same transaction. No seat change, no availability snapshot, no second
    booking.cancelled. Returns 'late_success', or 'noop' if the booking is not
    'released' (e.g. a second late 'paid' finds it already refund_required).
    """
    cur.execute(
        """UPDATE bookings SET status = 'refund_required', payment_id = %s, updated_at = now()
           WHERE booking_id = %s AND status = 'released'
           RETURNING customer_email""",
        (payment_id, booking_id),
    )
    row = cur.fetchone()
    if row is None:
        return "noop"
    write_outbox(cur, REFUND_REQUESTED, {
        "booking_id": booking_id,
        "payment_id": payment_id,
        "customer_email": row[0],
    })
    return LATE_SUCCESS


# --- Safety net (contract section 4 row 8, section 9) ---------------------------

PAYMENTS_UNAVAILABLE = "payments_unavailable"


def due_safety_net(seconds, limit=50):
    """booking_ids still payment_pending whose checkout_started_at + seconds has passed
    (database clock), oldest first.

    Polled every 0.5 s, so it runs on the background pollers' shared connection
    (_relay_query below) instead of opening a new connection each time.
    """
    def work(cur):
        cur.execute(
            """SELECT booking_id FROM bookings
               WHERE status = 'payment_pending'
                 AND checkout_started_at + make_interval(secs => %s) <= now()
               ORDER BY checkout_started_at, booking_id
               LIMIT %s""",
            (seconds, limit),
        )
        return [booking_id for (booking_id,) in cur.fetchall()]
    return _relay_query(work)


def release_payment_pending(booking_id, seconds):
    """Row 8: Payment gave the safety net no answer; release the booking.

    One transaction, lock order event row -> booking row -> seat row (section
    4a). No creation lock: checkout requires an event record (section 6).
    - booking payment_pending -> released, ONLY if still payment_pending and
      checkout_started_at + seconds has passed (database clock). Otherwise
      (a payment.result won meanwhile) nothing is written: returns 'noop'.
    - its seat held -> free (keyed on the booking; must change one row);
    - one availability snapshot;
    - booking.cancelled with reason payments_unavailable (section 12 fields).
    Returns 'payments_unavailable' or 'noop'. Any error rolls back everything.
    The Payment call happened before, outside any transaction.
    """
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT event_id, seat_id FROM bookings WHERE booking_id = %s", (booking_id,))
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"No booking {booking_id!r}")
            event_id, seat_id = row

            # 1. The event row.
            if not _lock_event_row(cur, event_id):
                raise LookupError(f"No event record for {event_id!r}")
            cur.execute("SELECT title FROM events WHERE event_id = %s", (event_id,))
            title = cur.fetchone()[0]

            # 2. The booking row: the deciding conditional update.
            cur.execute(
                """UPDATE bookings SET status = 'released', updated_at = now()
                   WHERE booking_id = %s AND status = 'payment_pending'
                     AND checkout_started_at + make_interval(secs => %s) <= now()
                   RETURNING customer_email, customer_name""",
                (booking_id, seconds),
            )
            won = cur.fetchone()
            if won is None:
                return "noop"
            customer_email, customer_name = won

            # 3. The seat row, keyed on the booking.
            cur.execute(
                """UPDATE seats SET status = 'free', booking_ref = NULL, customer_id = NULL, held_until = NULL
                   WHERE event_id = %s AND seat_id = %s AND booking_ref = %s AND status = 'held'""",
                (event_id, seat_id, booking_id),
            )
            if cur.rowcount != 1:
                raise LookupError(f"Seat {seat_id!r} is not held by booking {booking_id!r}")

            # 4. The outbox rows, in the same transaction.
            write_availability_snapshot(cur, event_id)
            write_outbox(cur, BOOKING_CANCELLED, {
                "booking_id": booking_id,
                "reason": PAYMENTS_UNAVAILABLE,
                "customer_email": customer_email,
                "customer_name": customer_name,
                "event_title": title[:EVENT_TITLE_MAX],
            })
            return PAYMENTS_UNAVAILABLE
    finally:
        conn.close()


# Contract section 4, rows 10 and 11: message type -> (new status, statuses it may come from).
REFUND_COMPLETED = "refund.completed"
REFUND_FAILED = "refund.failed"
REFUND_TRANSITIONS = {
    REFUND_COMPLETED: ("refunded", ("refund_required", "refund_pending")),   # row 10
    REFUND_FAILED: ("refund_pending", ("refund_required",)),                  # row 11
}


def apply_refund_message(message_id, booking_id, message_type):
    """Apply refund.completed (row 10) or refund.failed (row 11).

    One transaction: the processed message_id and ONE conditional UPDATE on
    the booking row (rows 10/11 change no seats, so only the booking row is
    locked; section 4a). No outbox row. Decided by message_type only, never by
    the payload's status (refund.failed carries status "paid").
    Returns 'refunded' or 'refund_pending' (changed), 'noop' (booking in
    another state: recorded only), 'unknown_booking' (recorded only) or
    'duplicate' (already recorded: nothing).
    """
    if message_type not in REFUND_TRANSITIONS:
        raise ValueError(f"Not a refund message type: {message_type!r}")
    new_status, from_statuses = REFUND_TRANSITIONS[message_type]
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            if not _record_message(cur, message_id):
                return "duplicate"
            cur.execute(
                """UPDATE bookings SET status = %s, updated_at = now()
                   WHERE booking_id = %s AND status = ANY(%s)""",
                (new_status, booking_id, list(from_statuses)),
            )
            if cur.rowcount == 1:
                return new_status
            # Nothing changed. Look up ONLY to choose the result for the log/metric.
            cur.execute("SELECT 1 FROM bookings WHERE booking_id = %s", (booking_id,))
            return "noop" if cur.fetchone() is not None else "unknown_booking"
    finally:
        conn.close()


# --- Releasing held bookings: hold expiry (row 4) and event cancelled (row 13) ---

def _release_held(cur, event_id, reason, title, expired_only):
    """Release held bookings of one event and free their seats (contract section 4, rows 4 and 13).

    Runs inside the caller's transaction, AFTER the caller took the event lock
    (lock order, section 4a: event row -> booking rows -> seat rows). Only
    bookings still 'held' are touched (and, for row 4, only those whose
    held_until has passed on the database clock), so a booking a checkout has
    just moved to payment_pending is never released. Writes one
    booking.cancelled per released booking; event_title is left out when the
    event has no record (title None). The caller writes the availability
    snapshot. Returns how many bookings were released (0 is fine).
    """
    expired = " AND held_until < now()" if expired_only else ""
    cur.execute(
        f"""UPDATE bookings SET status = 'released', updated_at = now()
            WHERE event_id = %s AND status = 'held'{expired}
            RETURNING booking_id, customer_email, customer_name""",
        (event_id,),
    )
    released = cur.fetchall()
    if not released:
        return 0
    booking_ids = [booking_id for booking_id, _, _ in released]
    # Seat updates are keyed on the booking (section 4a); every one must change.
    cur.execute(
        """UPDATE seats SET status = 'free', booking_ref = NULL, customer_id = NULL, held_until = NULL
           WHERE event_id = %s AND booking_ref = ANY(%s) AND status = 'held'""",
        (event_id, booking_ids),
    )
    if cur.rowcount != len(booking_ids):
        raise LookupError(f"Event {event_id!r}: {len(booking_ids)} booking(s) released but "
                          f"{cur.rowcount} seat(s) freed; nothing is changed")
    for booking_id, customer_email, customer_name in released:
        payload = {
            "booking_id": booking_id,
            "reason": reason,
            "customer_email": customer_email,
            "customer_name": customer_name,
        }
        if title is not None:
            payload["event_title"] = title[:EVENT_TITLE_MAX]
        write_outbox(cur, BOOKING_CANCELLED, payload)
    return len(released)


def expired_hold_events():
    """Event IDs that have at least one held booking whose held_until has passed (database clock)."""
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT event_id FROM bookings
                   WHERE status = 'held' AND held_until < now() ORDER BY event_id"""
            )
            return [event_id for (event_id,) in cur.fetchall()]
    finally:
        conn.close()


def release_expired_holds(event_id):
    """Row 4 for ONE event, in ONE transaction: release all its expired held bookings.

    Lock order as a hold takes it (section 4a): the event row, or, while the
    event has no record, the creation lock and then a second look. Then the
    booking rows and seats (_release_held), then ONE availability snapshot if
    the event has a record and anything was released. Any error rolls back
    this event only. Returns (bookings released, snapshot written).
    """
    conn = connect_once()
    try:
        with conn, conn.cursor() as cur:
            has_record = _lock_event_row(cur, event_id)
            if not has_record:
                _lock_event_creation(cur, event_id)
                has_record = _lock_event_row(cur, event_id)
            title = None
            if has_record:
                cur.execute("SELECT title FROM events WHERE event_id = %s", (event_id,))
                title = cur.fetchone()[0]
            released = _release_held(cur, event_id, "hold_expired", title, expired_only=True)
            snapshot = bool(released) and has_record
            if snapshot:
                write_availability_snapshot(cur, event_id)
    finally:
        conn.close()
    # Counted only after the commit above succeeded.
    if released:
        metrics.HOLDS_RELEASED.labels("hold_expired").inc(released)
    return released, snapshot


# --- event.changed ------------------------------------------------------------

EVENT_STATUSES = ("scheduled", "cancelled")


def apply_event_changed(message_id, payload):
    """Apply one Catalogue event.changed snapshot (contract sections 3 and 7).

    Everything happens in ONE transaction, including recording message_id and
    any availability outbox row, so a database error rolls it all back and the
    caller's exception makes the shared consumer retry. Returns 'duplicate',
    'stale' or 'applied'.
    """
    result, released = _apply_event_changed(message_id, payload)
    # Row 13's releases, counted only after the transaction committed.
    if released:
        metrics.HOLDS_RELEASED.labels("event_cancelled").inc(released)
    return result


def _apply_event_changed(message_id, payload):
    """apply_event_changed's transaction. Returns (result, held bookings released by row 13)."""
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
                return "duplicate", 0

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
                return "stale", 0

            created_seats = False
            released = 0
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
            else:
                # 'cancelled' is stored above and creates no seats. Contract row 13:
                # release every booking of this event that is still 'held' (any
                # number, including zero). payment_pending bookings are left to
                # Payment's answer (rows 5-7); confirmed ones stay (section 13.2).
                released = _release_held(cur, event_id, "event_cancelled", payload["title"],
                                         expired_only=False)

            # Contract section 11: ONE snapshot when the record is first created
            # (also for a cancelled-first event), whenever new free seats
            # appeared, and when row 13 freed seats. Written after the release,
            # so it lists the freed seats.
            if not existed or created_seats or released:
                write_availability_snapshot(cur, event_id)
            return "applied", released
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
# queries below share ONE connection, kept open between polls. The safety net's
# twice-a-second check (due_safety_net above) shares it too. Request paths
# (holds, lookup) and all writes of bookings still use connect_once().
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
