"""PostgreSQL ledger and outbox. Provider delays run outside transactions."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from . import provider


class Conflict(ValueError):
    pass


def connect(db_url: str):
    return psycopg.connect(db_url, autocommit=True, row_factory=dict_row,
                           connect_timeout=5, application_name="stagedoor-payment")


def setup_db(db_url: str) -> None:
    with connect(db_url) as db, db.transaction():
        # Serialise schema setup if two processes start at the same time.
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                   ("payment-schema:" + db.execute("SELECT current_schema() AS name").fetchone()["name"],))
        db.execute(Path(__file__).with_name("schema.sql").read_text())


def lock(db, key: str) -> None:
    # Session lock: only matching operations wait; no long-running transaction.
    # Closing this dedicated connection releases all its locks, even on failure.
    db.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))


def format_payment(row) -> dict:
    # Keep the HTTP response unchanged; these two new columns are ledger details.
    keys = ("payment_id", "booking_id", "amount", "currency", "status", "refund_id", "created_at")
    result = {key: row[key] for key in keys}
    result["created_at"] = result["created_at"].isoformat()
    return result


def save_event(db, name: str, row, email: str | None = None) -> None:
    # HTTP uses cents; existing Notification emails expect decimal money strings.
    payload = {
        "booking_id": row["booking_id"], "payment_id": row["payment_id"],
        "amount": f"{row['amount'] // 100}.{row['amount'] % 100:02d}",
        "currency": row["currency"], "status": row["status"],
    }
    if row["refund_id"]:
        payload["refund_id"] = row["refund_id"]
    if email is None:
        email = row["customer_email"]
    if email:
        payload["customer_email"] = email
    if name == "refund.failed":
        payload["reason"] = "provider_rejected"
    msg = {
        "message_id": str(uuid.uuid4()), "type": name,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "version": 1, "producer": "payment", "payload": payload,
    }
    db.execute("INSERT INTO outbox (message_id, body) VALUES (%s, %s)",
               (msg["message_id"], json.dumps(msg)))


def charge(db_url: str, booking_id: str, amount: int, currency: str,
           mode: str = "ok", delay: float = 0, error_rate: float = 0,
           email: str | None = None) -> dict:
    if not booking_id.strip() or amount <= 0:
        raise ValueError("A booking ID and positive amount are required")
    with connect(db_url) as db:
        lock(db, "charge:" + booking_id)
        row = db.execute("SELECT * FROM payments WHERE booking_id = %s", (booking_id,)).fetchone()
        if row:
            if row["amount"] != amount or row["currency"] != currency:
                raise Conflict("Booking ID already used with another amount or currency")
            return format_payment(row)

        # The simulator has no external financial side effects. Its delay does
        # not keep a transaction open, and unrelated bookings can run in parallel.
        status = provider.pay(mode, delay, error_rate)
        payment_id = str(uuid.uuid4())
        with db.transaction():
            row = db.execute(
                "INSERT INTO payments (payment_id, booking_id, amount, currency, status, customer_email) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (payment_id, booking_id, amount, currency, status, email),
            ).fetchone()
            save_event(db, "payment.result", row)
        return format_payment(row)


def get_payment(db_url: str, payment_id: str) -> dict:
    with connect(db_url) as db:
        row = db.execute("SELECT * FROM payments WHERE payment_id = %s", (payment_id,)).fetchone()
        if row is None:
            raise KeyError("Payment not found")
        return format_payment(row)


def refund(db_url: str, payment_id: str, mode: str = "ok", delay: float = 0,
           error_rate: float = 0, message_id: str | None = None,
           booking_id: str | None = None, email: str | None = None) -> dict:
    rejected = False
    with connect(db_url) as db:
        # Lock message IDs before payments, in the same order in every worker.
        if message_id:
            lock(db, "message:" + message_id)
        lock(db, "refund:" + payment_id)
        row = db.execute("SELECT * FROM payments WHERE payment_id = %s", (payment_id,)).fetchone()
        if row is None:
            raise KeyError("Payment not found")
        if booking_id is not None and booking_id != row["booking_id"]:
            raise Conflict("Payment does not belong to this booking")
        if message_id:
            old = db.execute("SELECT payment_id FROM processed_messages WHERE message_id = %s", (message_id,)).fetchone()
            if old:
                if old["payment_id"] != payment_id:
                    raise Conflict("Message ID already used for another payment")
                return format_payment(row)
        if row["status"] == "failed":
            raise Conflict("A failed payment cannot be refunded")

        if row["status"] != "refunded":
            try:
                provider.refund(mode, delay, error_rate)
            except provider.RefundRejected:
                rejected = True

        with db.transaction():
            if rejected:
                changed = db.execute(
                    "UPDATE payments SET refund_failed = TRUE "
                    "WHERE payment_id = %s AND NOT refund_failed RETURNING *", (payment_id,)
                ).fetchone()
                if changed:
                    row = changed
                    save_event(db, "refund.failed", row, email)
            elif row["status"] != "refunded":
                row = db.execute(
                    "UPDATE payments SET status = 'refunded', refund_id = %s "
                    "WHERE payment_id = %s RETURNING *", (str(uuid.uuid4()), payment_id)
                ).fetchone()
                save_event(db, "refund.completed", row, email)
            if message_id:
                db.execute("INSERT INTO processed_messages VALUES (%s, %s)", (message_id, payment_id))
        result = format_payment(row)
    if rejected and message_id is None:
        raise provider.RefundRejected("Provider rejected the refund; payment remains paid")
    return result


def pending_events(db_url: str) -> list[dict]:
    with connect(db_url) as db:
        return db.execute("SELECT * FROM outbox WHERE NOT sent ORDER BY id LIMIT 100").fetchall()


def mark_sent(db_url: str, message_id: str) -> None:
    with connect(db_url) as db:
        db.execute("UPDATE outbox SET sent = TRUE WHERE message_id = %s", (message_id,))


def get_totals(db_url: str) -> dict:
    with connect(db_url) as db:
        return {
            "payments": {row["status"]: row["count"] for row in db.execute(
                "SELECT status, COUNT(*) AS count FROM payments GROUP BY status")},
            "outbox": db.execute("SELECT COUNT(*) AS count FROM outbox WHERE NOT sent").fetchone()["count"],
        }
