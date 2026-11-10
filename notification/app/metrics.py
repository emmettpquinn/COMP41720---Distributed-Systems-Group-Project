"""Prometheus metrics for the Ticketing dashboard panel.

The panel shows messages processed, duplicates ignored, dead-letter queue depth,
email delivery errors, and queue depth per consumer. Ticketing owns the broker
topology, so it also reports the depth of every queue in it, including other
services' DLQs. That gives the dashboard one "messages parked anywhere" view.
"""

from prometheus_client import Counter, Gauge, Histogram

MESSAGES = Counter(
    "ticketing_messages_total",
    "Messages taken off the ticketing queue, by type and outcome",
    ["type", "outcome"],  # outcome: processed | retried | dead_lettered
)
DUPLICATES_IGNORED = Counter(
    "ticketing_duplicates_ignored_total",
    "Redelivered messages recognised by message ID and not applied again",
    ["type"],
)
TICKETS_ISSUED = Counter("ticketing_tickets_issued_total", "E-tickets issued")
TICKETS_VOIDED = Counter("ticketing_tickets_voided_total", "Valid e-tickets voided")
EMAILS_SENT = Counter("ticketing_emails_sent_total", "Emails accepted by SMTP", ["kind"])
EMAIL_ERRORS = Counter("ticketing_email_errors_total", "Email send attempts that failed", ["kind"])

QUEUE_DEPTH = Gauge(
    "broker_queue_depth",
    "Messages ready in each queue of the platform topology",
    ["queue", "owner", "kind"],  # kind: main | retry | dlq
)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests", ["service", "method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["service", "method", "route"]
)
