"""Outbox relay: publishes Booking's outbox rows to RabbitMQ (contract section 3).

Same pattern as Catalogue's outbox (catalogue/app/outbox.py, worker.py):
- the stored envelope is never rebuilt, so a republish has the same
  message_id and body;
- each row is published with publisher confirms and mandatory=True, and only
  THEN marked published. A crash between the two means the row is sent again
  later (at-least-once); Catalogue ignores the repeat by message_id and by
  sequence.
Holds never wait for this: they only write the row and commit.

Two kinds of publish failure (contract section 3):
- PERMANENT: the broker returned the message as unroutable (mandatory=True,
  basic.return: no queue is bound to its routing key). Trying again cannot
  help, so the row is set aside (failed_at, error), logged, counted in
  booking_outbox_failed, and the relay moves on to the next row.
- TEMPORARY: everything else (connection or channel lost, confirm timeout, a
  broker nack, any unexpected error). The row stays pending, PublishFailed is
  raised, and the rows after it wait for the next run, so the order stays
  oldest first.
"""
import asyncio
import logging

# aio-pika 10.2.3 (aiormq 7.2.5): with on_return_raises=True, a returned
# (unroutable) message raises PublishError; a broker nack raises its parent
# class DeliveryError with a Basic.Nack frame. Only PublishError is permanent.
from aio_pika.exceptions import PublishError

import db
import events
import metrics

log = logging.getLogger("booking.outbox")

EXCHANGE = "events"
POLL_SECONDS = 0.25
BATCH = 50
PUBLISH_TIMEOUT_S = 5


class PublishFailed(Exception):
    """A publish was not confirmed. The row stays unpublished for the next try."""


def is_permanent(error):
    """True only for a message the broker returned as unroutable (basic.return)."""
    return isinstance(error, PublishError)


def describe(error):
    """The text stored on a set-aside row: exception class plus reply code and text."""
    frame = getattr(error, "frame", None)
    return (f"{type(error).__name__}: reply_code={getattr(frame, 'reply_code', None)} "
            f"reply_text={getattr(frame, 'reply_text', None)} "
            f"exchange={getattr(frame, 'exchange', None)} "
            f"routing_key={getattr(frame, 'routing_key', None)}")


async def refresh_pending():
    """Set booking_outbox_pending and booking_outbox_failed from the database. Never raises."""
    try:
        metrics.OUTBOX_PENDING.set(await asyncio.to_thread(db.count_pending_outbox))
        metrics.OUTBOX_FAILED.set(await asyncio.to_thread(db.count_failed_outbox))
    except Exception:  # noqa: BLE001 - a metric must never stop the relay
        log.warning("Could not count outbox rows", exc_info=True)


async def dispatch_once(publish, limit=BATCH):
    """Publish up to `limit` pending rows, oldest first.

    `publish(envelope_text)` must return only once the broker has confirmed.
    A permanent failure sets the row aside and carries on with the next row.
    On the first temporary failure the row is left pending, the failure is
    logged, and PublishFailed is raised (the remaining rows wait for the next
    run). Returns how many rows were published.
    """
    rows = await asyncio.to_thread(db.pending_outbox, limit)
    sent = 0
    for message_id, message_type, envelope_text in rows:
        try:
            await publish(envelope_text)
        except Exception as error:
            if is_permanent(error):
                text = describe(error)
                await asyncio.to_thread(db.set_aside_outbox, message_id, text)
                log.warning("Outbox row %s (%s) was returned as unroutable and is set aside: %s",
                            message_id, message_type, text)
                continue
            log.warning("Publish of outbox row %s (%s) failed (%s); it stays pending",
                        message_id, message_type, type(error).__name__)
            raise PublishFailed(message_id) from error
        await asyncio.to_thread(db.mark_published, message_id)
        metrics.OUTBOX_PUBLISHED.labels(message_type).inc()
        sent += 1
    return sent


async def run(url):
    """Connect, publish pending rows every POLL_SECONDS, reconnect forever.

    Cancel the task to stop it. Connects through events._connect, which never
    abandons a connect attempt halfway (see its docstring), so shutdown is clean.
    """
    from messaging import Envelope

    while True:
        try:
            connection = await events._connect(url)
            async with connection:
                # on_return_raises=True: an unroutable message raises PublishError
                # (permanent) instead of being reported as a success.
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                exchange = await channel.get_exchange(EXCHANGE, ensure=True)

                async def publish(envelope_text):
                    envelope = Envelope.from_json(envelope_text.encode())
                    # Waits for the broker's confirm; an unroutable message
                    # (mandatory=True), a nack or a timeout raises instead.
                    await exchange.publish(envelope.to_amqp(), routing_key=envelope.type,
                                           mandatory=True, timeout=PUBLISH_TIMEOUT_S)

                log.info("Outbox relay connected")
                while True:
                    await dispatch_once(publish)
                    await refresh_pending()
                    await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - keep trying; rows stay durable
            if not isinstance(error, PublishFailed):
                log.warning("Outbox relay paused (%s); pending rows stay in the outbox",
                            type(error).__name__)
            await refresh_pending()
            await asyncio.sleep(2)
