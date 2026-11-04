"""The message envelope every service publishes and consumes.

DRAFT: Erwan owns the message contracts. This module covers only the
envelope fields that every message carries, as the team plan's rules require:
a message ID, the type, when it happened, and a version or sequence number.
The payload fields of each message belong in the contracts document.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aio_pika


class InvalidEnvelope(ValueError):
    """The message body cannot be parsed. Retrying will never fix this."""


@dataclass(frozen=True)
class Envelope:
    message_id: str
    type: str
    occurred_at: str
    version: int
    producer: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        type: str,
        payload: dict[str, Any],
        producer: str,
        version: int = 1,
        message_id: str | None = None,
    ) -> "Envelope":
        return cls(
            message_id=message_id or str(uuid.uuid4()),
            type=type,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            version=version,
            producer=producer,
            payload=payload,
        )

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "message_id": self.message_id,
                "type": self.type,
                "occurred_at": self.occurred_at,
                "version": self.version,
                "producer": self.producer,
                "payload": self.payload,
            }
        ).encode()

    @classmethod
    def from_json(cls, body: bytes) -> "Envelope":
        try:
            data = json.loads(body)
            return cls(
                message_id=str(data["message_id"]),
                type=str(data["type"]),
                occurred_at=str(data["occurred_at"]),
                version=int(data.get("version", 1)),
                producer=str(data.get("producer", "unknown")),
                payload=dict(data.get("payload") or {}),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidEnvelope(str(exc)) from exc

    def to_amqp(self, headers: dict[str, Any] | None = None) -> aio_pika.Message:
        return aio_pika.Message(
            body=self.to_json(),
            message_id=self.message_id,
            type=self.type,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=headers or {},
        )
