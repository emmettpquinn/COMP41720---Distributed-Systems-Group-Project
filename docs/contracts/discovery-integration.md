# Discovery integration on this branch

Source: `origin/feature/discovery-date-filter`, commits `cc508e8` and `eccabe5`.
Originally reviewed and imported on 2026-11-17. Subsequent master integration
preserves the original Discovery commits in this branch's history; no upstream
branch was modified. Current startup details were updated on 2026-11-19.
Source ownership remains with Discovery. The original `.dockerignore` safeguards
are retained; Node test dependencies are additionally excluded.

## Included implementation and integration edits

The imported service owns its pgvector projection, duplicate/version records,
keyword fallback, date grammar, index versions, model bulkhead/breaker, metrics
and tests. Catalogue creation/update/cancellation fixtures match the three current
Catalogue examples exactly. Both `change` and legacy demo `action` are accepted;
conflicts are rejected. Cancellation tombstones prevent stale resurrection.

Integration edits remove personal names from file contents, add process-only
liveness and a bounded `/ready` check, connect shared Compose/monitoring, and expose
read-only search/similar routes through the gateway. No model call is required
for readiness: model outages can serve labelled keyword fallback. Missing database
or configured broker readiness returns 503 without leaking connection details.

The base stack now runs all five business services, each with its own database.
Booking consumes event metadata, publishes availability and implements checkout,
expiry and late-success refunds. The gateway integrates checkout/status and
ticket lookup. The development overlay remains compatible but adds no extra service.

## Catalogue payload limits

Create and patch inputs now share Discovery-compatible limits:

- Title: 1-500 characters after trimming.
- Description: at most 8000 characters.
- Tags: at most 50; each nonblank tag at most 200 characters after trimming.

Oversized requests return 422 before event/outbox writes. Outgoing snapshots are
also checked so a partial patch of legacy oversized metadata returns 409 and
rolls back; the affected fields must be repaired together. No text is silently
truncated and no existing row or immutable outbox envelope is rewritten.
Before enabling publication on old volumes, review existing oversized metadata
and already-pending envelopes. Those historical payloads need an explicit recovery
agreement; the new input checks cannot retroactively change them.

These limits remain Catalogue/Discovery compatibility rules. Booking has since
accepted the event/availability contracts; its implemented sequence, cancellation,
pricing and Payment timing rules are documented in [Booking contracts](booking.md).

## Startup and explicit simulator

Start all five business containers and shared monitoring from the root:

```sh
CATALOGUE_MESSAGING_ENABLED=true docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.monitoring.yml up -d --build --wait --wait-timeout 240
```

Catalogue messaging now defaults to true. The explicit flag above also enables
publication of committed snapshots, including queued seed messages, through the
shared broker. If explicitly disabled, browsing and the saved Discovery index
remain available, but new Catalogue changes do not reach Booking or Discovery.
Booking now consumes its event-change queue and publishes availability updates.

Discovery stays on port 8003. Its independent demo uses 8004, so do not run that
port mapping alongside the integrated Payment service. No standalone Discovery
Prometheus/Grafana instances are needed: the shared stack has seven scrape targets
and four service dashboards. The imported independent configs remain available for the
Discovery owner and have separate project names/storage.

By default `discovery-model` is a private HTTP protocol simulator. Its vectors and
token counts are synthetic; no learned model runs. The UI appends **Simulated
model** for `simulated_http` or `simulated` provider responses, including when the
ranking mode is labelled semantic. This is a functional integration demonstration,
not evidence of learned-model relevance or actual API charges.

## Optional real Ollama

Use a fresh named project to keep simulator-bound indexes separate:

```sh
CATALOGUE_MESSAGING_ENABLED=true docker compose -p ticket-real-model -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.monitoring.yml -f docker-compose.ollama.yml up -d --build --wait --wait-timeout 240
```

Use free ports or stop the other owned stack first; never stop unrelated projects.
The optional overlay replaces the private simulator with Ollama and adds a model
initialization job. First use needs network access to download images/model;
subsequent offline use needs those assets cached. Search can use keyword fallback
until initialization/indexing finishes. This overlay was configuration-validated,
not run with a learned model in this integration session.

Do not reuse a simulator-bound index as a real-model index. Model digests are
bound to an index version. For existing data, build and activate a new complete
index through the authenticated admin API; do not delete volumes to change mode.
A fresh project is the simplest isolated rehearsal. Admin/model-control endpoints
are not exposed by the browser gateway. The simulator's fault controls have no
host port and are used only by the isolated verification harness.

## Browser behaviour

The gateway permits `GET /api/discovery/search?q=...&limit=...` and
`GET /api/discovery/events/{event_id}/similar`; it blocks admin routes and forwards
fallback/provider metadata unchanged. Search ordering comes from Discovery.
Displayed venue/timezone/status come from current Catalogue metadata; unknown and
currently cancelled IDs are filtered from cards. Event detail and hold authority
remain unchanged. Search does not authorize a hold or bypass Booking.

The UI displays the returned semantic/keyword label and effective date window in
its timezone. Unsupported date grammar is shown as a validation error; unavailable
search is not disguised as keyword fallback. Browse all clears search state.
Reservation controls remain disabled while a list request or hold is in flight.

## Verification

- Discovery: 49 tests passed against a separate disposable pgvector test database,
  with no skips. The test fixture clears Discovery tables, so never set
  `DISCOVERY_TEST_DATABASE_URL` to an application database.
- Catalogue: 60 tests include exact-limit acceptance, pre-write rejection and a
  legacy oversized partial-patch rollback check. Gateway: 10 boundary tests.
- Six Node/jsdom DOM tests cover ranking/live metadata, fallback labels, date
  errors, unavailable/disabled search and hold controls. jsdom is a development
  dependency only; production containers remain Python.
- The live private-stack harness verifies separate service databases, actual
  Catalogue outbox -> RabbitMQ -> Discovery create/update/cancel, duplicates,
  stale cancellation replay, model outage/recovery, process restart and shared
  metrics/dashboard queries. Reproduce with:

```sh
python3 tools/verify_discovery_integration.py --output /tmp/discovery-integration.json
npm --prefix gateway ci --ignore-scripts
npm --prefix gateway run test:ui
```

The harness removes only its own generated stack/volumes. Raw JSON/logs stay
outside Git. Safari visual verification was blocked by a locked private window
and computer-use restrictions on its authentication screen; DOM tests are not a
substitute claim of visual/device testing.
