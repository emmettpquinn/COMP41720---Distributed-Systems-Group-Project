"""Per-application metrics with bounded labels and an isolated registry."""

import asyncio
import asyncpg

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self, messaging_enabled=False):
        self.registry = CollectorRegistry()
        self.requests = Counter("http_requests_total", "HTTP requests", ["service", "method", "route", "status"], registry=self.registry)
        self.latency = Histogram("http_request_duration_seconds", "HTTP request latency", ["service", "method", "route"], registry=self.registry)
        self.outbox = Gauge("catalogue_outbox_pending", "Committed messages awaiting publish confirmation", registry=self.registry)
        self.published = Counter("catalogue_events_published_total", "Confirmed outbox deliveries", registry=self.registry)
        self.database = Gauge("catalogue_database_up", "Whether the metrics DB probe succeeded", registry=self.registry)
        self.messaging_enabled = Gauge("catalogue_messaging_enabled", "Whether the proposed messaging adapter is enabled", registry=self.registry)
        self.messaging_enabled.set(int(messaging_enabled))
        self.broker = Gauge("catalogue_broker_connected", "Whether the messaging worker is connected", registry=self.registry)
        self.availability = Counter("catalogue_availability_messages_total", "Snapshot application outcomes", ["outcome"], registry=self.registry)
        self.lag = Histogram("catalogue_availability_lag_seconds", "Source change to projection application time", buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120), registry=self.registry)
        self.clock_skew = Counter("catalogue_availability_clock_skew_total", "Applied updates with source timestamps ahead of the DB clock", registry=self.registry)
        self.consumer = Counter("catalogue_consumer_messages_total", "Shared consumer outcomes", ["type", "outcome"], registry=self.registry)
        self.cache = Counter("catalogue_cache_requests_total", "Detail cache lookups", ["result"], registry=self.registry)

    async def refresh(self, pool, timeout):
        try:
            async with asyncio.timeout(timeout):
                async with pool.acquire() as connection:
                    pending = await connection.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL")
            self.outbox.set(pending)
            self.database.set(1)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            # Export a failed dependency probe rather than reporting an invented zero.
            self.database.set(0)
            self.outbox.set(float("nan"))
