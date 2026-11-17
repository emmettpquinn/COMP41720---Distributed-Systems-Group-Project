"""Catalogue settings. Defaults are for local development only."""

import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    database_timeout_seconds: float = 2.0
    messaging_enabled: bool = False
    amqp_url: str = ""
    cache_ttl_seconds: float = 0.5
    cache_max_entries: int = 128
    outbox_poll_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not math.isfinite(self.database_timeout_seconds) or self.database_timeout_seconds <= 0:
            raise ValueError("DATABASE_TIMEOUT_SECONDS must be a finite positive number")
        if not math.isfinite(self.cache_ttl_seconds) or self.cache_ttl_seconds < 0:
            raise ValueError("CACHE_TTL_SECONDS must be finite and nonnegative")
        if self.cache_max_entries < 1:
            raise ValueError("CACHE_MAX_ENTRIES must be positive")
        if not math.isfinite(self.outbox_poll_seconds) or self.outbox_poll_seconds <= 0:
            raise ValueError("OUTBOX_POLL_SECONDS must be finite and positive")
        if self.messaging_enabled and not self.amqp_url:
            raise ValueError("AMQP_URL is required when Catalogue messaging is enabled")

    @classmethod
    def from_env(cls) -> "Settings":
        messaging = os.getenv("CATALOGUE_MESSAGING_ENABLED", "false").lower()
        if messaging not in {"true", "false"}:
            raise ValueError("CATALOGUE_MESSAGING_ENABLED must be true or false")
        return cls(
            database_url=os.getenv(
                "DATABASE_URL", "postgresql://catalogue:catalogue@localhost:5432/catalogue"
            ),
            database_timeout_seconds=float(os.getenv("DATABASE_TIMEOUT_SECONDS", "2")),
            messaging_enabled=messaging == "true",
            amqp_url=os.getenv("AMQP_URL", ""),
            cache_ttl_seconds=float(os.getenv("CACHE_TTL_SECONDS", "0.5")),
            cache_max_entries=int(os.getenv("CACHE_MAX_ENTRIES", "128")),
            outbox_poll_seconds=float(os.getenv("OUTBOX_POLL_SECONDS", "0.25")),
        )
