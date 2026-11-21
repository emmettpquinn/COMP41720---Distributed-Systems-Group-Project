# Payment service

This service pretends to take payments and give full refunds. It saves the results in PostgreSQL so retrying the same booking does not charge it twice. No real money is used.

Booking handles seats, reservations and checkout. Payment handles payment records and refunds. The services talk through HTTP requests and RabbitMQ messages.

More detail is in the [integration guide](../docs/contracts/payment-integration.md), [Payment ADR](../docs/adr/ADR-004-idempotent-payments.md) and [demo guide](../docs/payment-demo.md).

## Start the project

Open a terminal in the main Group folder and run:

```powershell
docker compose up -d --build
```

To include the monitoring dashboard:

```powershell
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d --build
```

| Page | Address |
|---|---|
| Booking website | http://localhost:8080 |
| Try Payment requests | http://localhost:8004/docs |
| Try Booking requests | http://localhost:8002/docs |
| View test emails | http://localhost:8025 |
| Prometheus, with monitoring running | http://localhost:9090 |
| Grafana dashboard, with monitoring running | http://localhost:3000 |

The default Grafana login is `admin` / `stagedoor-demo`.

Docker sets the database and RabbitMQ addresses. You do not need a `.env` file or a separate PostgreSQL download. In the shared project, the Payment database is only reachable inside Docker. The Python default address on port 5434 will not connect to it from your computer. Use the Docker commands in this README.

## Payment requests

| Request | What it does |
|---|---|
| `POST /payments` | Takes a simulated payment or returns its saved result |
| `GET /payments/{payment_id}` | Looks up a payment; returns 404 if it does not exist |
| `POST /payments/{payment_id}/refunds` | Gives a full refund or returns the existing refund |

Example payment:

```json
{"booking_id":"booking UUID","amount":4500,"currency":"EUR","customer_email":"buyer@example.test"}
```

Amounts in HTTP requests are in cents: `4500` means EUR 45.00. RabbitMQ messages send the same amount as `"45.00"`. This version is intended for currencies with two decimal places.

Booking sets the price, currently EUR 45.00 per seat. It saves the price when the seat is held. Payment uses that saved amount and does not choose the price.

The response includes the payment ID, booking ID, amount, currency, status, refund ID and creation time. Status is `paid`, `failed` or `refunded`.

- **200:** the request was handled. Check the status: a declined payment returns `failed` even though the HTTP code is 200.
- **409:** details conflict with an existing payment, the payment cannot be refunded, or the provider rejected the refund.
- **422:** the request contains invalid or missing fields.
- **503 or a timeout:** the result is uncertain. The payment might still finish.

Retry using the same booking ID, amount and currency. If the result is already saved, it is returned without taking payment again. Changing the amount or currency for that booking returns 409. After a declined payment, a new purchase needs a new booking ID.

If you never received the payment ID, repeat `POST /payments` with the same details. There is no separate lookup by booking ID.

## How Booking uses Payment

1. Booking receives a checkout request and replies with 202, meaning payment is being processed.
2. Booking calls Payment in the background. Payment returns its final result.
3. Booking checks the result and confirms or releases the seat. The client checks the booking status through `GET /bookings/{id}?customer_id=...`.

Booking tries Payment up to three times, allowing two seconds per attempt and adding short random waits between retries. After five failed attempts, its circuit breaker pauses calls for 30 seconds before trying again. Payment also sends a `payment.result` message. Booking safely handles receiving both the HTTP result and the message.

A seat hold does not expire normally while payment is in progress. Booking's safety net waits 60 seconds by default, then makes one more same-ID call. If there is still no answer, it releases the seat. If payment succeeds later, Booking requests a refund instead of taking the seat back. The safety-net setting can be lowered to 7 seconds for the demo. There is no 15-minute payment-checking job.

If payment finishes after an event is cancelled, it is refunded. A booking confirmed before a later cancellation currently keeps its ticket.

The shared Docker setup now supports checkout in the browser at http://localhost:8080. Reserve a seat, choose **Pay and complete booking**, and the page checks Booking until it gets a result. After confirmation, choose **Check ticket**. Booking sends the payment request; the browser does not call Payment directly. The booking-status reply includes `price_minor` and `currency` from the price saved when the hold was created. See the [checkout guide](../docs/contracts/checkout-preview.md).

## Messages and refunds

Payment listens for `refund.requested` messages on `payment.refund-requests`. Each request needs the booking ID and payment ID. Email is optional. The booking must match the saved payment.

Payment sends:

- `payment.result`: a charge succeeded or failed.
- `refund.completed`: a refund succeeded.
- `refund.failed`: the provider rejected a refund. The payment itself still has status `paid`.

Messages use the shared version-1 format and `events` exchange. The payment change and the message to send are saved together. The sender waits for RabbitMQ to confirm receipt before marking a message as sent. A crash can cause another copy to be sent, so the receiving service must ignore duplicates. RabbitMQ accepting a message does not mean another service has finished handling it.

For a temporary refund problem, the request is retried after 1, 2 and 4 seconds. If it keeps failing, it goes to `payment.refund-requests.dlq` for later replay. If the provider rejects the refund, Payment sends `refund.failed` once per payment. Trying again after a rejection needs a new message ID; Booking has a `refund_recovery.py` command for this. The [demo guide](../docs/payment-demo.md) covers both cases.

## Database

There are three tables:

| Table | What it stores |
|---|---|
| `payments` | Payments, refunds, optional email and whether a refund rejection was reported |
| `processed_messages` | Refund-message IDs already handled, so duplicates are ignored |
| `outbox` | Messages saved for sending and whether RabbitMQ accepted them |

The `refund_failed` flag is a history marker. It can stay true after a later refund succeeds. Check `status` to see the current result.

Old five-table PostgreSQL databases are updated automatically without losing their records. Payment no longer uses SQLite. Sent messages are kept in the outbox; there is no cleanup job yet.

Open the database from the Group folder:

```powershell
docker compose exec payment-db psql -U payment -d payment
```

```sql
\dt
SELECT payment_id, booking_id, amount, currency, status, refund_id FROM payments;
SELECT message_id, body FROM outbox WHERE NOT sent ORDER BY id;
SELECT * FROM processed_messages;
\q
```

`\dt` lists tables. `\q` exits the database tool.

## Test delays and failures

Set these in PowerShell before restarting Payment:

```powershell
$env:PAYMENT_MODE = "ok"       # ok, decline or down
$env:PAYMENT_DELAY = "0"       # seconds, 0 to 10
$env:PAYMENT_ERROR_RATE = "0"  # probability, 0 to 1
docker compose up -d --no-deps --force-recreate payment
```

| Setting | Meaning |
|---|---|
| Mode `ok` | New payments and refunds normally succeed |
| Mode `decline` | New payments are declined and refunds are rejected |
| Mode `down` | New payment/refund attempts temporarily fail |
| Delay `10` | Adds ten seconds to each new charge or refund |
| Error rate `0.5` | Gives each new provider attempt a 50% chance of temporary failure |

Saved completed results still work when these failure settings are on. For the late-payment demo, use Payment delay 10 and Booking safety net 7, as shown in the demo guide.

## Run the tests

Start the project first, with normal mode, zero delay and zero error rate. Run this from the Group folder:

```powershell
docker compose run --rm --no-deps `
  -e PAYMENT_TEST_DB=postgresql://payment:payment@payment-db:5432/payment `
  -e PAYMENT_TEST_URL=http://payment:8000 `
  -e PAYMENT_TEST_AMQP=amqp://guest:guest@rabbitmq:5672/ `
  -v "${PWD}/payment/tests:/srv/tests:ro" `
  payment python -m unittest discover -s tests -v
```

This runs the 25 tests inside Docker, where the database and broker are reachable. Database tests use separate temporary areas called schemas. The live RabbitMQ test creates a test payment and refund. Save your actual test output for the submission.

Booking's 500-buyer test checks seat holds, not 500 completed payments. Demo timings can vary between runs.

## Health checks and dashboard

- `/health`: the Payment process is running.
- `/ready`: its database and configured RabbitMQ connection are working.
- `/metrics`: numbers used by Prometheus and Grafana, including requests, errors, response times, payments, refunds and messages waiting to be sent.

## Current limits

PostgreSQL locks stop two requests for the same booking/payment from running that operation at once. Other bookings can still run. The fake provider delay happens before the short database transaction that saves the result and its message. This lock design needs a direct database connection and does not support proxies that swap connections between transactions.

The system currently assumes one Payment message sender and one Booking instance. The fake provider does not move real money. A real provider would need its own duplicate protection and a way to recover uncertain results after a crash.

The current root Compose file has no RabbitMQ data volume. Message survival when the broker container is replaced therefore still needs a shared setup decision. See the ADR for the full design and trade-offs.
