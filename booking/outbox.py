"""Outbox relay: publishes Booking's outbox rows to RabbitMQ (contract section 3).

Same pattern as Catalogue's outbox (catalogue/app/outbox.py, worker.py):
- the stored envelope is never rebuilt, so a republish has the same
  message_id and body;
- each row is published with publisher confirms and mandatory=True, and only
  THEN marked published. A crash between the two means the row is sent again
  later (at-least-once); Catalogue ignores the repeat by message_id and by
  sequence.
Holds never wait for this: they only write the row and commit.
"""
import asyncio
import logging

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


async def refresh_pending():
    """Set booking_outbox_pending from the database. Never raises."""
    try:
        metrics.OUTBOX_PENDING.set(await asyncio.to_thread(db.count_pending_outbox))
    except Exception:  # noqa: BLE001 - a metric must never stop the relay
        log.warning("Could not count pending outbox rows", exc_info=True)


async def dispatch_once(publish, limit=BATCH):
    """Publish up to `limit` unpublished rows, oldest first.

    `publish(envelope_text)` must return only once the broker has confirmed.
    On the first failure the row is left unpublished, the failure is logged,
    and PublishFailed is raised (the remaining rows wait for the next run, so
    the order stays oldest first). Returns how many rows were published.
    """
    rows = await asyncio.to_thread(db.pending_outbox, limit)
    sent = 0
    for message_id, envelope_text in rows:
        try:
            await publish(envelope_text)
        except Exception as error:
            log.warning("Publish of outbox row %s failed (%s); it stays pending",
                        message_id, type(error).__name__)
            raise PublishFailed(message_id) from error
        await asyncio.to_thread(db.mark_published, message_id)
        metrics.OUTBOX_PUBLISHED.labels(_type_of(envelope_text)).inc()
        sent += 1
    return sent


def _type_of(envelope_text):
    from messaging import Envelope
    return Envelope.from_json(envelope_text.encode()).type


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
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                exchange = await channel.get_exchange(EXCHANGE, ensure=True)

                async def publish(envelope_text):
                    envelope = Envelope.from_json(envelope_text.encode())
                    # Waits for the broker's confirm; an unroutable message
                    # (mandatory=True) or a timeout raises instead.
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
