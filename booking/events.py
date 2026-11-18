"""Consumes Catalogue's event.changed messages from booking.event-changes.

Contract sections 3 and 7. The queue, its retry queues and its DLQ are declared
by topology-init; this module only consumes, through shared/messaging's
run_consumer, which acks on success and retries (then dead-letters) on error.
If RabbitMQ is unreachable, holds keep working: run() keeps reconnecting in
the background and logs each failure, like Payment's worker.
"""
import asyncio
import contextlib
import logging

import db
import metrics

log = logging.getLogger("booking.events")

QUEUE = "booking.event-changes"
EVENT_CHANGED = "event.changed"
SHUTDOWN_WAIT_S = 5


async def handle(envelope):
    """Dispatch by envelope type, never by a payload field (contract section 3)."""
    if envelope.type != EVENT_CHANGED:
        # Nothing on this queue but event.changed is expected; ack and move on.
        log.warning("Ignoring %s message %s on %s", envelope.type, envelope.message_id, QUEUE)
        return
    # Sync psycopg2 code runs in a thread so the event loop keeps serving HTTP.
    # A database error propagates, so the shared consumer retries the message.
    result = await asyncio.to_thread(db.apply_event_changed, envelope.message_id, envelope.payload)
    payload = envelope.payload
    log.info(
        "event.changed %s: %s v%s (%s) -> %s",
        envelope.message_id, payload.get("event_id"), payload.get("event_version"),
        payload.get("status"), result,
    )


def track_outcome(outcome, msg_type, error):
    """run_consumer's on_outcome hook: processed, retried or dead_lettered."""
    metrics.MESSAGES.labels(msg_type, outcome.value).inc()


async def _connect(url):
    """connect_with_retry, but never cancelled halfway through a connect.

    aio-pika 10 keeps a reconnect loop running forever if connect_robust is
    cancelled mid-attempt, which stops the process from exiting. On shutdown we
    therefore let the attempt finish (normally a few seconds; at most
    SHUTDOWN_WAIT_S) and close what it opened.
    """
    from messaging import connect_with_retry

    attempt = asyncio.ensure_future(connect_with_retry(url, attempts=3, delay_s=1))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        done, _ = await asyncio.wait({attempt}, timeout=SHUTDOWN_WAIT_S)
        if done and not attempt.cancelled() and attempt.exception() is None:
            with contextlib.suppress(Exception):
                await attempt.result().close()
        elif not done:
            log.warning("RabbitMQ connect still pending at shutdown; not waiting longer")
        raise


async def run(url):
    """Connect, consume, and reconnect forever. Cancel the task to stop it."""
    # Imported here so the module imports without the shared package (as Payment does).
    from messaging import run_consumer

    while True:
        try:
            connection = await _connect(url)
            async with connection:
                channel = await connection.channel(publisher_confirms=True)
                await run_consumer(channel, QUEUE, handle, on_outcome=track_outcome)
                # The robust connection re-registers the consumer after a reconnect.
                await asyncio.Future()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep trying; holds do not depend on this
            log.exception("Messaging paused; retrying the RabbitMQ connection")
            await asyncio.sleep(2)
