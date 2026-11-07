"""Catalogue settings. Defaults are for local development only."""

import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    database_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.database_timeout_seconds) or self.database_timeout_seconds <= 0:
            raise ValueError("DATABASE_TIMEOUT_SECONDS must be a finite positive number")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv(
                "DATABASE_URL", "postgresql://catalogue:catalogue@localhost:5432/catalogue"
            ),
            database_timeout_seconds=float(os.getenv("DATABASE_TIMEOUT_SECONDS", "2")),
        )
