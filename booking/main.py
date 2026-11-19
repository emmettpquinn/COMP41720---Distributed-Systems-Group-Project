"""Starts the Booking service. Run with:  python main.py"""
from contextlib import asynccontextmanager

import asyncio
import contextlib
import logging
import time

import uvicorn
from fastapi import FastAPI

import config
import db
import events
import expiry
import metrics
import outbox
import payments
from api import router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app):
    # Refuse to start with a price or currency Payment would always reject.
    config.validate()
    db.init_schema()
    if config.BOOKING_DEMO_SEED:
        from demo_seed import seed
        seed()
    # Contract section 4b: seats held by the old hold-only code have no booking;
    # free them first, so the backfill below sees the right free set.
    freed, upgrade_snapshots = db.free_holds_without_booking()
    logging.getLogger("booking").info(
        "Upgrade rule: freed %d held seat(s) without a booking (%d availability snapshot(s))",
        freed, upgrade_snapshots,
    )
    # Contract section 11: every event that has never had a snapshot gets one
    # (written to the outbox; the relay publishes it). A second start writes none.
    written = db.backfill_availability()
    if written:
        logging.getLogger("booking").info("Backfilled availability for %d event(s)", written)
    # Uses the relay's shared database connection (db.py), in a thread, before
    # the relay task exists; db.py's lock keeps the two from overlapping anyway.
    await outbox.refresh_pending()
    # Checkout's background Payment calls: one shared HTTP client and circuit
    # breaker for the app's whole life (payments.py).
    app.state.checkout = payments.create_runner(config.PAYMENT_URL)
    # Contract section 4, row 4: the hold expiry sweeper. Started after the
    # upgrade rule and the backfill above; it only writes the outbox, so it
    # runs with or without RabbitMQ. Cancelled with the other tasks below.
    tasks = [asyncio.create_task(expiry.run())]
    if config.AMQP_URL:
        # Fail startup on a missing shared package, rather than hide it in a task.
        import messaging  # noqa: F401
        # Both run in the background: holds and /ready never wait for RabbitMQ.
        tasks.append(asyncio.create_task(events.run(config.AMQP_URL)))
        tasks.append(asyncio.create_task(outbox.run(config.AMQP_URL)))
    try:
        yield
    finally:
        # Payment calls still running are abandoned; their bookings stay
        # payment_pending (Payment finishes a started charge anyway).
        await app.state.checkout.shutdown()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # The relay has stopped; close its shared database connection.
        await asyncio.to_thread(db.close_relay_connection)


app = FastAPI(title="Booking", lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def track_request(request, call_next):
    """Count every request and time it (same as Payment's middleware)."""
    start = time.perf_counter()
    status = 500  # if the handler crashes, we still record a 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # Use the route template (e.g. "/holds"), never the raw URL, so IDs don't become labels.
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        if path != "/metrics":
            metrics.REQUESTS.labels(request.method, path, str(status)).inc()
            metrics.LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
            if status >= 400:
                metrics.ERRORS.labels(str(status)).inc()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT)  # port: see config.py (temporary)
