# Booking contracts
 
Owner: Erwan (Booking).
 
Status labels:
- IMPLEMENTED: built and tested.
- ACCEPTED: Booking accepts a teammate's proposal; becomes agreed when their
  document says so too.
- PROPOSED: Booking's design, not yet built.
- OPEN: needs a team decision.
Changes to anything IMPLEMENTED or ACCEPTED are additive only, unless listed
under "Deliberate breaking changes" (§14).
 
---
 
## 1. Conventions — PROPOSED unless labelled
- Field names: lowercase_with_underscores. Message types use dots and hyphens.
- Times: offset-aware ISO 8601. `occurred_at` is UTC. Event times
  (`starts_at`) are passed through exactly as Catalogue sends them.
- IMPLEMENTED: Booking's `occurred_at` comes from Booking's own clock, in
  UTC. In `seat.availability-changed`, `available_seat_ids` is sorted.
- IMPLEMENTED: Text inputs are trimmed; a field that is empty after trimming
  is invalid (422).
- Length limits:
  - IMPLEMENTED: Booking rejects inputs over: IDs 64; `customer_email`,
    `customer_name` 255 (422).
  - IMPLEMENTED: In every message Booking publishes, `event_title` is cut to
    200 and `seat_label` to 64, so that what Booking sends fits Ticketing's
    columns (Ticketing builds a 255-character email subject from the title).
    `seat_label` falls back to the `seat_id` when a seat has no label.
    `event_starts_at` is passed through and not cut.
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
 
Booking's rules (IMPLEMENTED unless labelled):
- Uses `shared/messaging/consumer.py`.
- Decides what a message means by its `type`, never by a payload `status`
  field (`refund.failed` carries `status: "paid"`).
- Records each consumed `message_id` in the same transaction as its effect,
  including when the effect is a no-op; a repeated `message_id` does nothing.
- A bad `event.changed` (unknown `status`, invalid `event_version`, or a
  missing required field) is retried, then dead-lettered. A missing `seats`
  list counts as no seats. Any other message type on `booking.event-changes`
  is logged and acknowledged.
- Writes every outgoing message as an outbox row in the same transaction as
  the state change; a relay publishes afterwards (publish, then mark sent,
  with publisher confirms). Holds never wait for the relay.
- Outbox: the envelope is stored once and never changed. The relay polls
  about every 0.25 s, takes up to 50 unsent rows, oldest first, and
  publishes each with publisher confirms and mandatory delivery (5 s confirm
  timeout), then marks it sent. A temporary failure (below) leaves the row
  unsent for a later try. Delivery is at-least-once. One relay instance is
  assumed; a second Booking replica would send duplicates.
- IMPLEMENTED: two kinds of publish failure.
  - Permanent: only a message the broker returned as unroutable (mandatory
    delivery). The row is set aside (marked failed with `failed_at` from the
    database clock and an error text of at most 500 characters), logged, and
    the relay moves on to the next rows, so one bad message cannot block the
    others. A row is set aside only if it is still unpublished and not
    already set aside. A set-aside row is never retried or published.
    Set-aside rows do not count in `booking_outbox_pending`; a separate
    gauge, `booking_outbox_failed`, counts them (§11). How they are
    recovered is OPEN (§13.11).
  - Temporary: everything else (a nack, a confirm timeout, a lost connection
    or channel, any other error). The row stays unsent and the relay retries
    it, oldest first, as above; the rows after it wait.
- IMPLEMENTED: `booking.payment-updates` is dispatched by type:
  `payment.result` → §4 rows 5–7, `refund.completed` → row 10,
  `refund.failed` → row 11. A malformed message (payload not an object, a
  missing or invalid `booking_id`; for `payment.result` also an unknown
  `status` or a missing or invalid `payment_id`) is retried, then
  dead-lettered. Any other type is logged and acknowledged, and its
  `message_id` is not recorded.
- IMPLEMENTED: A message about a `booking_id` Booking does not know is
  logged, counted in a metric and acknowledged, not retried. Its
  `message_id` is recorded, so a redelivery does nothing and is not counted
  again as unknown (it counts as a duplicate).
- Note: Catalogue's messaging is on by default in compose
  (`CATALOGUE_MESSAGING_ENABLED=true`). If it is switched off, Booking never
  receives `event.changed`.
## 4. States — PROPOSED unless labelled
 
Seat status (inventory): `free` | `held` | `booked`.
 
Booking status: `held`, `payment_pending`, `confirmed`, `released`,
`refund_required`, `refund_pending`, `refunded`.
 
**Rule for every row:** a trigger acts only if the booking is in the listed
"from" state (and any listed condition holds). Otherwise it does nothing.
 
| # | From | Trigger (and condition) | To | Seat | Published (outbox) |
|---|---|---|---|---|---|
| 1 | — | Hold succeeds (§5) | held; `held_until` = now + 10 min | free → held | availability |
| 2 | held, not expired, event not cancelled | Checkout by the same customer (§6), breaker accepts checkouts (§9) | payment_pending; `checkout_started_at` = now | held | — |
| 3 | held | Checkout while the Payment circuit breaker refuses checkouts (§9) | held (unchanged) | held | — (reply 503 `payment_service_unavailable`) |
| 4 | held | `held_until` passed (sweeper, every 5 s) | released | held → free | availability; `booking.cancelled` (hold_expired) |
| 5 | payment_pending | Payment reports `paid`, event NOT cancelled | confirmed | held → booked | `booking.confirmed` |
| 6 | payment_pending | Payment reports `paid`, event cancelled | refund_required | held → free | availability; `booking.cancelled` (event_cancelled); `refund.requested` |
| 7 | payment_pending | Payment reports `failed`, or `refunded` (§8) | released | held → free | availability; `booking.cancelled` (payment_failed) |
| 8 | payment_pending | Safety net: `checkout_started_at` + S passed (§9). Booking calls Payment again; if it answers → row 5, 6 or 7; otherwise → | released | held → free | availability; `booking.cancelled` (payments_unavailable) |
| 9 | released | Payment reports `paid` (late success) | refund_required | untouched | `refund.requested` |
| 10 | refund_required or refund_pending | `refund.completed` | refunded | — | — (Payment informs Ticketing directly) |
| 11 | refund_required | `refund.failed` | refund_pending | — | — |
| 12 | refund_pending | Refund recovery command (§10) | refund_pending | — | `refund.requested` with a NEW `message_id` |
| 13 | held | Event cancelled (§7) | released | held → free | availability; `booking.cancelled` (event_cancelled) |
 
"Payment reports" means either Payment's HTTP reply or a `payment.result`
message; both feed the same rows (§9).
 
Rows 1-13 are IMPLEMENTED.
 
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
- Rows 5, 6 and 7 all store Payment's `payment_id`.
- IMPLEMENTED: Row 8 also recovers from a Booking crash mid-payment,
  because `checkout_started_at` is stored.
- Row 12 increments `refund_attempts`; it changes neither `status` nor
  `updated_at`.
- Final states: `confirmed`, `refunded`; `released` is final unless a late
  charge arrives (row 9).
### 4a. Implementation rules — PROPOSED unless labelled
- Every transition of an existing booking (rows 2 and 4–12) is one
  conditional UPDATE on the booking row ("… WHERE booking_id = X AND
  status = Y [AND condition]"), checking that exactly one row changed;
  otherwise nothing is changed and the trigger is a no-op (on the message
  path the `message_id` is still recorded). Time conditions use the
  database clock. IMPLEMENTED for rows 2 and 5–12.
- Exceptions (IMPLEMENTED):
  - Row 1 is a conditional UPDATE on the seat plus a booking INSERT.
  - Row 4 is one conditional UPDATE per EVENT (`event_id = E AND status =
    'held' AND held_until < now()`), changing any number of rows, including
    zero. The number of seats freed must match the number of bookings
    released, or the transaction is undone. One transaction per event; one
    event failing does not stop the others. Row 4 records no `message_id`.
  - Row 4's sweeper: it first runs at startup, after the upgrade rule
    (§4b) and the backfill (§11), then every 5 s. It only writes the
    outbox, so it runs with or without RabbitMQ. One Booking instance is
    assumed. A hold has expired when `held_until < now()` (strictly, on
    the database clock). Until the next sweep, for up to about 5 s, a hold
    retry on an expired hold still gets 200 with `booking_status: "held"`.
    The hold length is the setting `HOLD_MINUTES` (default 10).
  - Row 13 releases every booking of the event that is still `held`
    (including expired ones not yet swept), with reason `event_cancelled`,
    in the same transaction as recording the cancellation (also for a
    cancelled-first event). Any number of rows, including zero, is correct.
    `payment_pending` and `confirmed` bookings are not touched.
- Seat updates of an existing booking are keyed on the booking
  (`booking_ref = booking_id`) and must change the matching rows.
  IMPLEMENTED for rows 4–8 and 13: if the seats do not change, the whole
  transaction is rolled back. Row 9 never touches the seat.
- IMPLEMENTED: Row 2 commits BEFORE Payment is called. Payment is never
  called inside a database transaction, and no row lock is held during a
  Payment call.
- Lock order: the per-event creation lock, then the event row, then the
  booking row, then the seat rows. A path skips the locks it does not need:
  - Creation lock: needed only while an event has no record. A hold takes
    it only then; `event.changed` always takes it.
  - Event row: in paths that change seats of an event that has a record.
  - Booking row: the deciding conditional UPDATE.
  - IMPLEMENTED: holds (row 1), `event.changed` and the upgrade rule (§4b)
    follow this order. None of them locks an existing booking row: a hold
    takes the event row, wins the seat, then inserts its new booking; the
    upgrade rule takes the locks as a hold does and only frees seats that
    have no booking.
  - IMPLEMENTED: rows 5, 6 and 7 take event row → booking row → seat rows.
    They need no creation lock: checkout requires an event record (§6). The
    "event cancelled?" check for rows 5/6 is made under the event lock.
    Rows 2, 3, 10 and 11 change no seats and lock only the booking row.
  - IMPLEMENTED: on the message path the `message_id` is recorded first, in
    the same transaction, also when the effect is a no-op.
  - IMPLEMENTED: row 4 takes the creation lock (only while the event has no
    record) → event row → booking rows → seat rows. Row 13 runs inside
    `event.changed`: creation lock → event row → booking rows → seat rows.
    Row 8 takes event row → booking row → seat row (no creation lock:
    checkout requires a record). Row 9 takes event row → booking row (it
    shares rows 5–7's code path). Row 12 locks only the booking row.
- What shares one transaction ("processed `message_id`" applies only when a
  message triggered the row, not an HTTP reply or a timer):
| Row(s) | In one transaction |
|---|---|
| 1 | seat conditional UPDATE, booking INSERT (price, `held_until`, email, name), event sequence lock + increment, availability outbox row (sequence and outbox row only for an event with a record) |
| 2 | booking row lock, booking UPDATE + `checkout_started_at` (then commit, then call Payment) |
| 3 | nothing is written |
| 4 | creation lock (only while the event has no record), event row lock; `UPDATE bookings … WHERE event_id = E AND status = 'held' AND held_until < now()` (any count); their seats → free (count must match); sequence + one availability outbox row (only for an event with a record, and only if any booking was released); one `booking.cancelled` (hold_expired) outbox row per released booking; no processed `message_id` |
| 8 | event row lock, booking conditional UPDATE (`status = 'payment_pending'` and `checkout_started_at` + S passed, database clock), seat → free, sequence, availability + `booking.cancelled` (payments_unavailable) outbox rows (the Payment call happens before, outside) |
| 5 | processed `message_id`, event row lock, booking (+ `payment_id`), seat → booked, `booking.confirmed` outbox row |
| 6 | processed `message_id`, event row lock, booking (+ `payment_id`), seat → free, sequence, availability + `booking.cancelled` + `refund.requested` outbox rows |
| 7 | processed `message_id`, event row lock, booking (+ `payment_id`), seat → free, sequence, availability + `booking.cancelled` outbox rows |
| 9 | processed `message_id` (message path only), event row lock, booking conditional UPDATE `released` → `refund_required` (+ `payment_id`), `refund.requested` outbox row; no seat change, no availability, no second `booking.cancelled` |
| 10, 11 | processed `message_id`, booking conditional UPDATE (status only); no outbox row |
| 12 | booking conditional UPDATE (`status = 'refund_pending' AND payment_id IS NOT NULL`; `refund_attempts` + 1; `status` and `updated_at` unchanged), `refund.requested` outbox row with a new `message_id` |
| 13 | processed `message_id`, creation lock, event row lock, event status/version; `UPDATE bookings … WHERE event_id = E AND status = 'held'` (any count); their seats → free (count must match); sequence + one availability outbox row (only if row 13 freed seats or the record is new: at most one); one `booking.cancelled` (event_cancelled) outbox row per released booking |
 
- Rows 1-13 and their transactions are IMPLEMENTED (row 1 including the
  booking INSERT).
- Known risk: the per-event lock on the event row (rows 1, 4–9, 13; it is
  also the sequence lock) makes holds on the same event queue behind each
  other, and Payment's answers (rows 5–7) queue on it behind holds too.
  Holds on an event with a record take the event lock even when they lose
  the seat. Measure it in the 500-buyer test before relying on it.
- Known risk: every Booking database statement has a 2 s limit, waiting
  for a lock included. A sweep (row 4) or a safety-net release (row 8)
  that waits more than 2 s for a busy event's lock fails for that event
  and is counted; nothing is changed. The sweeper retries it on its next
  run; the safety net retries that booking after its 30 s error pause (§9).
### 4b. Upgrade rule — IMPLEMENTED
On every start, before the startup snapshots (§11), any seat `held` with no
booking row (a hold made before bookings existed) is set back to `free`.
- It only changes something once: every newer hold creates its booking, so
  later starts find nothing to free.
- One transaction per event, in the lock order (§4a).
- One availability snapshot per affected event that has a record; none for
  an event without one.
 
## 5. POST /holds
IMPLEMENTED:
Request: `event_id`, `seat_id`, `customer_id` (required, trimmed, non-empty);
optional `customer_email`, `customer_name` (trimmed). Length limits from §1
(422 if exceeded; see §14).
- 201 Seat held. `{ booking_ref, event_id, seat_id, status: "held",
  booking_id, booking_status }`
- 200 Seat already held by this same customer (a retry); same body,
  existing `booking_ref`.
- 409 Held or booked by someone else, or already booked (even by this
  customer). `{ error: "seat_taken", message: "That seat was just taken.
  We've refreshed the map, pick another." }`
- 409 Event cancelled. `{ error: "event_cancelled", message: "This event
  has been cancelled." }` A cancelled event always gives this reply, never
  201, 200, 404 or `seat_taken`. The cancelled check is part of the hold's
  single conditional update.
- 404 No such seat for this event.
  `{ error: "seat_not_found", message: "No such seat for this event." }`
- 422 Missing, empty, invalid or over-long fields (FastAPI `detail` format).
- 503 Any database error (FastAPI `detail` format).
Booking fields (IMPLEMENTED):
- A successful hold creates the booking (§4 row 1). Its price is fixed at
  hold time (§8).
- `customer_email`, `customer_name`: if absent or sent as `null`, Booking
  stores `<customer_id>@example.test` and `customer_id`. A provided value
  that is blank after trimming gets 422.
- `booking_id` is the same value as `booking_ref`. `booking_status` is the
  booking's state from §4. `status` keeps meaning "this customer holds the
  seat" and stays `"held"`, because the UI checks it.
- A retry returns the booking's current `booking_status` and never changes
  the booking (a new `customer_email` or `customer_name` on a retry is
  ignored).
- A seat that is `held` but has no booking gets `seat_taken`, even for the
  same customer. This can only happen before the upgrade rule (§4b) has run.
Also implemented: `GET /health`; `GET /ready` (checks the database, not
Payment; reports `capabilities: ["holds", "checkout"]`); `GET /metrics`
(Prometheus text format); an optional demo seed (`BOOKING_DEMO_SEED=true`),
off by default; it must stay off while the `event.changed` consumer runs (§7).
- A seat whose event has no record can still be held, and writes no
  availability snapshot (§11).
PROPOSED additions: none.
Notes:
- The hold is a single conditional update; exactly one caller wins a seat.
  Any lookup afterwards only chooses the reply.
- `booking_id` is the identifier used everywhere downstream; `booking_ref` is
  kept so the current UI keeps working.
- Known limit: worst case 2 s to connect plus 2 s per query can exceed the
  gateway's 3 s timeout; the UI reports that as "outcome unknown".
## 6. Checkout and status
POST /bookings/{booking_id}/checkout — IMPLEMENTED
Request: `{ customer_id }`. Checked in this order:
1. 404 Unknown booking, or `customer_id` does not match. Same reply as the
   lookup below: `{ error: "booking_not_found", message: "No such
   booking." }`
2. 202 `{ booking_id, booking_status: "payment_pending" }` if already
   `payment_pending` (repeat; Payment is not called again).
3. 409 `{ error: "event_cancelled", message: "This event has been
   cancelled." }` if the event is cancelled.
4. 409 `{ error: "not_checkoutable", message: "This booking can no longer
   be checked out." }` if not `held`, the hold has expired, or Booking has
   no event record for the event.
5. 503 `{ error: "payment_service_unavailable", message: "Payments are
   unavailable right now. Your seat is still reserved; please try again
   shortly." }` if the breaker refuses checkouts (§9); the booking stays
   `held` (row 3).
6. Otherwise 202 `{ booking_id, booking_status: "payment_pending" }` (row 2).
   If a cancellation is committed between these checks and the update, the
   reply is check 3's `event_cancelled` and the booking stays `held`.
   This also covers a cancellation committed while checkout waits for the
   booking's lock: the event's status is read after the lock, so the reply
   is `event_cancelled` (check 3), never `not_checkoutable`.
- 422 Missing, blank or over-long `customer_id` or `booking_id` (FastAPI
  `detail` format).
- 503 Any database error (FastAPI `detail` format).
Why 202: the gateway allows 3 s and never retries a POST. Booking's Payment
call (§9: at most about 6.75 s in total) cannot fit inside it, so it runs in
the background.
 
GET /bookings/{booking_id}?customer_id=… — IMPLEMENTED
- 200 `{ booking_id, event_id, seat_id, booking_status, held_until, payment_id }`
  (`held_until` is offset-aware; `payment_id` is `null` until set).
- 404 Unknown booking, or `customer_id` does not match. Both get the same
  reply, so a guessed `booking_id` reveals nothing:
  `{ error: "booking_not_found", message: "No such booking." }`
- 422 Missing `customer_id`, or a `booking_id` or `customer_id` that is blank
  or over 64 characters (FastAPI `detail` format).
- 503 Any database error (FastAPI `detail` format).
PROPOSED: the page polls this. What it shows:
- `held`: "Seat reserved"
- `payment_pending`: "Processing your payment"
- `confirmed`: "Confirmed, your ticket is on its way"
- `released`: "Booking released"
- `refund_required`, `refund_pending`: "A refund is on its way"
- `refunded`: "Refunded"
## 7. event.changed (consumed) — ACCEPTED (Catalogue's proposal)
Payload as in `catalogue/examples/contracts/event-*.json`.
 
Booking stores (IMPLEMENTED):
- Per event: `event_id`, `event_version`, `status`, `title`, `starts_at`
  (stored exactly as Catalogue sends it), `timezone`, plus its own
  availability `sequence`.
- Per seat: `seat_id`, `section`, `label`.
Rules (IMPLEMENTED unless labelled):
- Apply only an `event_version` newer than the stored one. Equal or older:
  do nothing (but record the `message_id`).
- Any newer snapshot with status `scheduled` creates missing seats from
  `seats[]`, and fills in a missing `label` or an empty `section` on
  existing seats.
  It never changes a seat's status. This covers `created`, an `updated` that
  overtakes it, and seats left by the demo seed.
- `updated` changes stored metadata only; held and booked seats keep their
  status. Seat-map changes after publication are out of scope (Catalogue
  excludes them too).
- A cancellation writes one availability snapshot only if row 13 freed
  seats. Combined with a first-record snapshot (§11), that is at most one
  per message. An update that adds no seats writes none (§11).
- `cancelled` is remembered even if it arrives first, so an older creation
  cannot reopen inventory. Then new holds are refused (409 `event_cancelled`,
  §5).
- IMPLEMENTED: after `cancelled`, checkouts are refused (§6); held bookings
  are released (row 13); a payment in progress that succeeds is refunded
  (row 6), one that fails is released as usual (row 7).
- OPEN: Confirmed bookings on a cancelled event keep their valid ticket and
  are not refunded (§13.2). The fix, if wanted, is row 6's refund path applied
  to each confirmed booking on that event.
- The demo seed (§5) is off by default (`BOOKING_DEMO_SEED=false`) and must
  stay off whenever this consumer runs; it uses the same event IDs as
  Catalogue's seed.
- IMPLEMENTED: A seat with no event record cannot be checked out (§6,
  check 4).
## 8. Payment (consumed API) — ACCEPTED (Payment's implementation)
POST /payments
`{ booking_id, amount (integer minor units), currency, customer_email }`
- The response holds the final `paid` or `failed`; a decline is HTTP 200 with
  `status: "failed"`. Booking stores `payment_id`.
- A repeated POST can also return HTTP 200 with `status: "refunded"` (the
  charge happened and was already refunded). IMPLEMENTED: Booking treats it
  like `failed`: the booking is released (row 7, reason `payment_failed`)
  and no refund is requested.
- IMPLEMENTED: Booking sends `customer_email` (the booking's stored email)
  on every POST /payments. `PAYMENT_URL` defaults to the compose service,
  Payment's internal address (`http://payment:8000`).
- After a timeout or error, Booking calls again with the same `booking_id`;
  if the charge was recorded, Payment returns it, otherwise it charges afresh.
  Either way the outcome is correct. Booking does this only in its background
  call, never in the browser path.
- Payment has no lookup by `booking_id`; "asking again" is always a repeated
  POST, which Payment de-duplicates.
- Messages carry money as a decimal string (`"45.00"`); HTTP uses minor units
  (`4500`).
Price (IMPLEMENTED): a flat synthetic price per seat in Booking's config
(4500, EUR), copied onto the booking at hold time. It never changes for that
booking, because Payment rejects a repeated `booking_id` with a different
amount (409). Startup is refused if the currency does not match
`^[A-Z]{3}$` or the price is 0 or below, because Payment would reject every
charge.
 
A `failed` (or `refunded`) payment releases the booking (row 7). To try again
the customer makes a new hold, which gets a new `booking_id`; Payment keeps the failed
result for the old one.
 
## 9. Payment results and the safety net — IMPLEMENTED unless labelled
Payment reports each charge in its HTTP reply and as `payment.result`
(written to Payment's outbox in the same transaction as the charge). Booking
feeds both into the same §4 rows; whichever arrives second does nothing.
 
Background call:
- 3 attempts, through the circuit breaker. Each attempt has a 2 s TOTAL
  deadline enforced by Booking (not the HTTP client's per-phase limits).
- Full jitter: a random pause of 0–250 ms before attempt 2 and 0–500 ms
  before attempt 3, so a call takes at most about 6.75 s in total.
- Circuit breaker: opens after 5 consecutive failures; after 30 s it lets
  exactly one trial attempt through (half-open). A successful trial closes
  it; a failed one opens it for another 30 s.
- Every HTTP attempt counts once in the breaker (not every booking). A
  failure arriving while it is already open changes nothing. A success
  (even a late one) closes it. A 409 or 422, and an attempt cancelled at
  shutdown, count neither way.
- When the breaker refuses an attempt, no request is sent and the call
  stops.
- Checkout (§6, check 5) is accepted while the breaker is closed, or open
  with 30 s passed and no trial running. It is refused (row 3) while open
  under 30 s, or while a trial is in flight. A checkout that loses the trial
  to another call in that brief race stays `payment_pending`.
- If no attempt gets an answer, the booking stays `payment_pending`. It then
  moves on a `payment.result` message, or by the safety net.
  Calls still running at shutdown are cancelled; their bookings also stay
  `payment_pending`.
"Answers" (for the background call and the safety net) means HTTP 200 with
`paid`, `failed` or `refunded` → row 5, 6 or 7 (`refunded` → row 7, §8).
- A 409 or 422 should never happen (the price is fixed). It is not retried,
  and is logged and counted.
- Anything else counts as a failure and is retried: timeout, connection
  error, any 5xx, any other 4xx, and a 200 with an unknown `status`, a
  missing or invalid `payment_id`, or another booking's `booking_id`.
Metrics:
- `booking_checkouts_total{outcome}`: `started`, `repeat`, `not_found`,
  `event_cancelled`, `not_checkoutable`, `breaker_open`.
- `booking_payment_attempts_total{outcome}`, one per HTTP attempt: `paid`,
  `failed`, `refunded`, `timeout`, `error`, `server_error`, `client_error`,
  `rejected` (409/422), `bad_response`; plus `short_circuited` when the
  breaker refused the attempt.
- `booking_payment_breaker_state` (gauge): 0 closed, 1 open, 2 half-open.
- `booking_payment_answers_total{result}`, one per background call:
  `confirmed`, `refund_required`, `released`, `late_success` (row 9),
  `noop`, `no_answer`.
- `booking_payment_apply_errors_total`: background calls stopped by an
  error (the booking stays `payment_pending`).
- `booking_payment_messages_total{type, result}`, messages on
  `booking.payment-updates`: `confirmed`, `refund_required`, `released`,
  `late_success` (row 9), `refunded`, `refund_pending`, `noop`,
  `duplicate`, `unknown_booking`.
- `booking_safety_net_calls_total{outcome}`, one per safety-net call:
  `confirmed`, `refund_required`, `released` (Payment answered: rows 5–7),
  `late_success` (row 9), `payments_unavailable` (no answer: row 8),
  `noop` (the booking had already moved on), `error`.
- `booking_safety_net_skipped_total`: due bookings skipped because their
  safety-net call is still in flight.
- `booking_late_successes_total`: late successes refunded (row 9), from
  either the HTTP reply or a message; counted after the commit.
 
Safety net (row 8) — IMPLEMENTED:
- Starts counting from the stored `checkout_started_at`.
- S is `BOOKING_SAFETY_NET_SECONDS`; default 60, decimals allowed. Startup
  is refused if S is below 7 (it must exceed the background call's 6.75 s).
- Checked every 0.5 s on a fixed rhythm (separately from row 4's 5 s
  sweeper), up to 50 bookings per check, so it fires within about S + 0.5 s.
- When it fires, Booking makes ONE more POST /payments (same `booking_id`
  and body, the same HTTP client and breaker, 2 s total deadline). Payment
  answers → row 5, 6 or 7. No answer → row 8 releases; a breaker refusal
  (no request sent) or a 409/422 counts as no answer.
- Row 8's UPDATE re-checks the status and the time condition (database
  clock), so a `payment.result` that arrives during the call wins.
- Row 8 on a cancelled event keeps the reason `payments_unavailable`.
- Booking keeps an in-memory set of bookings whose safety-net call is in
  progress, and skips a booking that is already in it. So the check can run
  every 0.5 s without starting a second call for the same booking.
- After an error (e.g. the database is down) the booking stays
  `payment_pending` and is left alone for 30 s before it is tried again.
- At shutdown, running safety-net calls are cancelled before the HTTP
  client closes, so those bookings stay `payment_pending` and are picked up
  after the restart.
- After a Booking restart the set is empty, so a booking may get one more
  call. That is harmless: Payment returns the stored result for the same
  `booking_id` and never charges twice (§8).
- This relies on one Booking instance, the same assumption as the outbox
  relay (§3).
- After a Booking crash this call may be the first charge. That is correct.
Late success (row 9) — IMPLEMENTED:
- `paid` for a released booking → `refund_required` and one
  `refund.requested`; the seat is never touched (it may already belong to
  someone else).
- `failed` or `refunded` for a released booking does nothing.
- A duplicate late `paid` (HTTP reply and message) produces exactly one
  `refund.requested`.
Late success, measured (there is no live demo): with `PAYMENT_DELAY=10`
(Payment's maximum) and S = 7 s, the booking was released at about +9.1 s,
the refund was requested at about +10.6 s and the booking was `refunded` at
about +21.2 s (PAYMENT_DELAY applies to refunds too). Valid range of S with
these values: the background call's three attempts end by about 6.75 s, so
S must be above that (startup requires S ≥ 7); the safety net's call must
time out before the charge commits at 10 s, so S + 0.5 s + 2 s < 10 s, i.e.
S < 7.5 s.
Operations notes:
- Start Payment before Booking. Otherwise old `payment_pending` bookings
  are released on the first check; still correct, but avoidable.
- After a late-success run the breaker holds 4 consecutive failures until
  the next success.
- The sweeper and the safety net use the database clock. A clock jump in
  the VM can make them fire early in real time; that is safe, because
  every transition is conditional.
Row 6 through `payment.result`, measured with `PAYMENT_DELAY=10` on a
booking whose event was cancelled during the charge: the charge commits at
about +10.0 s, row 6 is applied at about +10.5 s, and the booking is
`refunded` at about +20.8 s.
## 10. Refund recovery — IMPLEMENTED unless labelled
IMPLEMENTED: rows 10 and 11 (`refund.completed` → `refunded`;
`refund.failed` → `refund_pending`). Two ways a refund can stall:
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
Refund recovery command (row 12), run inside the running Booking container:
`docker compose exec booking python refund_recovery.py <booking_id>`
`docker compose exec booking python refund_recovery.py --all`
- It acts only on a booking in `refund_pending` with a stored `payment_id`
  (`--all`: every such booking, longest waiting first; one transaction
  each). It writes one `refund.requested` with a NEW `message_id` to the
  outbox; the running Booking's relay publishes it. The booking stays
  `refund_pending` until `refund.completed` (row 10).
- Any other booking is refused and nothing is written. For one in
  `refund_required` it names the DLQ replay above (case 1).
- Exit code: 0 every booking asked was requested (or nothing to do); 1 at
  least one was refused; 2 bad arguments or the database is unavailable.
Metrics, read from the database each time `/metrics` is scraped:
- `booking_bookings{status}`: bookings per status, all seven statuses
  shown, 0 when a status has none.
- `booking_refund_oldest_age_seconds{status}` (`refund_required`,
  `refund_pending`): seconds since the oldest booking in that status
  entered it (database clock), 0 when there is none.
- `booking_status_refresh_errors_total`: scrapes where the database could
  not be read; the scrape still succeeds and the two gauges above keep
  their last values.
PROPOSED: a dashboard alert when a refund status is above 0 for more than
a few minutes.
 
## 11. seat.availability-changed (published) — ACCEPTED (Catalogue's proposal)
Payload exactly: `event_id`, `sequence`, `seats_remaining`,
`available_seat_ids` (the full set of free seats). No other fields.
`seats_remaining` equals the number of IDs.
 
Rules (IMPLEMENTED unless labelled):
- A snapshot is written:
  - on a successful hold on an event that has a record (§4 row 1);
  - when Booking first creates its record for an event from `event.changed`
    (whether or not its seats already existed); a cancelled-first event gets
    a snapshot of its free seats, normally none;
  - when a newer version adds seats;
  - when the upgrade rule (§4b) frees held seats: one per affected event that
    has a record;
  - when §4 row 6, 7 or 8 frees a seat;
  - when §4 row 4 or 13 frees seats: one per event per transaction (row 4
    only for an event that has a record).
  Until the first snapshot, Catalogue shows availability as unknown.
- `sequence` starts at 1 per event; 0 means "no snapshot yet". It is
  incremented in the same transaction as the seat change and the outbox row,
  and the snapshot is computed after taking the sequence lock. It is never
  reset.
- On startup, after the upgrade rule (§4b), each event with `sequence` 0 gets
  exactly one snapshot.
- For a cancelled event the snapshot is published as normal; Catalogue keeps
  the event shown as cancelled.
- Catalogue rejects (retries, then dead-letters) a snapshot for an event or
  seat it does not know. Booking only publishes for events it learned from
  Catalogue.
- PROPOSED: a Booking database reset is supported only as a full stack reset
  (`docker compose down -v`).
- Metrics: `booking_outbox_pending` (gauge: outbox rows not yet published,
  set-aside rows excluded), `booking_outbox_failed` (gauge: set-aside
  outbox rows, §3), `booking_outbox_published_total{type}` (rows the broker
  confirmed), and `booking_messages_total{type, outcome}` (consumed
  messages; `outcome` is `processed`, `retried` or `dead_lettered`).
  `booking_holds_released_total{reason}` (`hold_expired` for row 4,
  `event_cancelled` for row 13; counted after the commit) and
  `booking_expiry_sweep_errors_total` (one per event whose release failed,
  plus one per failed sweep; the sweeper keeps running).
  Checkout and Payment metrics are in §9; booking-status metrics in §10.
## 12. Messages Booking publishes to Ticketing and Payment
ACCEPTED: fields as Ticketing reads them today (`docs/messaging.md`,
`notification/app/handlers.py`) and as Payment validates `refund.requested`
(`payment/app/messages.py`). Lengths capped per §1.
IMPLEMENTED: `booking.confirmed`, `booking.cancelled` (all four reasons
below) and `refund.requested` are sent as below. `booking.cancelled`
carries only the fields listed; row 4 on an event without a record leaves
out `event_title`.
 
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
5. Withdrawn: a repository question, not part of the contract.
6. A refund made directly in Payment on a confirmed booking: Booking ignores
   it, so the seat stays booked while Ticketing voids the ticket. Handle, or
   leave (the gateway does not expose Payment)?
7. Ticketing: reword the `payments_unavailable` email
   (notification/app/emails.py:10), which currently says "please try again"
   although the customer may have been charged.
8. RESOLVED: compose now defaults `BOOKING_DEMO_SEED` to off.
9. Outbox cleanup: published rows are kept forever.
10. Discovery's demo seeder publishes `event.changed` without `status`,
    `timezone` or `seats`; Booking would dead-letter those (the seeder is off
    by default). Discovery's owner to decide.
11. Set-aside outbox rows (§3): how are they recovered?
## 14. Deliberate breaking changes — IMPLEMENTED
- IDs longer than 64 characters are rejected with 422 (before this change
  they were accepted). Agreed by Booking's owner, because Ticketing and
  Payment cannot store them.
## 15. Tests that will change (booking/tests/test_integration.py)
- Done: `hold_seat()` now returns `(outcome, booking_id, booking_status)`,
  and the tests check the new tuples.
- Seat inserts without `label` or an event record (in the tests and in the
  demo seed) if those become required.
- Done: `/ready` capabilities are now `['holds', 'checkout']`, and the
  test checks them.
- Done: the test of other hold outcomes and the cancelled-event checkout
  test were updated for row 13 (a cancellation now releases held bookings
  and writes one snapshot).