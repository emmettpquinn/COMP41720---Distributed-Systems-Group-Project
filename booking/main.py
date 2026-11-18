"""Starts the Booking service. Run with:  python main.py"""
from contextlib import asynccontextmanager

import asyncio
import contextlib
import logging
import os
import time

import uvicorn
from fastapi import FastAPI

import config
import db
import events
import metrics
from api import router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app):
    db.init_schema()
    if os.getenv("BOOKING_DEMO_SEED", "false").lower() == "true":
        from demo_seed import seed
        seed()
    task = None
    if config.AMQP_URL:
        # Fail startup on a missing shared package, rather than hide it in a task.
        import messaging  # noqa: F401
        # Runs in the background: holds and /ready never wait for RabbitMQ.
        task = asyncio.create_task(events.run(config.AMQP_URL))
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


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
