"""Prometheus metrics for the Booking service (same style as payment/app/metrics.py)."""
from prometheus_client import Counter, Gauge, Histogram


REQUESTS = Counter("booking_http_requests_total", "HTTP requests", ["method", "path", "status"])
ERRORS = Counter("booking_http_errors_total", "HTTP errors (status >= 400)", ["status"])
LATENCY = Histogram("booking_http_request_duration_seconds", "HTTP request time", ["method", "path"])
HOLDS = Counter("booking_holds_total", "Hold requests by outcome", ["outcome"])
# Consumed messages, from shared run_consumer's on_outcome hook
# (outcome: processed, retried, dead_lettered).
MESSAGES = Counter("booking_messages_total", "Consumed messages by type and outcome", ["type", "outcome"])
# Transactional outbox (outbox.py): rows the relay will still try to publish,
# rows set aside after a permanent failure (never counted as pending), and
# rows the broker confirmed, by message type.
OUTBOX_PENDING = Gauge("booking_outbox_pending", "Outbox rows not yet published (set-aside rows excluded)")
OUTBOX_FAILED = Gauge("booking_outbox_failed", "Outbox rows set aside after a permanent publish failure")
OUTBOX_PUBLISHED = Counter("booking_outbox_published_total", "Outbox rows published (broker confirmed)", ["type"])

# db.hold_seat's outcome -> the label to show on the dashboard.
HOLD_LABELS = {"held": "held", "already_yours": "retry", "taken": "taken", "not_found": "not_found",
               "event_cancelled": "event_cancelled"}
