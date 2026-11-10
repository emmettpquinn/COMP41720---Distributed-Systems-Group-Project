"""RabbitMQ topology for the whole platform, defined in code.

Owner: Emmett Quinn (topology). Message names come from the team plan;
Erwan Brennan owns the message contracts (fields and versions).

Layout
------
* ``events``  - one durable topic exchange. Producers publish with the
  routing key equal to the message type, e.g. ``booking.confirmed``.
* One durable queue per consumer, named ``<service>.<purpose>``, bound to the
  message types that consumer needs.
* For every consumer queue ``Q``:
    - ``Q.retry.1`` / ``Q.retry.2`` / ``Q.retry.3``: holding queues with a fixed
      TTL (1 s, 2 s, 4 s). When the TTL runs out the broker dead-letters the
      message back into ``Q``. One queue per delay avoids head-of-line blocking,
      which a single queue with per-message TTLs would suffer from.
    - ``Q.dlq``: the parking lot. A message lands here after its retries are
      used up, or if it cannot be parsed at all. It stays until it is replayed
      by hand (``python -m messaging.replay Q``).
* ``dlx`` - a direct exchange that ``Q`` dead-letters to on a broker-side
  reject, so even a consumer that crashes mid-message cannot lose it.

Running ``python -m messaging.topology`` declares everything. It is
idempotent, and the one-command startup runs it once before any service starts.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import aio_pika
from aio_pika.abc import AbstractChannel

log = logging.getLogger(__name__)

EVENTS_EXCHANGE = "events"
DLX_EXCHANGE = "dlx"

# Backoff before retry 1, 2 and 3. A message is attempted once and then
# retried len(RETRY_DELAYS_MS) times before it is parked in the DLQ.
RETRY_DELAYS_MS: tuple[int, ...] = (1_000, 2_000, 4_000)


class MessageType:
    EVENT_CHANGED = "event.changed"
    SEAT_AVAILABILITY_CHANGED = "seat.availability-changed"
    PAYMENT_RESULT = "payment.result"
    REFUND_REQUESTED = "refund.requested"
    REFUND_COMPLETED = "refund.completed"
    REFUND_FAILED = "refund.failed"
    BOOKING_CONFIRMED = "booking.confirmed"
    BOOKING_CANCELLED = "booking.cancelled"

    ALL = (
        EVENT_CHANGED,
        SEAT_AVAILABILITY_CHANGED,
        PAYMENT_RESULT,
        REFUND_REQUESTED,
        REFUND_COMPLETED,
        REFUND_FAILED,
        BOOKING_CONFIRMED,
        BOOKING_CANCELLED,
    )


@dataclass(frozen=True)
class ConsumerQueue:
    name: str
    owner: str
    bindings: tuple[str, ...]

    def retry_queue(self, attempt: int) -> str:
        return f"{self.name}.retry.{attempt}"

    @property
    def dlq(self) -> str:
        return f"{self.name}.dlq"


# One entry per consumer. This matches "Messages on the broker" in the team plan.
QUEUES: tuple[ConsumerQueue, ...] = (
    ConsumerQueue(
        "catalogue.seat-availability",
        owner="catalogue",
        bindings=(MessageType.SEAT_AVAILABILITY_CHANGED,),
    ),
    ConsumerQueue(
        "booking.event-changes",
        owner="booking",
        bindings=(MessageType.EVENT_CHANGED,),
    ),
    ConsumerQueue(
        "booking.payment-updates",
        owner="booking",
        bindings=(
            MessageType.PAYMENT_RESULT,
            MessageType.REFUND_COMPLETED,
            MessageType.REFUND_FAILED,
        ),
    ),
    ConsumerQueue(
        "discovery.event-changes",
        owner="discovery",
        bindings=(MessageType.EVENT_CHANGED,),
    ),
    ConsumerQueue(
        "payment.refund-requests",
        owner="payment",
        bindings=(MessageType.REFUND_REQUESTED,),
    ),
    ConsumerQueue(
        "ticketing.booking-updates",
        owner="ticketing",
        bindings=(
            MessageType.BOOKING_CONFIRMED,
            MessageType.BOOKING_CANCELLED,
            MessageType.REFUND_COMPLETED,
            MessageType.REFUND_FAILED,
        ),
    ),
)

QUEUES_BY_NAME = {q.name: q for q in QUEUES}


def get_queue(name: str) -> ConsumerQueue:
    try:
        return QUEUES_BY_NAME[name]
    except KeyError:
        known = ", ".join(sorted(QUEUES_BY_NAME))
        raise KeyError(f"Unknown queue {name!r}. Known queues: {known}") from None


def all_queue_names() -> list[str]:
    """Every queue the topology creates: main, retry and dead-letter."""
    names: list[str] = []
    for q in QUEUES:
        names.append(q.name)
        names.extend(q.retry_queue(i + 1) for i in range(len(RETRY_DELAYS_MS)))
        names.append(q.dlq)
    return names


async def declare_topology(channel: AbstractChannel) -> None:
    """Declare every exchange and queue. Safe to call any number of times."""
    events = await channel.declare_exchange(
        EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
    )
    dlx = await channel.declare_exchange(
        DLX_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )

    for q in QUEUES:
        main = await channel.declare_queue(
            q.name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": DLX_EXCHANGE,
                "x-dead-letter-routing-key": q.name,
            },
        )
        for routing_key in q.bindings:
            await main.bind(events, routing_key=routing_key)

        for i, delay_ms in enumerate(RETRY_DELAYS_MS, start=1):
            await channel.declare_queue(
                q.retry_queue(i),
                durable=True,
                arguments={
                    "x-message-ttl": delay_ms,
                    # "" is the default exchange: route straight back to Q.
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": q.name,
                },
            )

        dlq = await channel.declare_queue(q.dlq, durable=True)
        await dlq.bind(dlx, routing_key=q.name)

    log.info("Topology declared: %d consumer queues", len(QUEUES))


async def connect_with_retry(url: str, attempts: int = 30, delay_s: float = 2.0):
    """Connect to RabbitMQ, waiting for it to come up during startup."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await aio_pika.connect_robust(url)
        except Exception as exc:  # noqa: BLE001 - any failure here means "not up yet"
            last_exc = exc
            log.warning("RabbitMQ not reachable (attempt %d/%d): %s", attempt, attempts, exc)
            await asyncio.sleep(delay_s)
    raise RuntimeError(f"Could not connect to RabbitMQ at {url}") from last_exc


async def _main() -> None:
    url = os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/")
    connection = await connect_with_retry(url)
    async with connection:
        channel = await connection.channel()
        await declare_topology(channel)
    for name in all_queue_names():
        print(name)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
