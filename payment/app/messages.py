"""Adapter for the existing shared topology; payloads are proposed version 1."""

import asyncio
import json
import logging

from pydantic import BaseModel, Field

from . import metrics, store

log = logging.getLogger(__name__)


class RefundInput(BaseModel):
    booking_id: str = Field(min_length=1, max_length=64, pattern=r"\S")
    payment_id: str = Field(min_length=1, max_length=64, pattern=r"\S")
    customer_email: str | None = Field(default=None, max_length=255)


async def process_refund(db_url, settings, msg):
    if msg.type != "refund.requested" or msg.version != 1:
        raise ValueError("Expected refund.requested version 1")
    if not msg.message_id.strip():
        raise ValueError("A message ID is required")
    data = RefundInput.model_validate(msg.payload)
    await asyncio.to_thread(store.refund, db_url, data.payment_id, settings.mode,
                            settings.delay, settings.error_rate, msg.message_id,
                            data.booking_id, data.customer_email)


async def send_events(db_url, publish):
    """Mark only confirmed publishes as sent. Replays keep the original ID."""
    rows = await asyncio.to_thread(store.pending_events, db_url)
    for row in rows:
        body = json.loads(row["body"])
        await publish(body)
        await asyncio.to_thread(store.mark_sent, db_url, row["message_id"])
        metrics.PUBLISHED.labels(body["type"]).inc()


async def run_worker(db_url, settings, url, state):
    # Import lazily so HTTP-only local development does not need shared on sys.path.
    from messaging import Envelope, connect_with_retry, run_consumer
    from messaging.topology import declare_topology

    def track_message(outcome, msg_type, error):
        metrics.MESSAGES.labels(outcome.value).inc()

    async def receive(msg):
        await process_refund(db_url, settings, msg)

    while True:
        try:
            conn = await connect_with_retry(url, attempts=3, delay_s=1)
            async with conn:
                channel = await conn.channel(publisher_confirms=True, on_return_raises=True)
                await declare_topology(channel)
                await run_consumer(channel, "payment.refund-requests", receive,
                                   on_outcome=track_message, prefetch=1)
                # Keep outbox publications separate from consumer retry publications.
                out = await conn.channel(publisher_confirms=True, on_return_raises=True)
                exchange = await out.get_exchange("events", ensure=True)

                async def publish(body):
                    msg = Envelope.from_json(json.dumps(body).encode())
                    await exchange.publish(msg.to_amqp(), routing_key=msg.type,
                                           mandatory=True, timeout=10)

                state.broker = conn
                while True:
                    metrics.BROKER.set(int(conn.connected.is_set()))
                    await send_events(db_url, publish)
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Messaging paused; unsent events remain in the outbox")
            await asyncio.sleep(2)
        finally:
            state.broker = None
            metrics.BROKER.set(0)
