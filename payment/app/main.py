import asyncio
import contextlib
import os
import psycopg
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from . import config, messages, metrics, provider, store


class PaymentInput(BaseModel):
    booking_id: str = Field(min_length=1, max_length=64, pattern=r"\S")
    amount: int = Field(gt=0, strict=True, description="Minor units: 4500 means EUR 45.00")
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    customer_email: str | None = Field(default=None, max_length=255)


def call_store(fn, *args):
    try:
        return fn(*args)
    except (store.Conflict, provider.RefundRejected) as error:
        raise HTTPException(409, str(error)) from error
    except KeyError as error:
        raise HTTPException(404, "Payment not found") from error
    except (ConnectionError, psycopg.OperationalError, psycopg.InterfaceError) as error:
        raise HTTPException(503, "Payment unavailable; retry with the same booking ID") from error


def create_app(db_url=None, amqp_url=None):
    db_url = db_url or os.getenv("DATABASE_URL", "postgresql://payment:payment@127.0.0.1:5434/payment")
    url = os.getenv("AMQP_URL", "") if amqp_url is None else amqp_url
    settings = config.load_settings()

    @asynccontextmanager
    async def startup(app):
        await asyncio.to_thread(store.setup_db, db_url)
        app.state.broker = None
        task = None
        if url:
            # Fail startup on a missing shared package, rather than hide it in a task.
            import messaging  # noqa: F401
            task = asyncio.create_task(messages.run_worker(db_url, settings, url, app.state))
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Stagedoor Payment", lifespan=startup)

    @app.middleware("http")
    async def track_request(request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            name = getattr(route, "path", "unmatched")
            if name != "/metrics":
                metrics.REQUESTS.labels(request.method, name, str(status)).inc()
                metrics.LATENCY.labels(request.method, name).observe(time.perf_counter() - start)
                if status >= 400:
                    metrics.ERRORS.labels(str(status)).inc()


    @app.post("/payments")
    def pay(data: PaymentInput):
        return call_store(store.charge, db_url, data.booking_id, data.amount, data.currency,
                   settings.mode, settings.delay, settings.error_rate, data.customer_email)

    @app.get("/payments/{payment_id}")
    def get_payment(payment_id: str):
        return call_store(store.get_payment, db_url, payment_id)

    @app.post("/payments/{payment_id}/refunds")
    def refund(payment_id: str):
        return call_store(store.refund, db_url, payment_id, settings.mode, settings.delay, settings.error_rate)


    @app.get("/metrics")
    def get_metrics():
        counts = call_store(store.get_totals, db_url)
        for status in ("paid", "failed", "refunded"):
            metrics.PAYMENTS.labels(status).set(counts["payments"].get(status, 0))
        metrics.OUTBOX.set(counts["outbox"])
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        call_store(store.get_totals, db_url)
        conn = app.state.broker
        if url and (conn is None or not conn.connected.is_set()):
            raise HTTPException(503, "Broker is not connected")
        return {"status": "ready", "messaging": "enabled" if url else "disabled"}

    return app


app = create_app()
