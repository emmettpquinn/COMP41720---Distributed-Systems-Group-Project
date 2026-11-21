# Payment and Booking integration

Updated 21 November 2026 against the shared Booking and gateway implementation. This replaces the holds-only review and describes the implemented backend and browser checkout flow.

## Ownership

- Booking owns seats, holds, checkout, confirmation, expiry and compensation decisions.
- Payment owns its PostgreSQL charge/refund ledger, idempotency and payment events.
- Downstream uses `booking_id`; Booking retains `booking_ref` with the same UUID for UI compatibility.
- Hold requests accept optional name/email. Booking falls back to the customer ID and `<customer_id>@example.test`. Payment only needs email.
- Booking owns the current configured EUR 45.00-per-seat price, fixed at hold time. Payment receives the amount and does not calculate it.

## HTTP

Payment accepts `POST /payments` with `booking_id`, positive integer minor-unit `amount`, three-letter `currency` and optional `customer_email`. HTTP 200 contains final status `paid`, `failed` or an already-saved `refunded` result. A declined charge is not an HTTP error. HTTP 503 or a timeout is an unknown outcome.

Repeat the same booking ID/amount/currency to recover the result without charging twice. Changed details return 409. `GET /payments/{payment_id}` reads a known ID; there is no GET by booking ID. `POST /payments/{payment_id}/refunds` is a full idempotent refund.

| Booking endpoint | Behaviour |
|---|---|
| `POST /holds` | Creates a hold/booking with fixed price and customer details |
| `POST /bookings/{id}/checkout` with `{customer_id}` | Commits `payment_pending`, returns 202, then calls Payment in the background |
| `GET /bookings/{id}?customer_id=...` | Returns the booking state, saved `price_minor` and `currency` to its owner |

Payment should not return 202 to match Booking: Booking provides the asynchronous boundary and its client expects Payment's final result. Booking business errors use `{error, message}`; validation/database errors retain FastAPI's `detail`. Breaker refusal is 503 `payment_service_unavailable` and leaves the seat held.

## Retries and late success

Booking makes three attempts with two-second total deadlines. Jitter is 0-250 ms before attempt two and 0-500 ms before attempt three: about 6.75 seconds maximum for the initial call. Its breaker opens after five failed attempts and allows a trial after 30 seconds. HTTP 409/422 is not retried or counted as an outage.

HTTP answers and `payment.result` apply the same conditional Booking transition; whichever arrives second is a no-op. Ordinary hold expiry excludes `payment_pending`. The safety net waits `BOOKING_SAFETY_NET_SECONDS` from checkout start (default 60, minimum 7), then makes one more same-ID call through the same breaker. No answer releases the booking with `payments_unavailable`.

A later `paid` result moves a released booking to `refund_required` and emits `refund.requested`, without changing the seat or emitting another cancellation. No 15-minute reconciliation task exists. For a shortened demo use `PAYMENT_DELAY=10` and safety net 7; record actual timings.

A payment completing after event cancellation is refunded without a ticket. Already-confirmed bookings on an event cancelled later currently retain their ticket.

## Messages and recovery

Messages use `message_id`, `type`, `occurred_at`, `version: 1`, `producer`, `payload`; routing key equals type on `events`.

| Message | Route | Payload |
|---|---|---|
| `payment.result` | Payment -> Booking | `booking_id`, `payment_id`, `amount`, `currency`, `status`; email when known |
| `refund.requested` | Booking -> Payment | `booking_id`, `payment_id`; optional `customer_email` |
| `refund.completed` | Payment -> Booking and Ticketing | Payment fields plus `refund_id`, status `refunded` |
| `refund.failed` | Payment -> Booking and Ticketing | Payment fields plus `reason: provider_rejected`; payment status remains `paid` |

HTTP amount `4500` corresponds to event amount `"45.00"`. Dispatch refund outcomes by message type. Payment validates booking/payment ownership and atomically saves the refund, processed message ID and outgoing event. Both repeated message IDs and new requests for an already-refunded payment are safe.

Temporary errors retry through the shared 1/2/4-second ladder, then park in Payment's DLQ. Booking stays `refund_required`; fix and replay. Terminal rejection emits `refund.failed` once per payment, moving Booking to `refund_pending`. Recovery requires a new request ID through `refund_recovery.py`. Published outbox records are retained; broker confirmation is not consumer acknowledgement.

## Deployment and open boundaries

Root Compose starts Booking, Payment, separate databases, RabbitMQ and topology setup. Catalogue messaging defaults on; Booking demo seeding defaults off. Payment's database has no host port. Monitoring uses `docker-compose.monitoring.yml`, and Booking is already a Prometheus target.

The shared Compose stack now enables Booking holds and checkout in the gateway. The browser sends checkout through `POST /api/booking/bookings/{id}/checkout`, reads status through `GET /api/booking/bookings/{id}?customer_id=...`, and checks tickets through `GET /api/ticketing/tickets/{id}`. It displays Booking's saved EUR price and never sends a price or direct Payment request. Checkout 202 starts polling; a lost response triggers status reads rather than another automatic checkout POST. See the [checkout contract](checkout-preview.md). Ticketing's `payments_unavailable` email still needs its owner's correction because the customer may have been charged. Booking currently ignores direct refunds of confirmed bookings made outside its saga; do not use that as a normal purchase path.

The shared [integration runbook](../integration-development.md#booking-integration-verification-2026-11-19) records real-service purchase, decline and late-success refund checks from 19 November. Those results are previous evidence, not a fresh test run of this documentation update.

See [ADR-004](../adr/ADR-004-idempotent-payments.md), the [Booking contract](booking.md) and [demo guide](../payment-demo.md). The last-seat evidence measures hold correctness, not completed-payment throughput.
