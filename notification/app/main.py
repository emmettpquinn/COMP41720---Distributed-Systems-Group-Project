"""Ticketing and Notification service (owner: Emmett Quinn).

Consumes booking.confirmed, booking.cancelled, refund.completed and
refund.failed. Issues and voids e-tickets and emails customers through MailHog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text

from messaging import QUEUES, Outcome, connect_with_retry, declare_topology, run_consumer
from messaging.topology import RETRY_DELAYS_MS

from . import metrics
from .config import settings
from .db import Ticket, create_schema, make_engine, make_sessionmaker
from .handlers import TicketingService
from .mailer import SmtpMailer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ticketing")

SERVICE = "ticketing"


def _record_outcome(outcome: Outcome, msg_type: str, error: str | None) -> None:
    metrics.MESSAGES.labels(msg_type, outcome.value).inc()


async def _poll_queue_depths(connection) -> None:
    """Keep broker_queue_depth current for every queue in the topology."""
    channel = await connection.channel()
    while True:
        for q in QUEUES:
            names = [(q.name, "main"), (q.dlq, "dlq")] + [
                (q.retry_queue(i), "retry") for i in range(1, len(RETRY_DELAYS_MS) + 1)
            ]
            for name, kind in names:
                try:
                    if channel.is_closed:  # a failed passive declare closes the channel
                        channel = await connection.channel()
                    # Use the underlying channel: aio-pika's robust channel caches
                    # queue declarations, so its message_count never changes.
                    raw = await channel.get_underlay_channel()
                    declared = await raw.queue_declare(name, passive=True)
                    metrics.QUEUE_DEPTH.labels(name, q.owner, kind).set(declared.message_count)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Depth poll for %s failed: %s", name, exc)
        await asyncio.sleep(settings.queue_depth_poll_s)


async def _sweep_stale_claims(service: TicketingService) -> None:
    """Recover emails claimed by a worker that crashed mid-send."""
    while True:
        await asyncio.sleep(30)
        try:
            recovered = await service.release_stale_claims()
            if recovered:
                log.warning("Recovered stale email claims for bookings %s", recovered)
        except Exception:  # noqa: BLE001
            log.exception("Stale-claim sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = make_engine(settings.database_url)
    for attempt in range(30):
        try:
            await create_schema(engine)
            break
        except Exception as exc:  # noqa: BLE001 - database still starting
            log.warning("Database not ready (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("Database never became ready")

    sessionmaker = make_sessionmaker(engine)
    mailer = SmtpMailer(settings.smtp_host, settings.smtp_port, settings.mail_from, settings.smtp_timeout_s)
    service = TicketingService(sessionmaker, mailer)

    connection = await connect_with_retry(settings.amqp_url)
    consume_channel = await connection.channel()
    await declare_topology(consume_channel)  # idempotent; topology-init normally did this already
    await run_consumer(consume_channel, settings.consumer_queue, service.handle, on_outcome=_record_outcome)

    poller = asyncio.create_task(_poll_queue_depths(connection))
    sweeper = asyncio.create_task(_sweep_stale_claims(service))

    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.connection = connection
    try:
        yield
    finally:
        for task in (poller, sweeper):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await connection.close()
        await engine.dispose()


app = FastAPI(title="Ticketing and Notification", lifespan=lifespan)


@app.middleware("http")
async def http_metrics(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        if path != "/metrics":
            metrics.HTTP_REQUESTS.labels(SERVICE, request.method, path, str(status)).inc()
            metrics.HTTP_LATENCY.labels(SERVICE, request.method, path).observe(time.perf_counter() - start)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request) -> dict:
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"database unavailable: {exc}") from exc
    if request.app.state.connection.is_closed:
        raise HTTPException(503, "broker connection closed")
    return {"status": "ready"}


@app.get("/tickets/{booking_id}")
async def get_ticket(booking_id: str, request: Request) -> dict:
    """Check whether a booking holds a valid ticket."""
    async with request.app.state.sessionmaker() as session:
        t = (
            await session.execute(select(Ticket).where(Ticket.booking_id == booking_id))
        ).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "No ticket for this booking")
    return {
        "booking_id": t.booking_id,
        "ticket_id": t.ticket_id,
        "ticket_code": t.ticket_code,
        "status": t.status,
        "valid": t.status == "valid",
        "event_id": t.event_id,
        "event_title": t.event_title,
        "seat_label": t.seat_label,
        "issued_at": t.issued_at,
        "voided_at": t.voided_at,
        "void_reason": t.void_reason,
    }


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
