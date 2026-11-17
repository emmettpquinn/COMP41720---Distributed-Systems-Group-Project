"""Starts the Booking service. Run with:  python main.py"""
from contextlib import asynccontextmanager

import os
import time

import uvicorn
from fastapi import FastAPI

import config
import db
import metrics
from api import router


@asynccontextmanager
async def lifespan(app):
    db.init_schema()
    if os.getenv("BOOKING_DEMO_SEED", "false").lower() == "true":
        from demo_seed import seed
        seed()
    yield


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
