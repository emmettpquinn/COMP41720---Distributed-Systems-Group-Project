# ADR-005: At-least-once delivery with idempotent consumers and dead-letter queues, instead of exactly-once

**Status:** proposed (draft for the Week 10 check)
**Owner:** Emmett Quinn
**Date:** 2026-11-04

## Context

Eight message types cross RabbitMQ between five services. Money and tickets ride on several of them: `booking.confirmed` issues a ticket, and `refund.completed` voids one. The broker, a consumer or a downstream dependency (the mail server) can fail at any moment, and each must be survivable without losing a message or acting on one twice.

RabbitMQ does not offer end-to-end exactly-once delivery. Its guarantees stop at the broker. A consumer that crashes after acting on a message but before acking it will see the message again. Even a broker that could do exactly-once could not make SMTP, which sits outside every transaction, happen exactly once.

**Facts that would reopen this decision:**

- A move to a broker with transactional consume-and-produce (Kafka transactions), *and* every side effect living inside the same transactional store.
- Duplicate-check lookups showing up as a bottleneck. At on-sale volume (500 buyers) they are one primary-key read per message.
- Messages parked in the DLQ more than about once a day in normal running. That would mean the retry budget is wrong, not that the design is.

## Decision

Emmett Quinn sets the delivery model for every queue as follows:

1. **At-least-once delivery.** Producers publish persistent messages with publisher confirms through an outbox (Booking, Payment, Catalogue). Consumers ack only after their work is committed.
2. **Idempotent consumers.** Every message carries a `message_id`. A consumer records it in its own database *in the same transaction as the state change*, and ignores any message whose ID it has already recorded. A unique constraint backs every invariant: one ticket per booking (Ticketing), one payment per booking (Payment).
3. **Bounded retries, then park.** A failed message is retried after 1 s, 2 s and 4 s through per-delay retry queues. After that it goes to `<queue>.dlq`, shows on the dashboard, and is replayed by hand (`python -m messaging.replay <queue>`) once the cause is fixed.
4. **External side effects are claimed, then recorded.** Ticketing records each email it owes as a row. A worker claims it (`pending → sending`, with a 60 s lease), sends it, and marks it `sent`. A retry or replay finds the ticket already issued but the email still pending, and sends only the email.

## Trade-off analysis

| Option | Correctness | Complexity | Fit for us |
|---|---|---|---|
| At-most-once (ack on receipt) | Loses tickets on any crash | Lowest | Rejected: breaks "no one charged without a ticket" |
| **At-least-once + idempotent consumers + DLQ (chosen)** | No loss; duplicates absorbed by IDs and constraints | One table and one check per consumer | Works with RabbitMQ, testable, and explainable in the viva |
| Exactly-once via broker transactions | Only inside the broker; SMTP and the provider are still outside | High; needs a different broker | Rejected: does not cover the side effects that matter |
| Infinite retry, no DLQ | No loss, but a poison message blocks or loops forever | Low | Rejected: hides failures, so nothing appears on the dashboard |

## Consequences

- **Positive (correctness: one ticket per booking).** Tested: 100 redeliveries of one `booking.confirmed` on the running stack produced 1 ticket, 1 email and 99 recorded duplicates.
- **Positive (resilience: survive injected failure).** With MailHog stopped, tickets are still issued. Emails park in the DLQ, the dashboard shows the depth, and after replay each customer gets exactly one email.
- **Positive (ordering).** Because duplicates are safe, consumers don't need in-order delivery. Ticketing makes ticket state monotonic (valid → void, never back), so a cancellation arriving before its confirmation can never produce a valid ticket.
- **Negative (one email may be sent twice).** If Ticketing dies after SMTP accepts an email but before it records `sent`, the email is sent again. SMTP has no idempotency key, so this window cannot be closed. It is bounded to one email per crash. Every email carries an `X-Notification-Key` header, so a copy is recognisable.
- **Negative (operations: someone must act).** Parked messages need a person to fix the cause and replay. The dashboard alert and the runbook entry are part of the design, not extras.
- **Negative (storage).** Processed-message records grow with traffic. That is fine at project scale. In production they would be pruned after the longest possible redelivery window (for example 7 days).
