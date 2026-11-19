"""Consumes Payment's messages from booking.payment-updates (contract sections 3, 4 and 9).

- payment.result   -> rows 5, 6 or 7: the SAME function as Payment's HTTP reply
                      (db.apply_payment_answer); whichever arrives second does nothing.
- refund.completed -> row 10: refund_required or refund_pending -> refunded.
- refund.failed    -> row 11: refund_required -> refund_pending.

Dispatch is by envelope type only, never by the payload's status field
(refund.failed carries status "paid"). Each message_id is recorded in the same
transaction as its effect (db.py), so a repeated delivery does nothing.

Errors (contract section 3, as for event.changed):
- a malformed message (not a dict, missing booking_id, unknown status, bad
  payment_id) raises ValueError BEFORE any database work, so shared
  run_consumer retries it and then parks it in booking.payment-updates.dlq;
- a database error also raises (everything rolled back, message retried);
- a booking_id Booking does not know is logged, counted and acknowledged.
"""
import asyncio
import logging

import db
import metrics

log = logging.getLogger("booking.payment_updates")

QUEUE = "booking.payment-updates"
PAYMENT_RESULT = "payment.result"
ID_MAX = 64     # same limit as Payment's booking_id / payment_id fields


def _text_id(payload, field):
    """A required non-empty string field of at most ID_MAX characters."""
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > ID_MAX:
        raise ValueError(f"{field} must be a non-empty string of at most {ID_MAX} characters, got {value!r}")
    return value


def _payload(envelope):
    payload = envelope.payload
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be an object, got {type(payload).__name__}")
    return payload


async def handle(envelope):
    """Dispatch by envelope type (contract section 3)."""
    message_type = envelope.type
    if message_type == PAYMENT_RESULT:
        payload = _payload(envelope)
        booking_id = _text_id(payload, "booking_id")
        status = payload.get("status")
        if status not in db.PAYMENT_STATUSES:
            raise ValueError(f"Unknown payment status {status!r}")
        payment_id = _text_id(payload, "payment_id")
        # Sync psycopg2 code runs in a thread so the event loop keeps serving HTTP.
        result = await asyncio.to_thread(db.apply_payment_answer, booking_id, status, payment_id,
                                         envelope.message_id)
        detail = f"{status} (payment {payment_id})"
    elif message_type in db.REFUND_TRANSITIONS:
        booking_id = _text_id(_payload(envelope), "booking_id")
        result = await asyncio.to_thread(db.apply_refund_message, envelope.message_id, booking_id,
                                         message_type)
        detail = ""
    else:
        # Only the three types above are bound to this queue; ack anything else.
        log.warning("Ignoring %s message %s on %s", message_type, envelope.message_id, QUEUE)
        return

    metrics.PAYMENT_MESSAGES.labels(message_type, result).inc()
    # unknown_booking, and a noop for a paid result (the booking was no longer
    # payment_pending; late success, row 9, is not built yet) are worth a warning.
    noteworthy = result == "unknown_booking" or (result == "noop" and detail.startswith("paid"))
    log.log(logging.WARNING if noteworthy else logging.INFO,
            "%s %s for booking %s %s-> %s", message_type, envelope.message_id, booking_id,
            detail + " " if detail else "", result)
