"""Shared RabbitMQ topology, envelope, retry/DLQ consumer and replay tool."""

from .consumer import Outcome, run_consumer
from .envelope import Envelope, InvalidEnvelope
from .topology import (
    EVENTS_EXCHANGE,
    QUEUES,
    MessageType,
    all_queue_names,
    connect_with_retry,
    declare_topology,
)

__all__ = [
    "EVENTS_EXCHANGE",
    "QUEUES",
    "Envelope",
    "InvalidEnvelope",
    "MessageType",
    "Outcome",
    "all_queue_names",
    "connect_with_retry",
    "declare_topology",
    "run_consumer",
]
