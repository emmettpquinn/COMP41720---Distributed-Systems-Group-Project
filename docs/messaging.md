# Messaging: broker topology and how to use it

- **Topology:** Emmett, in [shared/messaging/topology.py](../shared/messaging/topology.py).
- **Message contracts (payload fields and versions):** Erwan. The payload fields listed below are only what Ticketing reads today. Treat them as input to the contracts, not the contracts themselves.

## Topology

- **Exchange:** `events` (topic). The routing key is the message type.
- **Queues:** each consumer queue `Q` gets `Q.retry.1`/`.2`/`.3` (TTL 1 s, 2 s, 4 s, which then route back to `Q`) and `Q.dlq`. `Q` also dead-letters to `Q.dlq` through the `dlx` exchange on a broker-side reject.

| Consumer queue | Owner | Bound to |
|---|---|---|
| `catalogue.seat-availability` | Catalogue | `seat.availability-changed` |
| `booking.event-changes` | Booking | `event.changed` |
| `booking.payment-updates` | Booking | `payment.result`, `refund.completed`, `refund.failed` |
| `discovery.event-changes` | Discovery | `event.changed` |
| `payment.refund-requests` | Payment | `refund.requested` |
| `ticketing.booking-updates` | Ticketing | `booking.confirmed`, `booking.cancelled`, `refund.completed`, `refund.failed` |

To add a queue or binding, edit `QUEUES` in `topology.py` and tell Emmett. RabbitMQ refuses to redeclare an existing queue with different arguments. If you change a queue's arguments, run `docker compose down -v` locally.

## Envelope

Every message is JSON with these fields:

```json
{
  "message_id": "uuid",
  "type": "booking.confirmed",
  "occurred_at": "2026-11-04T10:00:00+00:00",
  "version": 1,
  "producer": "booking",
  "payload": { }
}
```

Set the AMQP `message_id` and `type` properties to the same values. Publish persistent, with publisher confirms. `Envelope.new(...).to_amqp()` does all of this for you.

## Consuming from your service

```python
from messaging import connect_with_retry, run_consumer, Envelope

async def handle(env: Envelope) -> None:
    ...  # must be idempotent: record env.message_id in the same DB transaction

connection = await connect_with_retry(AMQP_URL)
channel = await connection.channel()
await run_consumer(channel, "booking.payment-updates", handle)
```

When `handle` returns, the message is acked. When it raises, the message is retried with backoff and then parked. Copy `shared/messaging` into your image the way [notification/Dockerfile](../notification/Dockerfile) does.

## Payload fields Ticketing reads (input for the contracts)

| Message | Fields |
|---|---|
| `booking.confirmed` | `booking_id`, `customer_email`, `customer_name`, `event_id`, `event_title`, `event_starts_at`, `seat_label` (or `seats` list) |
| `booking.cancelled` | `booking_id`, `reason`, `customer_email` |
| `refund.completed` | `booking_id`, `payment_id`, `amount`, `currency`; `customer_email` optional (taken from the ticket otherwise) |
| `refund.failed` | `booking_id`, `payment_id`; `customer_email` optional |

`reason` is one of `hold_expired`, `payment_failed`, `payments_unavailable`, `event_cancelled` or `refund_required`. For the late-success case in ADR-001, Booking sends `booking.cancelled` with `refund_required`, and the customer is told a refund is on its way.
