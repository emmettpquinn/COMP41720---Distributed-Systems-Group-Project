"""Prometheus metrics for the Booking service (same style as payment/app/metrics.py)."""
from prometheus_client import Counter, Histogram


REQUESTS = Counter("booking_http_requests_total", "HTTP requests", ["method", "path", "status"])
ERRORS = Counter("booking_http_errors_total", "HTTP errors (status >= 400)", ["status"])
LATENCY = Histogram("booking_http_request_duration_seconds", "HTTP request time", ["method", "path"])
HOLDS = Counter("booking_holds_total", "Hold requests by outcome", ["outcome"])
# Consumed messages, from shared run_consumer's on_outcome hook
# (outcome: processed, retried, dead_lettered).
MESSAGES = Counter("booking_messages_total", "Consumed messages by type and outcome", ["type", "outcome"])

# db.hold_seat's outcome -> the label to show on the dashboard.
HOLD_LABELS = {"held": "held", "already_yours": "retry", "taken": "taken", "not_found": "not_found",
               "event_cancelled": "event_cancelled"}
