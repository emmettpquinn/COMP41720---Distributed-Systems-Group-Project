# Gateway checkout integration

Updated: 2026-11-19. The browser now follows the implemented
[Booking contract](booking.md), rather than the earlier simulated API assumptions.
The normal Compose stack enables holds and checkout. The standalone gateway
process keeps its opt-in defaults; both flags must be true to enable checkout.

## Routes and identity

- `POST /api/booking/holds` forwards the existing event/seat/customer request.
- `POST /api/booking/bookings/{booking_id}/checkout` sends `{customer_id}`.
- `GET /api/booking/bookings/{booking_id}?customer_id=...` reads the result.
- `GET /api/ticketing/tickets/{booking_id}` checks a ticket after confirmation.

The synthetic customer ID stays stable in this browser tab. Booking returns the
same 404 for an unknown booking and the wrong customer. `booking_ref` remains
available for compatibility and equals the downstream `booking_id`.

The status reply contains `booking_id`, `event_id`, `seat_id`, `booking_status`,
`held_until` and `payment_id`. The integration adds `price_minor` and `currency`
from the saved booking row. These fields do not recalculate a hold's price after
a configuration change. The UI requires a valid EUR price before enabling payment,
but never sends a price itself. Booking alone calls Payment.

## States and messages

The UI accepts the exact Booking state vocabulary:

- `held`: show the saved total and allow checkout.
- `payment_pending`: show processing and poll; no countdown or second checkout.
- `confirmed`: show confirmation and offer ticket lookup.
- `released`: explain that the booking was not confirmed and continue checking
  for a possible late-payment refund. Do not infer the reason: the status API
  does not expose a cancellation reason.
- `refund_required`: a refund has been requested.
- `refund_pending`: refund has not completed and can need manual recovery.
- `refunded`: show the completed refund; never present it as a valid ticket.

The page may offer another hold after release/refund states; the old seat is
never re-held by late-success processing. A new active booking replaces the tab's
previous panel. The tab stores booking identity/display metadata only and reads
fresh state after reload. It never restores a trusted price or repeats checkout
just because the page reloaded.

## Timing and failures

The gateway keeps its three-second timeout and never automatically retries POST.
Checkout 202 is acceptance of background processing, not payment confirmation.
A lost response or generic 503 causes status reads. Only the explicit business
error `payment_service_unavailable` permits a manual same-booking checkout retry;
Booking leaves that hold intact. Business `{error, message}` and FastAPI `detail`
errors are preserved by the gateway.

A status cycle makes at most 45 sequential reads, two seconds after each completed
read, and stops after three consecutive failures. **Check booking status** starts
another GET cycle. Stopping polling does not cancel or release anything. Booking's
own expiry and safety-net rules decide those transitions.

A Ticketing 404 after confirmation can mean its consumer has not caught up.
The page displays a ticket code only for a matching booking and a valid Ticketing
record. A delayed ticket response cannot display a valid ticket after Booking
has reported release/refund. Confirmed bookings on later-cancelled events keep
valid tickets under the current team decision.

## Verification

From `gateway/`, with its Python and Node dependencies installed:

```sh
python -m unittest discover -s tests -v
npm run test:ui
```

Boundary tests use MockTransport; DOM tests use simulated responses matching the
real API, including owner identity and refund states. They do not claim visual
browser/device verification. The live harness uses real Catalogue, Booking,
Payment, Ticketing, Discovery, RabbitMQ and separate databases:

```sh
python3 tools/verify_development_startup.py --working-tree --failure-cases --output /tmp/checkout-startup.json
```

It checks purchase/ticket/email, availability, owner checks, persistence and
failure cases in a disposable project. Without `--working-tree`, the harness
instead uses a clean archive of committed HEAD. Before committing new gateway
code, that older archive does not contain the pending integration changes.
No full-system 500-buyer or learned-model relevance claim follows from these checks.
