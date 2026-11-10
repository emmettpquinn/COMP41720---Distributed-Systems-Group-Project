"""Ticketing's own database. No other service reads or writes these tables."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TicketStatus:
    VALID = "valid"
    VOID = "void"


class Ticket(Base):
    """One row per booking, which the unique constraint enforces.

    A row with status ``void`` and no ``ticket_code`` is a tombstone. It records
    that the booking was cancelled or refunded before any ticket was issued, so
    a late ``booking.confirmed`` can never issue one.
    """

    __tablename__ = "tickets"

    ticket_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    booking_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    ticket_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    customer_email: Mapped[str | None] = mapped_column(String(255))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    event_id: Mapped[str | None] = mapped_column(String(64))
    event_title: Mapped[str | None] = mapped_column(String(255))
    event_starts_at: Mapped[str | None] = mapped_column(String(64))
    seat_label: Mapped[str | None] = mapped_column(String(64))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessedMessage(Base):
    """Message IDs already applied. Written in the same transaction as the ticket change."""

    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    booking_id: Mapped[str | None] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationStatus:
    PENDING = "pending"
    SENDING = "sending"  # claimed by one worker until claimed_until
    SENT = "sent"
    SKIPPED = "skipped"  # no recipient known


class Notification(Base):
    """An email we owe a customer. At most one of each kind per booking."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("booking_id", "kind", name="uq_notification_booking_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=NotificationStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    source_message_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeliveryAttempt(Base):
    """The delivery log: every send attempt and how it went."""

    __tablename__ = "delivery_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("notifications.id"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # succeeded | failed
    error: Mapped[str | None] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
