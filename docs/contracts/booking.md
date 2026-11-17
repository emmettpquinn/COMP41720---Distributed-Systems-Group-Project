# Booking contracts
 
Owner: Erwan (Booking).
 
Status labels:
- IMPLEMENTED: exists in the code on master.
- ACCEPTED: Booking accepts a teammate's proposal; becomes agreed when their
  document says so too.
- PROPOSED: Booking's design, not yet built.
- OPEN: needs a team decision.
Changes to anything IMPLEMENTED or ACCEPTED are additive only, unless listed
under "Deliberate breaking changes" (§14).
 
---
 
## 1. Conventions — PROPOSED
- Field names: lowercase_with_underscores. Message types use dots and hyphens.
- Times: offset-aware ISO 8601. `occurred_at` is UTC. Event times
  (`starts_at`) are passed through exactly as Catalogue sends them.
- Text inputs are trimmed; a field that is empty after trimming is invalid.
- Length limits:
  - Booking rejects inputs over: IDs 64; `customer_email`, `customer_name` 255.
  - In every message Booking publishes, `event_title` is cut to 200 and
    `seat_label` to 64, so that what Booking sends fits Ticketing's columns
    (Ticketing builds a 255-character email subject from the title).
- Synthetic data only.
## 2. Shared envelope — ACCEPTED (shared/messaging/envelope.py)
Fields: `message_id`, `type`, `occurred_at`, `version`, `producer`, `payload`.
- `version` is the message-schema version; currently 1 for every type.
- The version of the thing described lives in the payload: `event_version`
  (Catalogue events), `sequence` (Booking availability).
- Booking always sends `producer: "booking"` and a UUID `message_id`.
  Booking accepts any non-empty string `message_id` from others.
- Catalogue's availability schema rejects unknown fields, so any new field in
  `seat.availability-changed` is agreed with Catalogue first.
## 3. Topology and delivery — ACCEPTED (owner: Emmett)
Exchange `events` (topic; routing key = message type). Booking consumes:
- `booking.event-changes` ← `event.changed`
- `booking.payment-updates` ← `payment.result`, `refund.completed`, `refund.failed`
Each queue: one attempt plus three retries (after 1, 2 and 4 s), then
`<queue>.dlq`. An unparseable message goes straight to the DLQ.
 
Booking's rules (PROPOSED):
- Uses `shared/messaging/consumer.py`.
- Decides what a message means by its `type`, never by a payload `status`
  field (`refund.failed` carries `status: "paid"`).
- Records each consumed `message_id` in the same transaction as its effect,
  including when the effect is a no-op; a repeated `message_id` does nothing.
- Writes every outgoing message as an outbox row in the same transaction as
  the state change; a relay publishes afterwards (publish, then mark sent,
  with publisher confirms).
- A message about a `booking_id` Booking does not know is logged, counted in a
  metric and acknowledged, not retried.
- Requires Catalogue's messaging to be switched on in compose
  (`CATALOGUE_MESSAGING_ENABLED=true`); otherwise Booking never receives
  `event.changed`.
## 4. States — PROPOSED
 
Seat status (inventory): `free` | `held` | `booked`.
 
Booking status: `held`, `payment_pending`, `confirmed`, `released`,
`refund_required`, `refund_pending`, `refunded`.
 
**Rule for every row:** a trigger acts only if the booking is in the listed
"from" state (and any listed condition holds). Otherwise it does nothing.
 
| # | From | Trigger (and condition) | To | Seat | Published (outbox) |
|---|---|---|---|---|---|
| 1 | — | Hold succeeds (§5) | held; `held_until` = now + 10 min | free → held | availability |
| 2 | held, not expired, event not cancelled | Checkout by the same customer (§6), breaker closed | payment_pending; `checkout_started_at` = now | held | — |
| 3 | held | Checkout while the Payment circuit breaker is open | held (unchanged) | held | — (reply 503 `payment_service_unavailable`) |
| 4 | held | `held_until` passed (sweeper, every 5 s) | released | held → free | availability; `booking.cancelled` (hold_expired) |
| 5 | payment_pending | Payment reports `paid`, event NOT cancelled | confirmed | held → booked | `booking.confirmed` |
| 6 | payment_pending | Payment reports `paid`, event cancelled | refund_required | held → free | availability; `booking.cancelled` (event_cancelled); `refund.requested` |
| 7 | payment_pending | Payment reports `failed` | released | held → free | availability; `booking.cancelled` (payment_failed) |
| 8 | payment_pending | Safety net: `checkout_started_at` + S passed (§9). Booking calls Payment again; if it answers → row 5, 6 or 7; otherwise → | released | held → free | availability; `booking.cancelled` (payments_unavailable) |
| 9 | released | Payment reports `paid` (late success) | refund_required | untouched | `refund.requested` |
| 10 | refund_required or refund_pending | `refund.completed` | refunded | — | — (Payment informs Ticketing directly) |
| 11 | refund_required | `refund.failed` | refund_pending | — | — |
| 12 | refund_pending | Refund recovery command (§10) | refund_pending | — | `refund.requested` with a NEW `message_id` |
| 13 | held | Event cancelled (§7) | released | held → free | availability; `booking.cancelled` (event_cancelled) |
 
"Payment reports" means either Payment's HTTP reply or a `payment.result`
message; both feed the same rows (§9).
 
Notes:
- A hold never expires while a payment is in progress (row 4 applies only to
  `held`). Booking announces a release only once the outcome is known, except
  in row 8, where it releases without knowing. If a charge did happen, row 9
  refunds it; the seat is never re-held.
- Whether the event is cancelled is checked when Payment's answer is applied
  (rows 5/6), not only at checkout. A payment that succeeds after the
  cancellation has been recorded is refunded (row 6). If the cancellation is
  recorded after the payment was confirmed, the booking stays confirmed, like
  any confirmed booking on a cancelled event (OPEN, §13.2).
- Row 8 also recovers from a Booking crash mid-payment, because
  `checkout_started_at` is stored.
- Final states: `confirmed`, `refunded`; `released` is final unless a late
  charge arrives (row 9).
### 4a. Implementation rules — PROPOSED
- Every per-booking transition is one conditional UPDATE on the booking row
  ("… WHERE booking_id = X AND status = Y [AND condition]"), checking that
  exactly one row changed; otherwise the transaction is rolled back and the
  trigger is a no-op. Time conditions use the database clock.
- Exception: row 13 acts on all held bookings of one event at once (see the
  table); any number of rows, including zero, is correct.
- Seat updates are keyed on the booking (`booking_ref = booking_id`) and must
  change the matching rows.
- Row 2 commits BEFORE Payment is called. Payment is never called inside a
  database transaction, and no row lock is held during a Payment call.
- What shares one transaction ("processed `message_id`" applies only when a
  message triggered the row, not an HTTP reply or a timer):
| Row(s) | In one transaction |
|---|---|
| 1 | seat conditional UPDATE, booking INSERT (price, `held_until`, email, name), event sequence lock + increment, availability outbox row |
| 2 | booking UPDATE + `checkout_started_at` (then commit, then call Payment) |
| 3 | nothing is written |
| 4, 7, 8 | booking, seat → free, sequence, availability + `booking.cancelled` outbox rows, processed `message_id` (row 8's Payment call happens before, outside) |
| 5 | booking (+ `payment_id`), seat → booked, `booking.confirmed` outbox row, processed `message_id` |
| 6 | booking (+ `payment_id`), seat → free, sequence, availability + `booking.cancelled` + `refund.requested` outbox rows, processed `message_id` |
| 9 | booking (+ `payment_id`), `refund.requested` outbox row, processed `message_id` |
| 10, 11 | booking, processed `message_id` |
| 12 | booking conditional check (`status = 'refund_pending'`, e.g. increment `refund_attempts`), `refund.requested` outbox row |
| 13 | event status/version, processed `message_id`; `UPDATE bookings … WHERE event_id = E AND status = 'held'` (any count); their seats → free; sequence; one availability outbox row; one `booking.cancelled` outbox row per released booking |
 
- Known risk: the per-event sequence lock (rows 1, 4, 6, 7, 8, 13) makes holds
  on the same event queue behind each other. Measure it in the 500-buyer test
  before relying on it.
### 4b. Upgrade rule — PROPOSED
On first start with the new schema, any seat `held` with no booking row (holds
made by the current hold-only code) is set back to `free`.
 
## 5. POST /holds
IMPLEMENTED (on master via 970258f, which imported 7996674 from
booking/skeleton; see §13.5):
Request: `event_id`, `seat_id`, `customer_id` (required, trimmed, non-empty).
- 201 Seat held. `{ booking_ref, event_id, seat_id, status: "held" }`
- 200 Seat already held by this same customer (a retry); same body,
  existing `booking_ref`.
- 409 Held or booked by someone else, or already booked (even by this
  customer). `{ error: "seat_taken", message: "That seat was just taken.
  We've refreshed the map, pick another." }`
- 404 No such seat for this event.
  `{ error: "seat_not_found", message: "No such seat for this event." }`
- 422 Missing, empty or invalid fields (FastAPI `detail` format).
- 503 Any database error (FastAPI `detail` format).
Also implemented: `GET /health`; `GET /ready` (checks the database, reports
`capabilities: ["holds"]`); an optional demo seed (`BOOKING_DEMO_SEED=true`),
which must be off once the `event.changed` consumer runs (§7).
PROPOSED additions (additive unless noted in §14):
- Optional request fields `customer_email`, `customer_name`. If absent,
  Booking stores `<customer_id>@example.test` and `customer_id`.
- Response adds `booking_id` (same value as `booking_ref`) and
  `booking_status` (the booking's state from §4). `status` keeps meaning
  "this customer holds the seat" and stays `"held"`, because the UI checks it.
- 409 `{ error: "event_cancelled", message }` when the event is cancelled.
  The cancelled check is part of the hold's single conditional update.
- The booking's price is fixed at hold time (§8).
- Length limits from §1 (422 if exceeded; see §14).
- `/ready` capabilities will add "checkout".
Notes:
- The hold is a single conditional update; exactly one caller wins a seat.
  Any lookup afterwards only chooses the reply.
- `booking_id` is the identifier used everywhere downstream; `booking_ref` is
  kept so the current UI keeps working.
- Known limit: worst case 2 s to connect plus 2 s per query can exceed the
  gateway's 3 s timeout; the UI reports that as "outcome unknown".
## 6. Checkout and status — PROPOSED
POST /bookings/{booking_id}/checkout
Request: `{ customer_id }`. Checked in this order:
1. 404 Unknown booking, or `customer_id` does not match.
2. 202 `{ booking_id, booking_status: "payment_pending" }` if already
   `payment_pending` (repeat).
3. 409 `{ error: "event_cancelled", message }` if the event is cancelled.
4. 409 `{ error: "not_checkoutable", message }` if not `held`, the hold has
   expired, or Booking has no event record for the event.
5. 503 `{ error: "payment_service_unavailable", message }` if the breaker is
   open; the booking stays `held` (row 3).
6. Otherwise 202 `{ booking_id, booking_status: "payment_pending" }` (row 2).
Why 202: the gateway allows 3 s and never retries a POST. Booking's Payment
call (planned, §9: about 6.75 s in total) cannot fit inside it, so it runs in
the background.
 
GET /bookings/{booking_id}?customer_id=…
- 200 `{ booking_id, event_id, seat_id, booking_status, held_until, payment_id }`
- 404 Unknown booking, or `customer_id` does not match.
The page polls this. What it shows:
- `held`: "Seat reserved"
- `payment_pending`: "Processing your payment"
- `confirmed`: "Confirmed, your ticket is on its way"
- `released`: "Booking released"
- `refund_required`, `refund_pending`: "A refund is on its way"
- `refunded`: "Refunded"
## 7. event.changed (consumed) — ACCEPTED (Catalogue's proposal)
Payload as in `catalogue/examples/contracts/event-*.json`.
 
Booking stores:
- Per event: `event_id`, `event_version`, `status`, `title`, `starts_at`,
  `timezone`, plus its own availability `sequence`.
- Per seat: `seat_id`, `section`, `label`.
Rules (PROPOSED):
- Apply only an `event_version` newer than the stored one. Equal or older:
  do nothing (but record the `message_id`).
- Any newer snapshot with status `scheduled` creates missing seats from
  `seats[]`, and fills in a missing `label` or `section` on existing seats.
  It never changes a seat's status. This covers `created`, an `updated` that
  overtakes it, and seats left by the demo seed.
- `updated` changes stored metadata only; held and booked seats keep their
  status. Seat-map changes after publication are out of scope (Catalogue
  excludes them too).
- `cancelled` is remembered even if it arrives first, so an older creation
  cannot reopen inventory. Then: new holds and checkouts are refused; held
  bookings are released (row 13); a payment in progress that succeeds is
  refunded (row 6), one that fails is released as usual (row 7).
- Confirmed bookings on a cancelled event keep their valid ticket and are not
  refunded: OPEN (§13.2). The fix, if wanted, is row 6's refund path applied
  to each confirmed booking on that event.
- The demo seed (§5) must be off (`BOOKING_DEMO_SEED=false`) whenever this
  consumer runs; it uses the same event IDs as Catalogue's seed. A seat with
  no event record cannot be checked out (§6, check 4).
## 8. Payment (consumed API) — ACCEPTED (Payment's implementation)
POST /payments
`{ booking_id, amount (integer minor units), currency, customer_email }`
- The response holds the final `paid` or `failed`; a decline is HTTP 200 with
  `status: "failed"`. Booking stores `payment_id`.
- After a timeout or error, Booking calls again with the same `booking_id`;
  if the charge was recorded, Payment returns it, otherwise it charges afresh.
  Either way the outcome is correct. Booking does this only in its background
  call, never in the browser path.
- Payment has no lookup by `booking_id`; "asking again" is always a repeated
  POST, which Payment de-duplicates.
- Messages carry money as a decimal string (`"45.00"`); HTTP uses minor units
  (`4500`).
Price (PROPOSED): a flat synthetic price per seat in Booking's config
(4500, EUR), copied onto the booking at hold time. It never changes for that
booking, because Payment rejects a repeated `booking_id` with a different
amount (409).
 
A `failed` payment releases the booking (row 7). To try again the customer
makes a new hold, which gets a new `booking_id`; Payment keeps the failed
result for the old one.
 
## 9. Payment results and the safety net — PROPOSED
Payment reports each charge in its HTTP reply and as `payment.result`
(written to Payment's outbox in the same transaction as the charge). Booking
feeds both into the same §4 rows; whichever arrives second does nothing.
 
Background call (planned):
- 2 s timeout per attempt, 3 attempts, backoff 250 ms then 500 ms with
  jitter (about 6.75 s in total), through the circuit breaker.
- Circuit breaker: opens after 5 consecutive failures; half-open after 30 s.
- If all attempts fail, the booking stays `payment_pending`. It then moves on
  a `payment.result` message, or by the safety net.
"Answers" (for the background call and the safety net) means HTTP 200 with
`paid` or `failed` → row 5, 6 or 7. Anything else counts as no answer:
timeout, connection refused, open breaker, any 4xx or 5xx. A 409 or 422 is
also logged and counted, since it should never happen (the price is fixed).
 
Safety net (row 8):
- Starts counting from the stored `checkout_started_at`.
- S is a setting; default 60 s.
- Checked at least every 0.5 s (separately from row 4's 5 s sweeper), so it
  fires within S + 0.5 s.
- When it fires, Booking makes ONE more POST /payments (same `booking_id`,
  2 s timeout, through the breaker). No answer → row 8 releases.
- After a Booking crash this call may be the first charge. That is correct.
Demonstrating late success (no teammate code changes; Booking settings only):
- `PAYMENT_DELAY=10` (Payment's maximum), Booking safety net S = 7 s.
- 0 s: attempt 1 takes Payment's per-booking lock; the provider waits 10 s.
  Attempts 1–3 time out at about 2, 4.25 and 6.75 s (3 failures; the breaker
  stays closed).
- About 7–7.5 s: the safety net calls, waits on Payment's lock, times out at
  about 9–9.5 s → row 8 releases.
- 10 s: the charge commits; `payment.result` arrives by about 10.5 s → row 9
  requests a refund. The refund itself also takes about 10 s, because
  PAYMENT_DELAY applies to refunds too.
- Valid range with these values: S + 0.5 s + 2 s < 10 s, i.e. S < 7.5 s, and
  S above about 6.75 s.
## 10. Refund recovery — PROPOSED
Two ways a refund can stall:
1. **Temporary error** (Payment's provider unavailable): Payment retries, then
   parks the `refund.requested` in `payment.refund-requests.dlq` and sends no
   message. The booking stays `refund_required`. Recovery: replay Payment's
   DLQ (`docker compose exec payment python -m messaging.replay
   payment.refund-requests`). This works because Payment did not record the
   request as processed.
2. **Rejection** (`refund.failed`): row 11 → `refund_pending`. Payment records
   the request even when rejected, so replaying it does nothing. Recovery:
   Booking's refund recovery command (row 12) sends a NEW `refund.requested`.
   Payment sends `refund.failed` only once per payment, so a second rejection
   produces no message.
Metrics: count and age of bookings in `refund_required` and in
`refund_pending` (planned dashboard alert when above 0 for more than a few
minutes).
 
## 11. seat.availability-changed (published) — ACCEPTED (Catalogue's proposal)
Payload exactly: `event_id`, `sequence`, `seats_remaining`,
`available_seat_ids` (the full set of free seats). No other fields.
`seats_remaining` equals the number of IDs.
 
Rules (PROPOSED):
- Published whenever an event's free set changes (§4 rows 1, 4, 6, 7, 8, 13),
  and once when Booking first creates its record for an event from
  `event.changed` (whether or not its seats already existed). Until then,
  Catalogue shows availability as unknown.
- `sequence` starts at 1 per event. It is incremented in the same transaction
  as the seat change and the outbox row, and the snapshot is computed after
  taking the sequence lock. It is never reset.
- For a cancelled event the snapshot is published as normal; Catalogue keeps
  the event shown as cancelled.
- Catalogue rejects (retries, then dead-letters) a snapshot for an event or
  seat it does not know. Booking only publishes for events it learned from
  Catalogue.
- PROPOSED: a Booking database reset is supported only as a full stack reset
  (`docker compose down -v`).
## 12. Messages Booking publishes to Ticketing and Payment
ACCEPTED: fields as Ticketing reads them today (`docs/messaging.md`,
`notification/app/handlers.py`) and as Payment validates `refund.requested`
(`payment/app/messages.py`). Lengths capped per §1.
 
booking.confirmed
`booking_id`, `customer_email`, `customer_name`, `event_id`, `event_title`,
`event_starts_at`, `seat_label`
(`event_title` and `event_starts_at` from §7; `seat_label` = the seat's
`label`).
 
booking.cancelled
`booking_id`, `reason`, `customer_email`, plus optional `customer_name` and
`event_title` (used by Ticketing when it has no ticket yet).
`reason` ∈ `hold_expired` | `payment_failed` | `payments_unavailable` |
`event_cancelled`
- `payments_unavailable` means "the payment could not be confirmed"; the
  customer may or may not have been charged. Ticketing's email for it should
  say so (§13.7).
- `refund_required` is no longer sent: a late success is refunded without a
  second cancellation (§4).
refund.requested
`booking_id`, `payment_id`, `customer_email` (optional). Version 1.
 
## 13. Open questions
1. One error-body shape for all responses: `{error, message}` vs FastAPI
   `detail`.
2. Confirmed bookings on a cancelled event: refund them (row 6's path), or
   leave them valid?
3. Pricing owner (flat config price meanwhile).
4. Gateway routes for checkout and status, the `checkout` capability, and UI
   for checkout, polling and the 503 (gateway owner).
5. Merge `booking/skeleton` into master to link the hold's commit history.
6. A refund made directly in Payment on a confirmed booking: Booking ignores
   it, so the seat stays booked while Ticketing voids the ticket. Handle, or
   leave (the gateway does not expose Payment)?
7. Ticketing: reword the `payments_unavailable` email
   (notification/app/emails.py:10), which currently says "please try again"
   although the customer may have been charged.
8. Dev compose: `BOOKING_DEMO_SEED` defaults to on (docker-compose.dev.yml);
   it must default to off once Booking's consumer runs (§7).
## 14. Deliberate breaking changes — PROPOSED
- IDs longer than 64 characters are rejected with 422 (today they are
  accepted). Agreed by Booking's owner, because Ticketing and Payment cannot
  store them.
## 15. Tests that will change (booking/tests/test_integration.py)
- Exact `hold_seat()` return tuples (lines 65, 86, 87) if its return value
  changes.
- Seat inserts without `label` or an event record (line 47, demo_seed.py:17)
  if those become required.
- `/ready` capabilities `['holds']` (line 104) when checkout is added.