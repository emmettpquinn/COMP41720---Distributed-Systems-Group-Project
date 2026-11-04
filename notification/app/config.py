import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://ticketing:ticketing@localhost:5432/ticketing"
    )
    amqp_url: str = os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/")
    consumer_queue: str = os.environ.get("CONSUMER_QUEUE", "ticketing.booking-updates")
    smtp_host: str = os.environ.get("SMTP_HOST", "localhost")
    smtp_port: int = int(os.environ.get("SMTP_PORT", "1025"))
    smtp_timeout_s: float = float(os.environ.get("SMTP_TIMEOUT_S", "5"))
    mail_from: str = os.environ.get("MAIL_FROM", "tickets@citytickets.example")
    queue_depth_poll_s: float = float(os.environ.get("QUEUE_DEPTH_POLL_S", "5"))


settings = Settings()
