"""Publish test booking messages, standing in for Booking until it exists.

    python -m scripts.publish_bookings --count 5                 # 5 bookings confirmed
    python -m scripts.publish_bookings --count 1 --copies 100    # 1 message delivered 100 times
    python -m scripts.publish_bookings --type booking.cancelled --booking bk-demo-1 --reason event_cancelled
    python -m scripts.publish_bookings --type refund.completed --booking bk-demo-1

Prints the booking IDs it used, so you can check them afterwards with
``GET /tickets/{booking_id}`` and ``scripts.check_mailhog``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from messaging import EVENTS_EXCHANGE, Envelope, MessageType, connect_with_retry


def payload(msg_type: str, booking_id: str, reason: str | None) -> dict:
    base = {"booking_id": booking_id, "customer_email": f"{booking_id}@example.com"}
    if msg_type == MessageType.BOOKING_CONFIRMED:
        return {
            **base,
            "customer_name": "Demo Customer",
            "event_id": "ev-demo",
            "event_title": "Late Night Jazz at the Harbour Room",
            "event_starts_at": "2026-11-13T21:00:00+00:00",
            "seat_label": "C7",
        }
    if msg_type == MessageType.BOOKING_CANCELLED:
        return {**base, "reason": reason or "hold_expired"}
    if msg_type in (MessageType.REFUND_COMPLETED, MessageType.REFUND_FAILED):
        return {**base, "payment_id": f"pay-{booking_id}", "amount": "45.00", "currency": "EUR"}
    raise SystemExit(f"Unsupported type {msg_type}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", default=MessageType.BOOKING_CONFIRMED)
    parser.add_argument("--count", type=int, default=1, help="number of distinct bookings")
    parser.add_argument("--copies", type=int, default=1, help="publish each message this many times (same message ID)")
    parser.add_argument("--booking", help="use this booking ID (only with --count 1)")
    parser.add_argument("--reason", help="cancellation reason")
    parser.add_argument("--amqp-url", default=os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/"))
    args = parser.parse_args()

    connection = await connect_with_retry(args.amqp_url, attempts=3)
    async with connection:
        channel = await connection.channel()
        exchange = await channel.get_exchange(EVENTS_EXCHANGE)
        for _ in range(args.count):
            booking_id = args.booking if args.booking and args.count == 1 else f"bk-{uuid.uuid4().hex[:8]}"
            env = Envelope.new(args.type, payload(args.type, booking_id, args.reason), producer="demo-script")
            for _ in range(args.copies):
                await exchange.publish(env.to_amqp(), routing_key=env.type)
            print(f"{env.type} {booking_id} message_id={env.message_id} x{args.copies}")


if __name__ == "__main__":
    asyncio.run(main())
