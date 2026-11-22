ARCHITECTURAL DECISION RECORD — ADR-005

# Deliver at least once, and make the Ticketing service idempotent, with retries and dead-letter queues

**Date:** 22 November 2026

**Status:** Accepted

## Context

Ticketing turns four messages into e-tickets and emails: `booking.confirmed` and `booking.cancelled` from Booking, and `refund.completed` and `refund.failed` directly from Payment. Each carries a `booking_id` and a `customer_email`. The forces:

- **Messages arrive more than once.** The producers' outboxes publish before they mark a row sent, and RabbitMQ redelivers anything a crashed consumer had not acked.
- **Two producers race.** Since ADR-001, a payment that never answers is released by Booking as `payments_unavailable`. If the charge lands later, Payment's `refund.completed` arrives about 11 s after that, on its own. Ticketing must never issue a ticket for a released booking, whichever message comes first.
- **Email is outside our control.** SMTP sits outside any transaction, can be down and has no idempotency key.
- **Failures must not block or vanish.** A message that keeps failing must be visible and recoverable.

## Decision

Ticketing accepts at-least-once delivery and removes duplicates itself. It acts only on a message's type and `booking_id`, and never calls Booking or Payment.

**Step 1: the ticket change.** One Postgres transaction records the `message_id`, creates or voids the ticket, and writes any email now owed as `pending`. Unique constraints allow one ticket per booking and one email of each kind per booking. Ticket state only moves from valid to void, so a cancellation or refund that arrives first leaves a void tombstone that a later confirmation cannot overturn.

**Step 2: the email.** Each pending email is claimed (`pending → sending`, 60 s lease), sent, then marked `sent`. A failed send is retried after 1 s, 2 s and 4 s, then parked in `ticketing.booking-updates.dlq` until `python -m messaging.replay` puts it back. On a retry, step 1 sees a duplicate and step 2 sends only what is still pending. Booking and Payment use the same consumer and DLQs (booking contract §3).

## Consequences

### Positive

- **Constraints, not timing, enforce correctness.** Duplicates hit a primary key or a unique constraint, and order does not matter. 100 copies of one `booking.confirmed` gave 1 ticket and 1 email.
- **Ticketing stays off the purchase path.** A Ticketing or mail outage delays the email, never a purchase. Publish to email took **37 / 63 / 155 ms** (min / median / max, 20 bookings). With MailHog stopped, messages were parked, then replayed with one email per customer.
- **The race needs no special case.** On the full stack (22 Nov), a late payment went `released` (9.3 s) → `refunded` (20.5 s). Ticketing left a void tombstone, issued no ticket, and sent one cancellation and one refund email.

### Negative

- **One email can go out twice** if Ticketing dies between SMTP accepting it and marking it `sent`: at most one per crash.
- **The cancellation email must hedge.** At `payments_unavailable`, nobody yet knows whether the customer was charged (booking contract §13.7).
- **Ticketing reflects messages, not the truth across services.** A refund made directly in Payment voids the ticket while Booking still shows the seat booked (§13.6).
- **Someone has to act.** Parked messages wait for a human, parking took 23.2 s rather than 7 s because of slow DNS failures, and `processed_messages` is never pruned.

## Alternatives considered

**Exactly-once delivery from the broker.** RabbitMQ does not offer it, and SMTP would still sit outside. Rejected.

**Ask Booking for the state before acting.** This would settle the races directly, but it couples Ticketing to Booking's availability during an on-sale. Rejected; revisit if the mismatches above become normal paths.

**Send the email inside the ticket transaction.** A mail outage would then stop tickets or lose emails. Rejected.

**An email outbox with no DLQ.** It works, but adds a second retry mechanism outside the DLQ that the dashboard and runbook already watch. Revisit if a real email provider imposes rate limits.
