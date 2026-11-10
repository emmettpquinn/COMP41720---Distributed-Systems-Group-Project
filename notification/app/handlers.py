"""Ticketing's message handling: issue and void tickets, then send the emails.

Processing a message has two steps.

1. **State change.** This is one database transaction. It records the message
   ID in ``processed_messages``, changes the ticket, and records any email we
   now owe the customer as a ``pending`` notification. A redelivered message
   finds its ID already recorded and changes nothing.

2. **Delivery.** Pending notifications for the booking are sent over SMTP and
   marked ``sent``. If sending fails, the handler raises, and the shared
   consumer retries with backoff and finally parks the message in the DLQ.
   When the message is retried or replayed, step 1 sees a duplicate and does
   nothing, but step 2 still finds the email pending and sends it. That is why
   a MailHog outage ends in exactly one email per customer.

Order of messages: a ticket only ever moves from ``valid`` to ``void``, never
back. A cancellation that arrives before its confirmation leaves a void
tombstone, so the late confirmation cannot issue a ticket. This monotonic rule
does the job a version comparison would otherwise do.

Remaining duplicate window: if the process dies after SMTP accepted an email
but before step 2 commits, the redelivery sends that email a second time. SMTP
has no idempotency key, so this cannot be closed completely. It is a single
email, and the ``X-Notification-Key`` header lets the customer's mail client
or support staff see that both copies are the same email. ADR-005 records this.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from messaging import Envelope, MessageType

from . import emails, metrics
from .db import (
    DeliveryAttempt,
    Notification,
    NotificationStatus,
    ProcessedMessage,
    Ticket,
    TicketStatus,
    utcnow,
)
from .mailer import MailDeliveryError, Mailer

log = logging.getLogger(__name__)

HANDLED_TYPES = {
    MessageType.BOOKING_CONFIRMED,
    MessageType.BOOKING_CANCELLED,
    MessageType.REFUND_COMPLETED,
    MessageType.REFUND_FAILED,
}

# Far longer than the SMTP timeout, so a live worker never loses its claim.
CLAIM_LEASE = timedelta(seconds=60)

_CODE_ALPHABET ="ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I confusion


def new_ticket_code() -> str:
    return "TKT-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))


class InvalidMessage(ValueError):
    pass


class TicketingService:
    def __init__(self, sessionmaker: async_sessionmaker, mailer: Mailer) -> None:
        self.sessionmaker = sessionmaker
        self.mailer = mailer

    # ------------------------------------------------------------------ entry
    async def handle(self, env: Envelope) -> None:
        if env.type not in HANDLED_TYPES:
            log.info("Ignoring message type %s (%s)", env.type, env.message_id)
            return
        booking_id = env.payload.get("booking_id")
        if not booking_id:
            raise InvalidMessage(f"{env.type} {env.message_id} has no booking_id")
        booking_id = str(booking_id)

        duplicate = await self._apply(env, booking_id)
        if duplicate:
            metrics.DUPLICATES_IGNORED.labels(env.type).inc()
            log.info("Duplicate %s %s ignored", env.type, env.message_id)
        await self.deliver_pending(booking_id)

    # ----------------------------------------------------------- step 1: state
    async def _apply(self, env: Envelope, booking_id: str) -> bool:
        """Apply the message once. Returns True if it had already been applied."""
        try:
            async with self.sessionmaker() as session, session.begin():
                if await session.get(ProcessedMessage, env.message_id) is not None:
                    return True
                session.add(
                    ProcessedMessage(
                        message_id=env.message_id, message_type=env.type, booking_id=booking_id
                    )
                )
                # Flush first. If another worker is applying the same message,
                # this blocks on the primary key and then fails, and we report a
                # duplicate.
                await session.flush()

                ticket = (
                    await session.execute(
                        select(Ticket).where(Ticket.booking_id == booking_id).with_for_update()
                    )
                ).scalar_one_or_none()

                p = env.payload
                if env.type == MessageType.BOOKING_CONFIRMED:
                    await self._on_confirmed(session, env, booking_id, ticket, p)
                elif env.type == MessageType.BOOKING_CANCELLED:
                    await self._on_cancelled(session, env, booking_id, ticket, p)
                elif env.type == MessageType.REFUND_COMPLETED:
                    await self._on_refund_completed(session, env, booking_id, ticket, p)
                elif env.type == MessageType.REFUND_FAILED:
                    await self._on_refund_failed(session, env, booking_id, ticket, p)
            return False
        except IntegrityError:
            # The message ID raced with another worker: that is a duplicate.
            # Any other conflict (two different messages creating the same
            # booking's ticket at once) is re-raised. The retry then sees the
            # committed row and applies cleanly.
            async with self.sessionmaker() as session:
                if await session.get(ProcessedMessage, env.message_id) is not None:
                    return True
            raise

    async def _on_confirmed(self, s: AsyncSession, env: Envelope, booking_id: str, t: Ticket | None, p: dict) -> None:
        if t is not None:
            # Already valid: nothing to do. Already void: the booking was
            # cancelled or refunded first, so a ticket must never be issued.
            log.info("booking.confirmed for %s ignored: ticket already %s", booking_id, t.status)
            return
        t = Ticket(
            ticket_id=str(uuid.uuid4()),
            booking_id=booking_id,
            ticket_code=new_ticket_code(),
            status=TicketStatus.VALID,
            issued_at=utcnow(),
            **_customer_and_event(p),
        )
        s.add(t)
        metrics.TICKETS_ISSUED.inc()
        await self._owe(s, env, t, "ticket_issued", *emails.ticket_issued(t))

    async def _on_cancelled(self, s: AsyncSession, env: Envelope, booking_id: str, t: Ticket | None, p: dict) -> None:
        reason = p.get("reason")
        t = self._void(s, booking_id, t, p, reason or "cancelled")
        await self._owe(s, env, t, "booking_cancelled", *emails.booking_cancelled(t, reason))

    async def _on_refund_completed(self, s: AsyncSession, env: Envelope, booking_id: str, t: Ticket | None, p: dict) -> None:
        t = self._void(s, booking_id, t, p, "refunded")
        amount = p.get("amount")
        if amount is not None and p.get("currency"):
            amount = f"{amount} {p['currency']}"
        await self._owe(s, env, t, "refund_completed", *emails.refund_completed(t, amount and str(amount)))

    async def _on_refund_failed(self, s: AsyncSession, env: Envelope, booking_id: str, t: Ticket | None, p: dict) -> None:
        # The booking stays "refund pending" in Booking, and the customer must
        # not hold a usable ticket for a seat that was resold.
        t = self._void(s, booking_id, t, p, "refund_pending")
        await self._owe(s, env, t, "refund_delayed", *emails.refund_delayed(t))

    # ---------------------------------------------------------------- helpers
    def _void(self, s: AsyncSession, booking_id: str, t: Ticket | None, p: dict, reason: str) -> Ticket:
        if t is None:
            # Tombstone: cancelled or refunded before any ticket was issued.
            t = Ticket(
                ticket_id=str(uuid.uuid4()),
                booking_id=booking_id,
                ticket_code=None,
                status=TicketStatus.VOID,
                voided_at=utcnow(),
                void_reason=reason,
                **_customer_and_event(p),
            )
            s.add(t)
            return t
        if t.status == TicketStatus.VALID:
            t.status = TicketStatus.VOID
            t.voided_at = utcnow()
            t.void_reason = reason
            metrics.TICKETS_VOIDED.inc()
        # Fill in contact details we did not have yet (tombstone first, details later).
        for key, value in _customer_and_event(p).items():
            if value and not getattr(t, key):
                setattr(t, key, value)
        return t

    async def _owe(self, s: AsyncSession, env: Envelope, t: Ticket, kind: str, subject: str, body: str) -> None:
        exists = (
            await s.execute(
                select(Notification.id).where(
                    Notification.booking_id == t.booking_id, Notification.kind == kind
                )
            )
        ).first()
        if exists:
            return
        recipient = env.payload.get("customer_email") or t.customer_email
        s.add(
            Notification(
                booking_id=t.booking_id,
                kind=kind,
                recipient=recipient,
                subject=subject,
                body=body,
                status=NotificationStatus.PENDING if recipient else NotificationStatus.SKIPPED,
                last_error=None if recipient else "no recipient address in message or ticket",
                source_message_id=env.message_id,
            )
        )

    # -------------------------------------------------------- step 2: delivery
    async def deliver_pending(self, booking_id: str) -> int:
        """Send every pending email for a booking. Raises if any send failed.

        Each email is first *claimed* with a conditional update
        (``pending -> sending`` with a lease), so two workers handling copies of
        the same message can never both send it. No database transaction stays
        open during the SMTP call. If a worker dies while holding a claim, the
        lease expires and ``release_stale_claims`` returns the email to pending.
        """
        async with self.sessionmaker() as session:
            ids = (
                await session.execute(
                    select(Notification.id)
                    .where(
                        Notification.booking_id == booking_id,
                        Notification.status == NotificationStatus.PENDING,
                    )
                    .order_by(Notification.id)
                )
            ).scalars().all()

        failures: list[str] = []
        sent = 0
        for notification_id in ids:
            n = await self._claim(notification_id)
            if n is None:
                continue  # another worker has it, or it was sent meanwhile
            try:
                await self.mailer.send(n.recipient, n.subject, n.body, f"{n.booking_id}:{n.kind}")
            except MailDeliveryError as exc:
                await self._finish(n, ok=False, error=str(exc))
                metrics.EMAIL_ERRORS.labels(n.kind).inc()
                failures.append(f"{n.kind}: {exc}")
            else:
                await self._finish(n, ok=True)
                metrics.EMAILS_SENT.labels(n.kind).inc()
                sent += 1
        if failures:
            raise MailDeliveryError(f"{len(failures)} email(s) for booking {booking_id} not sent: {failures[0]}")
        return sent

    async def _claim(self, notification_id: int) -> Notification | None:
        async with self.sessionmaker() as session, session.begin():
            result = await session.execute(
                update(Notification)
                .where(
                    Notification.id == notification_id,
                    Notification.status == NotificationStatus.PENDING,
                )
                .values(
                    status=NotificationStatus.SENDING,
                    claimed_until=utcnow() + CLAIM_LEASE,
                    attempts=Notification.attempts + 1,
                )
            )
            if result.rowcount != 1:
                return None
            return await session.get(Notification, notification_id)

    async def _finish(self, n: Notification, ok: bool, error: str | None = None) -> None:
        async with self.sessionmaker() as session, session.begin():
            values = (
                {"status": NotificationStatus.SENT, "sent_at": utcnow(), "last_error": None}
                if ok
                else {"status": NotificationStatus.PENDING, "last_error": error}
            )
            await session.execute(
                update(Notification).where(Notification.id == n.id).values(claimed_until=None, **values)
            )
            session.add(
                DeliveryAttempt(
                    notification_id=n.id,
                    attempt_no=n.attempts,
                    status="succeeded" if ok else "failed",
                    error=error,
                )
            )

    async def release_stale_claims(self) -> list[str]:
        """Return emails whose worker died mid-send to pending, then send them.

        Returns the booking IDs it retried.
        """
        async with self.sessionmaker() as session, session.begin():
            stale = (
                await session.execute(
                    select(Notification.id, Notification.booking_id).where(
                        Notification.status == NotificationStatus.SENDING,
                        Notification.claimed_until < utcnow(),
                    )
                )
            ).all()
            if stale:
                await session.execute(
                    update(Notification)
                    .where(Notification.id.in_([s.id for s in stale]))
                    .where(Notification.status == NotificationStatus.SENDING)
                    .values(status=NotificationStatus.PENDING, claimed_until=None)
                )
        bookings = sorted({s.booking_id for s in stale})
        for booking_id in bookings:
            try:
                await self.deliver_pending(booking_id)
            except MailDeliveryError as exc:
                log.warning("Stale email for %s still failing: %s", booking_id, exc)
        return bookings


def _customer_and_event(p: dict[str, Any]) -> dict[str, Any]:
    seat = p.get("seat_label")
    if not seat and isinstance(p.get("seats"), list):
        seat = ", ".join(str(x) for x in p["seats"])
    return {
        "customer_email": p.get("customer_email"),
        "customer_name": p.get("customer_name"),
        "event_id": _str(p.get("event_id")),
        "event_title": p.get("event_title"),
        "event_starts_at": _str(p.get("event_starts_at")),
        "seat_label": seat,
    }


def _str(v: Any) -> str | None:
    return None if v is None else str(v)
