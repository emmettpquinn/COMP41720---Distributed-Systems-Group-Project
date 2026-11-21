# ADR-004: Preventing duplicate payments using the booking ID

**Status:** Ready for team review. The code already uses this approach.
**Owner:** John (Payment)
**Date:** 2026-11-20

## The problem

Booking sends a payment request, but sometimes the reply takes too long. Booking then does not know whether the payment failed or succeeded.

If it sends another request, the customer could be charged twice. The same problem can happen with refunds or repeated RabbitMQ messages.

We need a way to retry safely, including after a service restarts.

## Our decision

Use the **booking ID** to recognise repeated payment requests. This is called **idempotency**: repeating the same request has the same effect as sending it once.

For example:

1. Booking requests EUR 45.00 for booking `bk-123`.
2. Payment saves the result, but Booking does not receive the reply.
3. Booking retries using `bk-123` and the same amount and currency.
4. Payment returns the saved result. It does not charge again.

If the same booking ID is sent with a different amount or currency, Payment returns **409 Conflict**. If a payment was declined, a new purchase needs a new booking ID.

## How we store the result

Payment has its own PostgreSQL database with three tables:

| Table | Purpose |
|---|---|
| `payments` | Saves payment/refund results, the customer email when provided, and whether a refund rejection was reported |
| `processed_messages` | Remembers refund-message IDs already handled |
| `outbox` | Saves messages that need sending, with their IDs and sent status |

The database allows only one payment row for each booking ID.

There is also a lock for each booking during a charge, or each payment during a refund. If two requests for the same operation arrive together, one waits and then reads the saved result. Different bookings can still run at the same time.

The fake provider's delay happens outside a database transaction. Once it returns, the payment result and its outgoing message are saved together in one short transaction. Either both are saved or neither is saved. The dedicated connection releases its locks when it closes.

## How refunds avoid duplicates

Payment checks both the refund message ID and the saved payment status.

- The same message ID is not applied twice.
- A different message ID for an already-refunded payment still cannot refund it twice.
- Repeating a completed refund returns the same refund ID.

The refund, processed message ID and message to send are saved together.

## What happens when a payment is slow

Booking replies **202** to checkout, meaning it has accepted the request and is working on it. It calls Payment in the background. Payment's own response contains the final result.

Booking normally makes up to three attempts, with two seconds allowed per attempt and short random waits between retries. After five failed attempts, its **circuit breaker** pauses calls for 30 seconds before allowing a test call. This helps avoid repeatedly calling a service that is struggling.

A seat hold does not expire normally while its payment is in progress. Booking's safety net waits 60 seconds by default, then makes one more request with the same booking ID. If there is still no answer, it releases the seat.

If the payment succeeds after that release, Booking asks Payment for a refund. It does not take the seat back from another customer. The safety net can be set as low as 7 seconds for a demo. There is **no 15-minute payment-checking job** in this version.

Booking owns the price, currently EUR 45.00 per seat, and saves it when the hold is created. HTTP requests use cents (`4500`); RabbitMQ messages use a decimal string (`"45.00"`).

## What happens when a refund fails

| Problem | What happens | How to recover |
|---|---|---|
| Temporary error, such as the provider being down | Retry after 1, 2 and 4 seconds; then put the request in the dead-letter queue if it still fails | Fix the problem and replay that queue |
| Provider rejects the refund | Send `refund.failed` once per payment; Booking becomes `refund_pending` | Fix the cause and send a new request ID using Booking's `refund_recovery.py` command |

The **dead-letter queue** is a waiting area for messages that could not be handled. A temporary failure leaves Booking in `refund_required` until it receives a refund result.

Replaying the same already-processed rejected request does nothing. A new request ID is needed to try again after rejection. A successful refund sends `refund.completed`.

## Why we save outgoing messages

Payment could crash after saving a charge but before telling Booking about it. The outbox prevents that message from being forgotten: it is saved with the payment and sent afterwards.

The sender asks RabbitMQ to confirm that it accepted the message before marking it sent. If Payment crashes between those steps, the message may be sent again with the same ID. The receiving service must recognise the duplicate.

This does not guarantee that every message is delivered only once. It makes repeated delivery safe. Sent outbox rows are kept; there is no cleanup job yet.

## Other approaches we considered

| Approach | Why we did not choose it |
|---|---|
| Give each retry a new payment key | Payment could treat it as another charge |
| Remember results only in memory | A restart would lose them |
| Check for an existing row without a lock or unique rule | Two simultaneous requests could both start a charge |
| Put Booking and Payment in one shared database | Mixes their responsibilities and still does not solve uncertainty about an external provider |

## Limits and checks

This provider is a simulation and moves no real money. If the service crashes before saving a result, a retry may call the simulator again. A real provider would need its own saved duplicate protection and a way to check uncertain payments after a crash. Rolling back our database cannot undo a real external charge.

The current setup assumes one Payment message sender and one Booking instance. The locks need direct PostgreSQL connections; they do not support proxies that swap connections between transactions. Connection and thread limits still need testing under load.

The tests check duplicate payments/refunds, conflicting amounts, saved results, messages, failures and database upgrades. The [demo guide](../payment-demo.md) shows the full payment and refund checks. Save the output of actual runs as evidence. A 500-buyer seat-hold test does not measure 500 completed payments.

We should review this decision if we add a real provider, partial refunds, currencies with different decimal rules, more service instances or a different database connection setup.
