# Catalogue development runbook

This branch implements the candidate Catalogue API, transactional outbox,
availability projection, cache, metrics and synthetic seeds. The proposed
[contracts](contracts/catalogue.md) still need Booking/Discovery agreement.
Messaging is disabled by default; HTTP writes still save durable outbox rows.
Development Booking, Discovery and Payment containers remain health-only shells.

## Startup and synthetic data

Follow the root [README](../README.md) for Compose startup. The dependency order is
PostgreSQL healthy, migrations, seed job, then API. Both jobs should exit 0.
`catalogue/seed-data.json` contains one fictional venue, two events and four seats.
The seed job uses Catalogue's own write path and outbox in one transaction, under
an advisory lock. Rerunning skips existing IDs and preserves subsequent edits,
cancellations and availability. It never writes another service's database.

```sh
docker compose run --rm catalogue-seed
curl -fsS http://localhost:8001/events
curl -fsS http://localhost:8001/events/event-demo-001
```

Include the same project and overlay options used at startup in every command.
Seeds define seat maps only. Availability remains null until Booking supplies a
valid snapshot; a seed does not imply that a seat can be booked.

## HTTP writes and reads

Interactive request schemas are at http://localhost:8001/docs. Supported endpoints:

- `POST /venues` and `GET /venues/{venue_id}`.
- `POST /events`, `GET /events` and `GET /events/{event_id}`.
- `PATCH /events/{event_id}` with an `expected_version` and changed metadata.

Create a venue before referencing it from an event. Events need a nonempty unique
seat map, offset-aware start timestamp and valid IANA timezone. A detail response
includes venue, seat map and projected availability. The list contains summaries
of both scheduled and cancelled events. Missing availability is null, not zero.

```sh
curl -fsS -X PATCH http://localhost:8001/events/event-demo-001 \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"title":"Evening Performance - revised"}'
```

A changed patch increments the event version once and saves its full immutable
`event.changed` snapshot in the same database transaction. A stale version returns
409. No-op patches do not create another version/message. Cancellation uses
`{"expected_version":2,"status":"cancelled"}`; repeating with the current version
is a no-op. Reactivation and seat-map/venue/ID mutation are excluded. Booking owns
cancelling bookings and refunds; Catalogue's status alone does not perform those.

Errors use FastAPI's `detail`: missing records 404, conflicts 409, invalid requests
422 and unavailable database access 503. Database calls have bounded timeouts.
These local development APIs have no authentication; access control belongs to
the deployment/gateway design.

## Candidate messaging adapter

Only enable the candidate adapter against an isolated broker or after contracts
are accepted. Starting with the flag enabled can publish all accumulated rows.

```sh
CATALOGUE_MESSAGING_ENABLED=true docker compose up -d --build catalogue
```

The worker uses the existing shared envelope, topology and retry/DLQ handler.
It declares publisher confirms and mandatory routing. The dispatcher locks one
pending row with `FOR UPDATE SKIP LOCKED`, publishes its saved envelope, and marks
it sent only after confirmation. Publish failure leaves the row pending. Failure
after confirmation but before the marker commits can redeliver the same message
ID, so downstream consumers must be idempotent. Reconnect attempts use backoff.
Concurrent dispatchers can publish different rows out of order; consumers must
apply increasing event versions. This provides at-least-once delivery.

The availability handler accepts the proposed full free-seat snapshot from
Booking. It validates the envelope, count, unique IDs and known seat-map subset.
The processed message ID and newer projection are committed together under an
event lock. Duplicate IDs and older/equal sequences cannot replace newer state;
newer releases can increase the free count. Unknown events or invalid snapshots
follow the shared retries and DLQ. A reset/backfill policy still needs agreement.

The shared retry policy is an initial attempt plus three retries at 1/2/4 seconds,
then DLQ. See [messaging notes](messaging.md) for inspection and replay. Do not
replay invalid payloads without correction or reset a durable sequence locally.

## Cache and freshness

Event detail uses a process-local bounded TTL cache (0.5 seconds, 128 entries by
default). Committed metadata and applied availability changes invalidate entries.
A generation check prevents an old in-flight read refilling an invalidated entry.
Other replicas observe changes after their TTL expires; there is no distributed
invalidation. `CACHE_TTL_SECONDS=0` disables caching; `CACHE_MAX_ENTRIES` sets size.
Cache hits can briefly serve a previous snapshot during a database outage.

Availability includes source `occurred_at` and local `applied_at`. Source-to-apply
lag is measured for applied snapshots. Future source timestamps are counted as
clock skew and excluded from lag samples. Idle time since the last change alone
does not prove staleness. Booking remains authoritative when a hold is attempted.

## Metrics and operational checks

`/health` checks process liveness; `/ready` checks only PostgreSQL. `/metrics`
exports normalized HTTP counts/latency, outbox backlog/published deliveries,
database health, messaging enabled/connected state, snapshot/consumer outcomes,
availability lag, clock skew and cache results. The exporter remains available
when its database probe fails; backlog is then NaN rather than an invented zero.

The monitoring overlay scrapes Catalogue, Ticketing, RabbitMQ and Prometheus.
Grafana provisions the **Catalogue** dashboard (UID `catalogue`)
with eight panels. Lag reference lines are 2 and 10 seconds; this batch does not
implement alert notification rules. Payment and AI panels await real integration.

## Verification and limits

The Catalogue suite has 54 passing tests against isolated PostgreSQL schemas,
covering API validation, transactional rollback, optimistic conflicts, snapshots,
duplicate/reordered delivery, dispatcher retry/concurrency, cache invalidation and
seed repeatability. See the root README for the test command.

Isolated live-broker probes also verified delivery to both event-change queues,
100 duplicate deliveries, reordered snapshots, invalid-message retry/DLQ,
post-confirm database-marker failure with stable-ID redelivery, broker stop/start
with durable pending writes, seed preservation and live Prometheus/Grafana data.
Queue probes substitute for unavailable real consumers; these are not accepted
cross-service contract tests. No 500-buyer lag target, timed 60-second outage,
full purchase flow, process-kill crash window or full-system startup is claimed.
