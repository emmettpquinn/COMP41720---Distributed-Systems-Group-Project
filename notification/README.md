# Ticketing and Notification

**Owner:** Emmett Quinn

Issues an e-ticket for each confirmed booking and voids it on cancellation or refund. Emails customers through MailHog. Makes sure no message is processed twice or silently lost.

## Interfaces

**API**

| Endpoint | Purpose |
|---|---|
| `GET /tickets/{booking_id}` | Ticket status (`valid` / `void`) and code |
| `GET /health` | Liveness |
| `GET /ready` | Readiness: database and broker |
| `GET /metrics` | Prometheus metrics |

**Consumes** `ticketing.booking-updates`: `booking.confirmed`, `booking.cancelled`, `refund.completed`, `refund.failed`.

**Sends** email over SMTP to MailHog (inbox at http://localhost:8025).

## Data (own Postgres)

| Table | Holds |
|---|---|
| `tickets` | One per booking (unique). A void row with no code is a tombstone: the booking was cancelled before a ticket existed. |
| `processed_messages` | Message IDs already applied, written in the same transaction as the ticket change. |
| `notifications` | Emails owed, one per (booking, kind). States: `pending` → `sending` (claimed, 60 s lease) → `sent`. |
| `delivery_log` | Every send attempt and its result. |

How processing works, and why it gives one ticket and one email per booking, is explained at the top of [app/handlers.py](app/handlers.py) and in [ADR-005](../docs/adr/ADR-005-at-least-once-delivery.md).

## Metrics for the dashboard panel

| Metric | Shows |
|---|---|
| `ticketing_messages_total{type,outcome}` | Processed, retried and dead-lettered messages |
| `ticketing_duplicates_ignored_total{type}` | Duplicates recognised and skipped |
| `ticketing_email_errors_total{kind}`, `ticketing_emails_sent_total{kind}` | Email delivery |
| `ticketing_tickets_issued_total`, `ticketing_tickets_voided_total` | Ticket changes |
| `broker_queue_depth{queue,owner,kind}` | Depth of every queue in the topology, including all DLQs |
| `http_requests_total`, `http_request_duration_seconds` | Request rate, errors and latency |

## Tests

```sh
pip install -r requirements.txt aiosqlite pytest pytest-asyncio
cd notification && python -m pytest -q
```

The tests cover 100 redeliveries, concurrent duplicates, a mail outage then replay, a cancellation before its confirmation, refunds, a worker crash mid-send, and retry routing.

For the live-stack demos, see [docs/runbook.md](../docs/runbook.md).
