import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import Notification, Ticket, utcnow
from app.mailer import MailDeliveryError
from messaging import Envelope, MessageType
from messaging.consumer import next_destination

BOOKING = "bk-1001"


def confirmed(booking_id=BOOKING, message_id=None):
    return Envelope.new(
        MessageType.BOOKING_CONFIRMED,
        {
            "booking_id": booking_id,
            "event_id": "ev-7",
            "event_title": "Quiet Strings at the Canal Hall",
            "event_starts_at": "2026-11-13T20:00:00+00:00",
            "seat_label": "B12",
            "customer_email": f"{booking_id}@example.com",
            "customer_name": "Aoife",
        },
        producer="booking",
        message_id=message_id,
    )


def cancelled(booking_id=BOOKING, reason="hold_expired"):
    return Envelope.new(
        MessageType.BOOKING_CANCELLED,
        {"booking_id": booking_id, "reason": reason, "customer_email": f"{booking_id}@example.com"},
        producer="booking",
    )


def refund_completed(booking_id=BOOKING):
    return Envelope.new(
        MessageType.REFUND_COMPLETED,
        {"booking_id": booking_id, "payment_id": "pay-1", "amount": "45.00", "currency": "EUR"},
        producer="payment",
    )


async def ticket(sm, booking_id=BOOKING):
    async with sm() as s:
        return (await s.execute(select(Ticket).where(Ticket.booking_id == booking_id))).scalar_one_or_none()


async def count(sm, model):
    async with sm() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_100_redeliveries_give_one_ticket_and_one_email(service, sessionmaker, mailer):
    msg = confirmed()
    for _ in range(100):
        await service.handle(msg)

    assert await count(sessionmaker, Ticket) == 1
    assert len(mailer.sent) == 1
    t = await ticket(sessionmaker)
    assert t.status == "valid" and t.ticket_code.startswith("TKT-")
    assert t.ticket_code in mailer.sent[0]["body"]


async def test_concurrent_redeliveries_give_one_ticket(service, sessionmaker, mailer):
    msg = confirmed()
    results = await asyncio.gather(*(service.handle(msg) for _ in range(20)), return_exceptions=True)
    # A worker that loses a race may raise, and the consumer would retry it.
    # Retry those here the way the consumer would.
    for r in results:
        if isinstance(r, Exception):
            await service.handle(msg)
    assert await count(sessionmaker, Ticket) == 1
    assert len(mailer.sent) == 1


async def test_same_booking_confirmed_twice_with_different_ids(service, sessionmaker, mailer):
    await service.handle(confirmed())
    await service.handle(confirmed())  # new message ID, same booking
    assert await count(sessionmaker, Ticket) == 1
    assert len(mailer.sent) == 1


async def test_mail_outage_then_redelivery_sends_exactly_one_email(service, sessionmaker, mailer):
    msg = confirmed()
    mailer.down = True
    for _ in range(4):  # first attempt plus three retries, all failing
        with pytest.raises(MailDeliveryError):
            await service.handle(msg)
    # The ticket was issued on the first attempt even though the email failed.
    assert (await ticket(sessionmaker)).status == "valid"
    assert mailer.sent == []

    mailer.down = False
    await service.handle(msg)  # replayed from the DLQ
    await service.handle(msg)  # and replayed again by mistake
    assert len(mailer.sent) == 1
    async with sessionmaker() as s:
        n = (await s.execute(select(Notification))).scalar_one()
    assert n.status == "sent" and n.attempts == 5


async def test_email_claimed_by_crashed_worker_is_recovered(service, sessionmaker, mailer):
    await service.handle(confirmed())
    mailer.sent.clear()
    # Simulate a worker that claimed the email and then died before finishing.
    async with sessionmaker() as s, s.begin():
        n = (await s.execute(select(Notification))).scalar_one()
        n.status, n.claimed_until = "sending", utcnow() - timedelta(seconds=1)

    await service.handle(confirmed(message_id=None))  # a duplicate does not steal a claim
    assert mailer.sent == []
    assert await service.release_stale_claims() == [BOOKING]
    assert len(mailer.sent) == 1


async def test_cancellation_before_confirmation_never_issues_ticket(service, sessionmaker, mailer):
    await service.handle(cancelled(reason="payment_failed"))
    await service.handle(confirmed())  # arrives late

    t = await ticket(sessionmaker)
    assert t.status == "void" and t.ticket_code is None
    kinds = [m["key"].split(":")[1] for m in mailer.sent]
    assert kinds == ["booking_cancelled"]


async def test_cancellation_voids_issued_ticket(service, sessionmaker, mailer):
    await service.handle(confirmed())
    await service.handle(cancelled(reason="event_cancelled"))
    t = await ticket(sessionmaker)
    assert t.status == "void" and t.void_reason == "event_cancelled"
    assert len(mailer.sent) == 2


async def test_refund_voids_ticket_and_uses_stored_email(service, sessionmaker, mailer):
    await service.handle(confirmed())
    refund = refund_completed()  # no customer_email in the payload
    await service.handle(refund)
    await service.handle(refund)  # duplicate refund.completed

    t = await ticket(sessionmaker)
    assert t.status == "void" and t.void_reason == "refunded"
    refund_mails = [m for m in mailer.sent if m["key"].endswith("refund_completed")]
    assert len(refund_mails) == 1 and refund_mails[0]["to"] == f"{BOOKING}@example.com"
    assert "45.00 EUR" in refund_mails[0]["body"]


async def test_late_success_refund_path(service, sessionmaker, mailer):
    """ADR-001: the seat was resold, so Booking cancels with refund_required and Payment refunds."""
    await service.handle(cancelled(reason="refund_required"))
    await service.handle(refund_completed())
    assert (await ticket(sessionmaker)).status == "void"
    subjects = [m["subject"] for m in mailer.sent]
    assert "refund on its way" in subjects[0]
    assert subjects[1].startswith("Refund completed")


async def test_refund_failed_keeps_ticket_void_and_tells_customer(service, sessionmaker, mailer):
    await service.handle(confirmed())
    await service.handle(
        Envelope.new(MessageType.REFUND_FAILED, {"booking_id": BOOKING, "payment_id": "pay-1"}, "payment")
    )
    assert (await ticket(sessionmaker)).status == "void"
    assert mailer.sent[-1]["subject"].endswith("is delayed")


async def test_message_without_booking_id_is_rejected(service):
    with pytest.raises(ValueError):
        await service.handle(Envelope.new(MessageType.BOOKING_CONFIRMED, {}, "booking"))


async def test_other_message_types_are_ignored(service, sessionmaker, mailer):
    await service.handle(Envelope.new(MessageType.EVENT_CHANGED, {"event_id": "e"}, "catalogue"))
    assert await count(sessionmaker, Ticket) == 0 and mailer.sent == []


def test_retry_routing_backs_off_then_parks():
    q = "ticketing.booking-updates"
    assert next_destination(q, 0) == f"{q}.retry.1"
    assert next_destination(q, 1) == f"{q}.retry.2"
    assert next_destination(q, 2) == f"{q}.retry.3"
    assert next_destination(q, 3) == f"{q}.dlq"


def test_envelope_round_trip():
    env = confirmed()
    assert Envelope.from_json(env.to_json()) == env
