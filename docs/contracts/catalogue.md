# Catalogue contracts - proposal

Status: proposed for Booking and Discovery review; not an agreed shared contract.
Checkpoint: C01. Last updated: 2026-11-14.

Catalogue now implements the candidate HTTP surface and an opt-in RabbitMQ
adapter described below. Messaging is disabled by default while consumer
agreement is pending. The shared envelope, routing keys and peer service code
are unchanged. See the [runbook](../catalogue-operations.md) for operational limits.

## Confirmed integration boundaries

- Catalogue owns venue/event metadata and seat-map definitions. Booking owns seat
  inventory, holds and bookings. Catalogue's availability is a read projection.
- `event.changed` is published by Catalogue to `events`, using the same routing
  key. Its existing consumers are `booking.event-changes` and
  `discovery.event-changes`.
- Booking publishes `seat.availability-changed`; Catalogue's existing queue is
  `catalogue.seat-availability`. There is no Catalogue-to-Booking synchronous call.
- The current envelope has `message_id`, `type`, `occurred_at`, `version`,
  `producer` and `payload`. Booking's hold API accepts string `event_id` and
  `seat_id`; Ticketing stores event IDs in a 64-character field.

Evidence: `shared/messaging/{envelope,topology}.py`, [messaging notes](../messaging.md),
Booking commit `7996674` and Ticketing commit `bb17236`. Payment commit `63b8b0e`
adds proposed payment/refund contracts; this draft does not redefine them.

## Proposed HTTP surface

Paths are relative to Catalogue; gateway prefixes are a separate integration choice.

| Method and path | Proposed behaviour | Success |
|---|---|---|
| `POST /venues` | Create synthetic/demo venue metadata with a stable ID | 201 and saved venue |
| `GET /venues/{venue_id}` | Read venue metadata | 200 and saved venue |
| `POST /events` | Create event/seat map; write its creation message atomically | 201 and saved event |
| `GET /events` | List event metadata, including status and event version | 200 and a JSON array, possibly empty |
| `GET /events/{event_id}` | Read event, venue, seat map and projected availability | 200 and event detail |
| `PATCH /events/{event_id}` | Update metadata or cancel using `expected_version` | 200 and saved event |

The initial draft uses caller-supplied stable IDs for repeatable synthetic seeds.
IDs are nonblank, trimmed strings of at most 64 characters. An event/seat pair
identifies a seat, matching Booking's composite key. IDs are immutable and are not
reused for a different event. Creation with an existing ID returns 409; seeds must
skip existing IDs and preserve edits rather than reset records or live inventory.

Proposed venue creation body:

```json
{"venue_id":"venue-demo-001","name":"Demo Venue","city":"Dublin"}
```

The event creation body is illustrated in
[`event-create-request.json`](../../catalogue/examples/contracts/event-create-request.json).
It includes `event_id`, `venue_id`, nonblank `title`, `description`, an offset-aware
`starts_at`, an IANA `timezone`, `tags`, and a nonempty `seats` list. Each seat has
`seat_id`, `section` and `label`; seat IDs are unique within the event. Initial
`event_version: 1` and `status: scheduled` are assigned by Catalogue, not the caller.

Event detail uses the same event metadata as an event-change payload, excluding
`change`. It also includes `availability`: null until the first valid Booking
snapshot, otherwise an object with `sequence`, `seats_remaining`,
`available_seat_ids`, `occurred_at` and `applied_at`. Unknown availability is never
represented as zero or as every seat being free. The proposed list response is an
array of summaries with `event_id`, `event_version`, `title`, `starts_at`, `timezone`,
`status` and `venue`. Full descriptions, tags, seat maps and projected availability
are read through detail. Both scheduled and cancelled events remain listable.

Proposed metadata update: `{"expected_version":1,"title":"Evening Performance - revised"}`.
The version check, metadata change and outbox insert share one transaction.
A stale expected version returns 409. A successful change increments the event
version once. The first patch supports descriptive metadata; IDs, venue and
seat-map changes after publication are excluded until Booking's rules are agreed.

Proposed cancellation: `{"expected_version":2,"status":"cancelled"}`.
It emits a cancelled event snapshot and keeps the event readable with that status.
Booking owns stopping holds, cancelling bookings and any refund decisions.
Reactivation is outside this initial draft. Repeating cancellation with the current
version returns the existing record without another state change/outbox row;
a stale expected version still returns 409.

Missing records/referenced venues return 404; duplicate IDs, version conflicts or
disallowed lifecycle changes return 409; invalid fields return 422; unavailable
database access returns 503. Follow the existing FastAPI `detail` error convention.
Validation is implemented locally; shared limits and error semantics still need agreement.

## Proposed event.changed payload

Use the unchanged envelope with `type: event.changed`, `producer: catalogue` and
`version: 1` as the proposed message-schema version. Its payload is a full event
metadata snapshot with:

- `change`: `created`, `updated` or `cancelled`;
- `event_id` and a positive, increasing `event_version`;
- `status`: `scheduled` or `cancelled`;
- embedded `venue` metadata, title, description, start/timezone, tags and seat map.

Examples: [created](../../catalogue/examples/contracts/event-created.json),
[updated](../../catalogue/examples/contracts/event-updated.json) and
[cancelled](../../catalogue/examples/contracts/event-cancelled.json).

The outbox stores the message ID, occurrence time and committed snapshot together
with the event write. Retries reuse that immutable body and ID; dispatch must not
rebuild an old message from the latest mutable event row. Volatile availability
is not included in `event.changed`, so seat holds do not trigger metadata re-indexing.

Proposed consumer rules for review: apply only a newer `event_version`. Booking
initializes missing inventory from a scheduled snapshot, but metadata updates must
not reset held/booked seats. A cancellation received first must be remembered so an
older creation cannot reopen inventory. Discovery indexes newer scheduled events
and removes cancelled ones. These are requirements on the proposed interaction,
not claims that the current consumers implement it.

## Proposed availability snapshots

Candidate shape: a full set of free seat IDs, not a count delta. Envelope:
`type: seat.availability-changed`, `producer: booking`, `version: 1`.
Payload: `event_id`, positive integer `sequence`, `seats_remaining` and the complete
`available_seat_ids` list. The count equals the number of unique IDs, and all IDs
must belong to the event's known seat map. An empty list/count zero is valid.

Examples: [snapshot](../../catalogue/examples/contracts/availability-snapshot.json)
and [later release](../../catalogue/examples/contracts/availability-after-release.json).
The second increases the free count from one to two. Sequence order is monotonic;
the free-seat count is allowed to rise or fall.

Proposed sequence rule: Booking stores a durable per-event counter and never resets
it for the same event ID. If inventory resets must be supported, agree an epoch or
rebuild protocol before implementation; a local counter reset is unsafe.

Catalogue records message IDs and applies only a newer sequence in one database
transaction, using a condition safe under concurrent deliveries. Duplicate message
IDs and equal/older sequences cannot replace newer state. This skipping rule relies
on full snapshots; it must be redesigned if Booking publishes deltas instead.

For an unknown event, fail the handler and follow the shared retry/DLQ path rather
than inventing metadata or silently acknowledging the update. Agree recovery and
backfill for database resets with Booking before treating this as final policy.
Cancelled event metadata remains cancelled even if an availability message arrives.

`occurred_at` is the source inventory-change time, not a retry/publish time.
`applied_at` is Catalogue's commit/application time. Event-to-apply lag uses these
timestamps with comparable clocks. A long time since the last inventory change
alone does not prove stale data; use consumer/backlog health alongside timestamps.
Browsing freshness does not guarantee that a subsequent hold will succeed.

## Decisions required before shared integration

1. Booking/Discovery: approve HTTP paths, IDs, event fields, summaries and snapshots.
2. Booking: approve full availability snapshots versus deltas, durable sequence/reset
   semantics, initial inventory publication and unknown-event recovery.
3. Contract maintainer: distinguish schema `version`, `event_version` and `sequence`;
   centralize accepted field definitions without silently changing the shared envelope.
4. Catalogue/Booking: agree cancellation timing and venue/seat-map mutation rules.
5. Booking/Payment/Ticketing: agree pricing/amount ownership, `booking_ref` versus
   `booking_id`, and how event/customer metadata reaches confirmations. No price
   field or amount authority is invented in this draft.
6. Topology/consumer owners: reconcile the plan's retry wording with current code
   (one attempt plus three retries at 1/2/4 seconds).

## Validation scope

Examples are hand-authored synthetic fixtures for review, not seeds or published
messages. Validate JSON, IDs/timestamps, envelope round-tripping, event versions,
seat-map consistency and snapshot counts/sequences. Such checks do not establish
consumer compatibility: the Catalogue-specific payload consumers are not yet
implemented in fetched code. Local endpoint and isolated real-broker probes now
exercise these candidate shapes; accepted cross-service contract tests remain
pending. Synthetic seed records are maintained separately in `catalogue/seed-data.json`.
