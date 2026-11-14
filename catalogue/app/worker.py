"""Opt-in broker adapter for the proposed Catalogue payloads."""

import asyncio
import json
import logging

from messaging import Envelope, connect_with_retry, declare_topology, run_consumer
from messaging.topology import EVENTS_EXCHANGE

from .availability import apply_snapshot
from .outbox import dispatch_once
from .schemas import AvailabilityMessage

log = logging.getLogger(__name__)


async def handle_availability(pool, envelope, cache, metrics, timeout=2):
    message = AvailabilityMessage.model_validate_json(envelope.to_json())
    async with asyncio.timeout(timeout):
        async with pool.acquire() as connection:
            outcome, lag = await apply_snapshot(connection, message)
    metrics.availability.labels(outcome).inc()
    if outcome == "applied":
        cache.invalidate(message.payload.event_id)
        if lag >= 0:
            metrics.lag.observe(lag)
        else:
            metrics.clock_skew.inc()
    return outcome


async def run_worker(pool, settings, cache, metrics):
    delay = 0.5
    while True:
        try:
            connection = await connect_with_retry(settings.amqp_url, attempts=3, delay_s=1)
            async with connection:
                consume_channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                await declare_topology(consume_channel)

                async def handle(envelope):
                    await handle_availability(pool, envelope, cache, metrics, settings.database_timeout_seconds)

                def outcome_hook(outcome, msg_type, error):
                    kind = msg_type if msg_type == "seat.availability-changed" else "unsupported"
                    metrics.consumer.labels(kind, outcome.value).inc()

                await run_consumer(consume_channel, "catalogue.seat-availability", handle, on_outcome=outcome_hook)
                publish_channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                exchange = await publish_channel.get_exchange(EVENTS_EXCHANGE, ensure=True)

                async def publish(body):
                    envelope = Envelope.from_json(json.dumps(body).encode())
                    await exchange.publish(envelope.to_amqp(), routing_key=envelope.type, mandatory=True, timeout=5)

                while True:
                    metrics.broker.set(int(connection.connected.is_set()))
                    sent = await dispatch_once(pool, publish)
                    metrics.published.inc(sent)
                    delay = 0.5
                    await asyncio.sleep(settings.outbox_poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Catalogue messaging paused (%s); pending rows remain durable", type(exc).__name__)
        finally:
            metrics.broker.set(0)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)
