# Payment service

Owns payment records, makes charge retries safe using the booking ID, and performs full refunds. The fake provider lives in `app/provider.py`. The service owns its ledger and outbox in PostgreSQL, separate from Notification's database.

## Run locally

Start the database from the repository root, then run Python from `payment`:

```powershell
docker compose up -d payment-db
cd payment
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8004
```

The default local database URL is `postgresql://payment:payment@127.0.0.1:5434/payment`. Set `DATABASE_URL` to use another PostgreSQL server. Compose uses its internal `payment-db:5432` address automatically. No `.env` file is required, and PostgreSQL is provided by Docker.

Open http://localhost:8004/docs. With no `AMQP_URL`, HTTP works and new events stay in the outbox until messaging is enabled. For local RabbitMQ support, run from the repository root instead:

```powershell
$env:AMQP_URL = "amqp://guest:guest@localhost:5672/"
$env:PYTHONPATH = "$PWD\shared"
python -m uvicorn app.main:app --app-dir payment --port 8004
```

## Docker and dashboard

From the repository root, stop any local Payment server on port 8004 first:

```powershell
docker compose up -d --build
```

To run only Payment and monitoring (and their broker dependencies):

```powershell
docker compose up -d --build payment prometheus grafana
```

- Payment API: http://localhost:8004/docs
- Metrics: http://localhost:8004/metrics
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000, login `admin` / `stagedoor-demo`
- Dashboard: Grafana > Dashboards > Stagedoor > Stagedoor Payment

Set `GRAFANA_PASSWORD` before first startup to change the demo password. Payment has a `/ready` check for its database and, when enabled, broker connection. `/health` only checks the process.

The `payment-db` named volume stores PostgreSQL data across container restarts. The broker and monitoring also have named volumes. Normal `docker compose down` preserves volumes; `down -v` deletes them. Payment no longer uses the old SQLite files or `payment-data` volume. Legacy SQLite files are backed up outside the project before cleanup; no records are automatically imported from them. Existing Docker volumes are not deleted by this code update.

## Proposed Booking HTTP contract

These fields are a proposed version 1 contract to review with Erwan. The existing shared routing keys are used unchanged.

- `POST /payments`: `{"booking_id":"bk-1","amount":4500,"currency":"EUR","customer_email":"buyer@example.com"}`.
- `GET /payments/{payment_id}`: read the saved result.
- `POST /payments/{payment_id}/refunds`: request a full refund, no body.

Amounts in HTTP are integer minor units: 4500 means EUR 45.00. Use EUR/two-decimal currencies only in this version. Results contain `payment_id`, `booking_id`, `amount`, `currency`, `status`, `refund_id`, `created_at`. Status is `paid`, `failed` or `refunded`. A provider decline returns 200 with status `failed`: Booking must inspect the status. A timeout is an unknown outcome, not a decline.

After a timeout or 503, Booking retries the same booking ID, amount and currency. It gets the saved result without calling the provider again. A changed amount/currency gives 409. A declined booking stays declined; a new purchase needs a new booking ID. Payment never allocates seats or confirms bookings. Booking must make cancellation/refund decisions for expired holds.

Errors: 404 unknown payment, 409 conflicting request/non-refundable payment/terminal refund rejection, 422 bad input, 503 temporary provider/database unavailability. A rejected refund leaves the payment paid and adds a `refund.failed` event. A temporary refund failure leaves no completed result and must be retried.

## Simulator controls

Set variables before starting or restarting the process:

```powershell
$env:PAYMENT_MODE = "ok"       # ok, decline, down
$env:PAYMENT_DELAY = "5"       # seconds, 0 to 10
$env:PAYMENT_ERROR_RATE = "0.5" # probability, 0 to 1; 0.5 means 50%
```

Compose reads the same environment variables. After changing them:

```powershell
docker compose up -d --force-recreate payment
```

`ok` normally succeeds; `decline` declines new payments and terminally rejects refunds; `down` makes new provider calls temporarily unavailable. The error rate injects temporary errors in both payment and refund calls, after the delay. Use 0 or 1 for deterministic tests. Duplicate completed operations bypass the provider and all injected errors. An error rate of 0.5 is probabilistic, not a guarantee that exactly half of requests fail.

## RabbitMQ and durable publication

Uses the existing `shared/messaging` package, `events` exchange and shared 1/2/4-second retry/DLQ mechanism. Consumers only acknowledge after the refund transaction commits. The outbox publisher uses persistent messages, publisher confirms and mandatory routing. It marks a message sent only after confirmation. A crash before that update may republish the SAME message ID; consumers must deduplicate.

Each new payment creates `payment.result`. Each first successful refund creates `refund.completed`. A terminal provider refund rejection creates `refund.failed` (once per payment). State and outgoing messages commit together. Imports preserve existing outbox IDs and sent flags rather than creating new historical events.

Consumes `refund.requested` from `payment.refund-requests`. Payload:

```json
{"booking_id":"bk-1","payment_id":"payment UUID","customer_email":"buyer@example.com"}
```

`booking_id` must match the saved payment. Email is optional; the original charge's email is used as a fallback. Include it on charge/refund requests so Notification can send emails even when cancellation precedes ticket creation.

Publishes the shared envelope (`message_id`, `type`, `occurred_at`, `version: 1`, `producer: payment`, `payload`). Result payloads have `booking_id`, `payment_id`, `amount`, `currency`, `status`; refunds add `refund_id` when successful, and rejection adds `reason: provider_rejected`. `customer_email` is included when known. For Notification compatibility, event `amount` is a DECIMAL STRING, e.g. `"45.00"`; HTTP remains integer cents. For `refund.failed`, status is the payment's current state (`paid`); the message type describes the failed refund. Agree these payload choices with Erwan before connecting Booking.

A repeated refund message ID is recorded in the same transaction as the state and outbox. A different message ID for an already-refunded payment also cannot refund twice. A terminal rejection is acknowledged and emits `refund.failed`; after the cause is resolved, recovery requires a NEW request ID (or a new HTTP refund call). Replaying the same processed message is a no-op. Temporary errors raise and follow shared retries; exhaustion parks the request in `payment.refund-requests.dlq`, without pretending the refund is permanently rejected. Fix the simulator then replay:

```powershell
docker compose exec payment python -m messaging.replay payment.refund-requests --list
docker compose exec payment python -m messaging.replay payment.refund-requests
```

A successful publisher confirm means broker acceptance, not that Booking or Notification has handled the event.

## Metrics

The provisioned dashboard shows HTTP rate/errors/p95 latency, payment records by state, actual provider payment/refund calls by outcome, pending outbox events, broker connection, consumer attempts and published events. Duplicate requests do not increase provider call counts. Database record/outbox gauges survive process restarts; counters reset per process. Route templates are used instead of IDs or email labels.

## Tests

Start `payment-db` first (`docker compose up -d payment-db` from the root). From `payment`:

```powershell
python -m unittest discover -s tests -v
```

Every database test uses a uniquely created temporary PostgreSQL schema, removed after that test. Existing application data is not cleared. Set `PAYMENT_TEST_DB` to use a different test database; the default is the same local URL on port 5434. The database user must be allowed to create schemas. The suite covers HTTP duplicate protection, changed requests, refunds, persistence, validation, metrics, failures, concurrent requests, message duplicates, rejected refunds, failed publishes, crash/republication IDs, atomic outbox rollback and automatic three-table migration. It does not substitute for the final Booking saga test.

An optional real-broker test is included and skipped by default. Against a running Compose stack:

```powershell
$env:PAYMENT_TEST_URL = "http://localhost:8004"
$env:PAYMENT_TEST_AMQP = "amqp://guest:guest@localhost:5672/"
python -m unittest discover -s tests -v
```

Use normal simulator settings (`ok`, zero delay/error rate) for these tests. The broker test creates a uniquely named test booking/refund and a temporary event queue. It does not need the Booking service.

Verified on 10 November 2026 against PostgreSQL 16: all 25 tests passed, including live RabbitMQ requests, duplicate provider-call prevention, unrelated-booking concurrency automatic five-to-three-table migration. Separate live checks verified Payment and PostgreSQL restarts with unsent outbox records, broker recovery, Prometheus/Grafana data and the existing Notification refund email.

Reliability reference: [RabbitMQ publisher confirms and consumer acknowledgements](https://www.rabbitmq.com/docs/confirms). Metric-label reference: [Prometheus naming guidance](https://prometheus.io/docs/practices/naming/).

## Database queries

From the repository root, open PostgreSQL's query tool:

```powershell
docker compose exec payment-db psql -U payment -d payment
```

```sql
\dt
SELECT payment_id, booking_id, amount, currency, status, refund_id FROM payments;
SELECT * FROM payments WHERE booking_id = 'bk-1';
SELECT * FROM payments WHERE status = 'refunded';
SELECT message_id, body FROM outbox WHERE NOT sent ORDER BY id;
SELECT * FROM processed_messages;
\q
```

There are exactly three tables:

| Table | Purpose |
|---|---|
| `payments` | Payment details, refund ID, optional `customer_email`, and `refund_failed` history flag |
| `processed_messages` | RabbitMQ message IDs already applied |
| `outbox` | Outgoing events awaiting broker confirmation |

`refund_failed` records that a terminal refund-failure event has already been reported; it can stay true after a later successful refund. Use `status` for the current payment state. Existing five-table PostgreSQL databases upgrade automatically on Payment startup: email addresses and failure flags are copied into `payments`, then the two redundant tables are removed in the same transaction. Payment IDs, refunds, message IDs and outbox records are preserved. HTTP amounts remain integer cents and RabbitMQ amounts remain decimal strings. `created_at` is now a timezone-aware PostgreSQL timestamp, returned in ISO format.

## Concurrency and remaining limits

Payment takes a PostgreSQL session advisory lock per booking during a new charge, or per payment during a refund. Same-operation duplicates wait and then read the saved result; different bookings can proceed in parallel. Provider delays run with autocommit and no open transaction. Only the final ledger/outbox changes share a short atomic transaction. The dedicated connection closes and releases its locks on success or failure. Use direct PostgreSQL connections; transaction-pooling proxies are not supported by this session-lock design. See [PostgreSQL advisory locks](https://www.postgresql.org/docs/16/explicit-locking.html#ADVISORY-LOCKS) and [Psycopg transaction management](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).

The simulator has no external financial side effects. If it crashes before committing, a retry may call the simulator again but no second ledger charge is created. A real provider needs its own durable idempotency and reconciliation: a PostgreSQL rollback cannot undo an external charge. No real money is moved. Keep one outbox publisher process for now; a crash may still cause safe at-least-once redelivery of an event. The 500-buyer scenario and final Booking saga are not yet load-tested. Connection/thread limits and Booking's retry budget must be tuned together. Final Booking integration, timing budget, the late-success demo and a Payment ADR remain.
