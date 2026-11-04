"""A reusable consumer that applies the team's retry and dead-letter rules.

Any service can use it:

    await run_consumer(channel, "booking.payment-updates", handle, on_outcome=metrics_hook)

``handle(envelope)`` must be idempotent, because delivery is at-least-once. If
it returns, the message is acked. If it raises, the message is republished to
the next retry queue (1 s, 2 s, 4 s backoff) and the original is acked. Once the
retries are spent, the message is parked in ``<queue>.dlq`` with the last error
in its headers. A message that cannot be parsed goes straight to the DLQ,
because retrying cannot fix it.

Each republish uses publisher confirms and finishes before the ack. A crash
between the two can therefore only cause a redelivery, never a lost message.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Awaitable, Callable

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

from .envelope import Envelope, InvalidEnvelope
from .topology import RETRY_DELAYS_MS, get_queue

log = logging.getLogger(__name__)

RETRIES_HEADER = "x-retries"
LAST_ERROR_HEADER = "x-last-error"
FAILED_QUEUE_HEADER = "x-failed-queue"


class Outcome(str, Enum):
    PROCESSED = "processed"
    RETRIED = "retried"
    DEAD_LETTERED = "dead_lettered"


Handler = Callable[[Envelope], Awaitable[None]]
OutcomeHook = Callable[[Outcome, str, str | None], None]  # (outcome, message type, error)


def next_destination(queue_name: str, retries_so_far: int) -> str:
    """Return where a failed message goes next: a retry queue or the DLQ."""
    q = get_queue(queue_name)
    if retries_so_far < len(RETRY_DELAYS_MS):
        return q.retry_queue(retries_so_far + 1)
    return q.dlq


def _retries(message: AbstractIncomingMessage) -> int:
    try:
        return int((message.headers or {}).get(RETRIES_HEADER, 0))
    except (TypeError, ValueError):
        return 0


async def _republish(
    channel: AbstractChannel,
    message: AbstractIncomingMessage,
    routing_key: str,
    headers: dict,
) -> None:
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=message.body,
            message_id=message.message_id,
            type=message.type,
            content_type=message.content_type,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers={**(message.headers or {}), **headers},
        ),
        routing_key=routing_key,
    )


async def run_consumer(
    channel: AbstractChannel,
    queue_name: str,
    handler: Handler,
    on_outcome: OutcomeHook | None = None,
    prefetch: int = 10,
) -> None:
    """Start consuming ``queue_name``. Returns once the consumer is registered."""
    get_queue(queue_name)  # fail fast on a typo
    await channel.set_qos(prefetch_count=prefetch)
    queue = await channel.get_queue(queue_name, ensure=True)

    def report(outcome: Outcome, msg_type: str, error: str | None = None) -> None:
        if on_outcome:
            try:
                on_outcome(outcome, msg_type, error)
            except Exception:  # noqa: BLE001 - metrics must never break consuming
                log.exception("on_outcome hook failed")

    async def on_message(message: AbstractIncomingMessage) -> None:
        msg_type = message.type or "unknown"
        try:
            envelope = Envelope.from_json(message.body)
        except InvalidEnvelope as exc:
            log.error("Unparseable message %s on %s: %s", message.message_id, queue_name, exc)
            await _republish(
                channel,
                message,
                get_queue(queue_name).dlq,
                {LAST_ERROR_HEADER: f"invalid envelope: {exc}"[:500], FAILED_QUEUE_HEADER: queue_name},
            )
            await message.ack()
            report(Outcome.DEAD_LETTERED, msg_type, str(exc))
            return

        msg_type = envelope.type
        try:
            await handler(envelope)
        except Exception as exc:  # noqa: BLE001 - any handler failure is retried
            retries = _retries(message)
            destination = next_destination(queue_name, retries)
            parked = destination.endswith(".dlq")
            error = f"{type(exc).__name__}: {exc}"[:500]
            log.warning(
                "Handling %s (%s) failed on attempt %d -> %s: %s",
                envelope.message_id, msg_type, retries + 1, destination, error,
            )
            await _republish(
                channel,
                message,
                destination,
                {
                    RETRIES_HEADER: retries if parked else retries + 1,
                    LAST_ERROR_HEADER: error,
                    FAILED_QUEUE_HEADER: queue_name,
                },
            )
            await message.ack()
            report(Outcome.DEAD_LETTERED if parked else Outcome.RETRIED, msg_type, error)
            return

        await message.ack()
        report(Outcome.PROCESSED, msg_type)

    await queue.consume(on_message)
    log.info("Consuming %s", queue_name)
