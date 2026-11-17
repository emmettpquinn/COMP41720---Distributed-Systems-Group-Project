# Payment integration review

Reviewed source: `origin/feature/payment-service` at `63b8b0e` on 2026-11-14.
The implementation is now imported into `feature/catalogue-integration`; the
source branch and shared envelope/topology are unchanged. This review closes the
code-inspection part of INT02. Booking acceptance remains pending.

## Implemented interface

- `POST /payments`: stable `booking_id`, positive integer minor-unit `amount`,
  three-letter `currency`, optional `customer_email`. A successful HTTP request
  returns the final simulated `paid` or `failed` result. A decline is HTTP 200
  with `status: failed`; inspect the body.
- `GET /payments/{payment_id}` returns the saved result. Repeating a charge with
  the same booking/amount/currency reuses the result; changed amount/currency is
  409. Unknown outcome recovery retries the same booking ID.
- `POST /payments/{payment_id}/refunds` requests a full refund. Refunds and
  duplicate request IDs are recorded with their outgoing message atomically.
- Messages use the existing envelope/version 1. `payment.result`,
  `refund.completed` and `refund.failed` use decimal-string amounts such as
  `"45.00"`, while HTTP uses `4500`. The refund consumer expects `booking_id` and
  `payment_id`; email can fall back to the saved charge.
- `/health` is process liveness; `/ready` checks database and enabled broker.
  `/metrics` and the imported dashboard report actual Payment activity.

## Fit and decisions still needed

The team plan's immediate accepted response with asynchronous completion differs
from the implemented final-result HTTP response. The simulator is in-process;
there is no separate provider container or durable real-provider reconciliation.
Booking must agree its timeout/retry budget, unknown-outcome lookup strategy,
late success/refund path and how a hold reference becomes a booking ID.

Catalogue deliberately has no price/amount authority in its candidate contract.
Pricing ownership must be agreed before checkout. The current Booking source has
only `POST /holds`, no checkout/status API, expiry worker or payment consumer.
Consequently this branch exposes no browser payment/checkout route. Payment's
successful ledger result alone does not mean a booking is confirmed or a ticket
has been issued.

Payment's refund fields fit Ticketing's existing decimal amount/email handler.
The live Payment broker test exercises duplicate refund requests and final ledger
state. Full Booking saga timing and Ticketing email acceptance still need a joint
test with the real Booking workflow.

## Controlled integration changes

- Payment application, SQL, Dockerfile and tests are copied from `63b8b0e`.
  README references to individuals were replaced with service-role wording.
- Root Compose adds `payment-db` on the private network, preserves existing
  Catalogue/Ticketing definitions, and runs the real Payment service on port 8004.
  No Payment database host port is published.
- Payment dashboard JSON is unchanged, stored under its own `payment/` directory;
  the provisioning path is narrowed to avoid scanning Catalogue's dashboard twice.
- Payment is a fifth monitoring target. Payment failure controls are unchanged.
- The upstream broker-volume change is deferred: this integration does not migrate
  or replace any existing broker storage. Container recreation persistence still
  needs a coordinated broker decision.

No teammate branch was modified and no merge commit was created. A later merge
of upstream service work must reconcile these already-imported paths explicitly.
