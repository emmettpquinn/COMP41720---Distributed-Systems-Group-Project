"""Email wording. Plain text keeps it readable in MailHog."""

from __future__ import annotations

from .db import Ticket

CANCEL_REASONS = {
    "hold_expired": "Your seat hold expired before payment completed, so the seat was released.",
    "payment_failed": "Your payment did not go through, so the seat was released.",
    "payments_unavailable": "Payments were unavailable, so the seat was released. Please try again.",
    "event_cancelled": "The event has been cancelled by the organiser.",
    "refund_required": (
        "Your payment arrived after your seat hold had expired and the seat had been "
        "sold to someone else. A full refund is on its way. You do not need to do anything."
    ),
}


def _greeting(name: str | None) -> str:
    return f"Hi {name}," if name else "Hi,"


def _event_line(t: Ticket) -> str:
    parts = [p for p in (t.event_title, t.event_starts_at) if p]
    return " - ".join(parts) if parts else "your event"


def ticket_issued(t: Ticket) -> tuple[str, str]:
    subject = f"Your e-ticket for {t.event_title or 'your event'}"
    body = (
        f"{_greeting(t.customer_name)}\n\n"
        f"Your booking is confirmed. Here is your e-ticket.\n\n"
        f"  Event:       {_event_line(t)}\n"
        f"  Seat:        {t.seat_label or '-'}\n"
        f"  Booking:     {t.booking_id}\n"
        f"  Ticket code: {t.ticket_code}\n\n"
        f"Show the ticket code at the door.\n"
    )
    return subject, body


def booking_cancelled(t: Ticket, reason: str | None) -> tuple[str, str]:
    if reason == "refund_required":
        subject = "Your booking could not be completed: refund on its way"
    else:
        subject = f"Booking {t.booking_id} cancelled"
    explanation = CANCEL_REASONS.get(reason or "", "Your booking has been cancelled.")
    voided = "\nAny ticket issued for this booking is no longer valid.\n" if t.ticket_code else ""
    body = (
        f"{_greeting(t.customer_name)}\n\n{explanation}\n"
        f"{voided}\n  Booking: {t.booking_id}\n  Event:   {_event_line(t)}\n"
    )
    return subject, body


def refund_completed(t: Ticket, amount: str | None) -> tuple[str, str]:
    subject = f"Refund completed for booking {t.booking_id}"
    amount_line = f"  Amount:  {amount}\n" if amount else ""
    body = (
        f"{_greeting(t.customer_name)}\n\n"
        f"Your refund has been completed. It may take a few days to appear on your statement.\n"
        f"Any ticket for this booking is no longer valid.\n\n"
        f"  Booking: {t.booking_id}\n{amount_line}"
    )
    return subject, body


def refund_delayed(t: Ticket) -> tuple[str, str]:
    subject = f"Your refund for booking {t.booking_id} is delayed"
    body = (
        f"{_greeting(t.customer_name)}\n\n"
        f"We could not complete your refund automatically. Our team has been alerted and "
        f"will process it by hand. You do not need to do anything.\n\n"
        f"  Booking: {t.booking_id}\n"
    )
    return subject, body
