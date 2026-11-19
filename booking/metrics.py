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

# Checkout and its background Payment call (payments.py, contract sections 6 and 9).
CHECKOUTS = Counter("booking_checkouts_total", "Checkout requests by outcome", ["outcome"])
# One per HTTP attempt to Payment (paid, failed, refunded, timeout, error, server_error,
# client_error, rejected = 409/422, bad_response), plus short_circuited when the breaker
# refused the attempt and no call was made.
PAYMENT_ATTEMPTS = Counter("booking_payment_attempts_total", "Payment HTTP attempts by outcome", ["outcome"])
BREAKER_STATE = Gauge("booking_payment_breaker_state", "Payment circuit breaker: 0 closed, 1 open, 2 half-open")
# What each background call ended with: confirmed, refund_required, released, noop,
# or no_answer (every attempt failed; the booking stays payment_pending).
PAYMENT_ANSWERS = Counter("booking_payment_answers_total", "Background payment calls by result", ["result"])
PAYMENT_APPLY_ERRORS = Counter("booking_payment_apply_errors_total",
                               "Background payment calls that stopped on an error (booking stays payment_pending)")
# Messages on booking.payment-updates (payment_updates.py) by type and what they did:
# confirmed, refund_required, released, refunded, refund_pending, noop, duplicate,
# or unknown_booking (a booking_id Booking does not know: logged, acknowledged).
PAYMENT_MESSAGES = Counter("booking_payment_messages_total", "Payment messages applied, by type and result",
                           ["type", "result"])

# Held bookings released (contract section 4): reason hold_expired (row 4, the
# expiry sweeper) or event_cancelled (row 13). Counted after the commit.
HOLDS_RELEASED = Counter("booking_holds_released_total", "Held bookings released, by reason", ["reason"])
for _reason in ("hold_expired", "event_cancelled"):
    HOLDS_RELEASED.labels(_reason)        # show both at 0 from the start
# Sweeper errors: one per event whose release failed, plus one per failed sweep (expiry.py).
EXPIRY_ERRORS = Counter("booking_expiry_sweep_errors_total", "Expiry sweeper errors (the sweeper keeps running)")

# db.hold_seat's outcome -> the label to show on the dashboard.
HOLD_LABELS = {"held": "held", "already_yours": "retry", "taken": "taken", "not_found": "not_found",
               "event_cancelled": "event_cancelled"}
