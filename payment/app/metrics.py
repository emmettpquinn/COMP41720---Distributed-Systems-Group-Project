from prometheus_client import Counter, Gauge, Histogram


REQUESTS = Counter("payment_http_requests_total", "HTTP requests", ["method", "path", "status"])
ERRORS = Counter("payment_http_errors_total", "HTTP errors", ["status"])
LATENCY = Histogram("payment_http_request_duration_seconds", "HTTP request time", ["method", "path"])
PROVIDER = Counter("payment_provider_calls_total", "Actual simulated provider calls", ["operation", "outcome"])
PAYMENTS = Gauge("payment_records", "Saved payments by current state", ["status"])
OUTBOX = Gauge("payment_outbox_pending", "Committed messages waiting for broker confirmation")
BROKER = Gauge("payment_broker_connected", "Whether the Payment messaging worker is connected")
PUBLISHED = Counter("payment_events_published_total", "Confirmed event publishes", ["type"])
MESSAGES = Counter("payment_refund_messages_total", "Refund consumer attempts", ["outcome"])
