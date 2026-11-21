# Gateway, browser and Booking integration

The base Compose stack runs all five business services, their private databases,
RabbitMQ, MailHog and the gateway. Discovery uses the explicit HTTP model simulator
by default. The monitoring overlay adds seven scrape targets and four service
dashboards. `docker-compose.dev.yml` remains a compatibility overlay; it no longer
adds Booking or turns on a demo seed.

## Source and responsibilities

Booking changes through `b069ca8` arrived via master `cb90d30`, preserving original
commits. Payment is based on `63b8b0e`, Ticketing/shared messaging on `bb17236` and
Discovery on `eccabe5`. No teammate branch was edited. Earlier source-import
verification below records its historical scope, not the current implementation.

Booking now consumes Catalogue's `event.changed`, owns inventory/holds/bookings,
and publishes full availability snapshots to Catalogue. Catalogue messaging is on
by default, while Booking's separate demo seed is off. New Catalogue events become
bookable after asynchronous ingestion. Availability can lag; every hold still
checks Booking's authoritative inventory.

Booking implements checkout/status, expiry, Payment retries/circuit breaking,
payment-result consumption, a safety net and late-success refunds. A hold does not
expire while `payment_pending`. The default hold is 10 minutes and the safety net
is 60 seconds, configurable through Compose. Confirmed bookings on subsequently
cancelled events keep their tickets under the current team decision.

## Browser and gateway

The gateway enables holds and checkout in the normal Compose setup. Its standalone
Python defaults remain disabled unless configured. The browser uses:

- Catalogue list/detail reads for current event metadata and projected seats.
- Discovery search/similar reads with date and provider labels.
- `POST /api/booking/holds` using a stable synthetic customer ID per tab.
- `POST /api/booking/bookings/{id}/checkout` with `{customer_id}`.
- `GET /api/booking/bookings/{id}?customer_id=...` for polling and reload recovery.
- `GET /api/ticketing/tickets/{id}` after Booking confirms.

The proxy keeps the three-second timeout, 64 KiB request limit, restricted origins,
credential filtering and no automatic POST retry. A checkout timeout or generic
503 is an unknown outcome: read status, never infer a decline. The explicit
`payment_service_unavailable` business error permits a manual retry of the same
booking while the hold remains. No direct Payment route is exposed.

The UI understands `held`, `payment_pending`, `confirmed`, `released`,
`refund_required`, `refund_pending` and `refunded`. There is no countdown during
payment. Released bookings can be checked for late-success refunds; a refund is
not represented as a ticket. Ticketing may return 404 while its consumer catches
up. The additive `price_minor`/`currency` status fields expose the price already
stored on the booking, so the browser does not calculate or submit a charge.
See [checkout contract and verification](contracts/checkout-preview.md).

## Checks

Gateway boundary tests:

```sh
docker compose run --rm --no-deps \
  -v "$PWD/gateway/tests:/srv/tests:ro" \
  gateway python -m unittest discover -s tests -v
```

Booking's integration tests use private temporary schemas. Disable messaging and
mount Catalogue's example envelopes when running them:

```sh
docker compose run --rm --no-deps -T \
  -e AMQP_URL= \
  -e BOOKING_TEST_DB=postgresql://booking:booking@booking-db:5432/booking \
  -v "$PWD/booking/tests:/srv/tests:ro" \
  -v "$PWD/catalogue/examples/contracts:/catalogue/examples/contracts:ro" \
  booking python -m unittest discover -s tests -v
```

These checks cover concurrency, metadata/availability messaging, checkout,
expiry, cancellation, safety-net and refund transitions. They create no shared
queue consumer and do not clear application data.

Payment tests, including live-broker refunds on an isolated synthetic stack:

```sh
docker compose run --rm --no-deps \
  -e PAYMENT_TEST_DB=postgresql://payment:payment@payment-db:5432/payment \
  -e PAYMENT_TEST_URL=http://payment:8000 \
  -e PAYMENT_TEST_AMQP=amqp://guest:guest@rabbitmq:5672/ \
  -v "$PWD/payment/tests:/srv/tests:ro" \
  payment python -m unittest discover -s tests -v
```

Use the same project/file options as startup. The Payment database tests create
and remove unique schemas; the broker test creates one unique synthetic charge
and refund. Do not run failure checks against shared projects. The dedicated
[Catalogue failure harness](../tools/verify_catalogue_failures.py) creates and
removes only its own generated project and volumes:

```sh
python3 tools/verify_catalogue_failures.py --output /tmp/catalogue-faults.json
```

Keep generated JSON/logs outside Git. The harness tests real process kills before
publish and after confirmation, a measured 60-second broker outage, duplicate/
reordered snapshots and a database outage. Queue probes stand in for unavailable
Booking/Discovery consumers; no 500-buyer or full purchase claim follows.

## Historical development preview (2026-11-14)

Project `catalogue-next-check` uses UI 38080, APIs 38201-38205, Prometheus 39090,
Grafana 33000, broker 45672/45673/45692 and MailHog 38025. It is isolated from
older verification stacks and contains only synthetic data. Catalogue messaging
was enabled only here for candidate probes; the committed/default setting stays
off. The browser smoke check in Safari verified listing, detail, hold success,
repeat of the same reference, and a competing-buyer 409 followed by projection
refresh and a locally marked refused seat. The two first-event seats are held by
those synthetic checks; the second event remains available for a fresh demo.
Responsive CSS is included; no mobile-device browser check is claimed.

The gateway's seven boundary tests, Payment's 25 tests (including live RabbitMQ),
and six added Booking integration tests passed. The unchanged Ticketing suite
also passed all 14 tests. All five Prometheus targets are
UP, and Catalogue/Payment dashboards are provisioned. The isolated Catalogue
failure harness passed; see [ADR evidence](evidence/catalogue-reliability.md).

## Independent checks added 2026-11-17

A fresh clean archive of committed `970258f` passed startup and four-database/
application restart checks, with repeat-safe seeds and real gateway/hold retries.
A separate 500-client Catalogue-only run passed without HTTP errors and observed
all 300 synthetic updates. See [measurement and startup evidence](evidence/catalogue-load-startup.md)
for workload, hardware, counts and limitations. Reproduce using the root README
commands; both new harnesses create and clean up private projects.

The preview ports above describe the 2026-11-14 session, not a continuously
running deployment. The 2026-11-17 independent stacks were cleaned up after their
checks; no preview was left running. No production service code or Compose
runtime configuration changed in this verification batch.

## Discovery update (2026-11-17)

Source `eccabe5` is imported into this branch and the shell removed. The approved
integration adds a private pgvector database, a private model simulator, gateway
read routes/UI, shared metrics/dashboard, readiness and compatible Catalogue
limits. See [the detailed integration review](contracts/discovery-integration.md).
There are now six expected scrape targets and three service dashboards.

The isolated live harness passed actual create/update/cancel, duplicate/stale
handling, model outage/fallback/recovery, projection persistence after restart and
shared Grafana proxy queries. Discovery 49 (pgvector), Catalogue 60, gateway 10 and
DOM 6 tests pass; the earlier dated results above retain their own scope.

The current synthetic preview is `discovery-integration-check`: UI 48080, APIs
48201-48205, Prometheus 49090, Grafana 43000 and MailHog 48025. It uses isolated
volumes and explicit Catalogue publication. A separate generated harness project
was removed after verification. No teammate branch or existing service database
was modified. No learned-model relevance, full purchase or 500-buyer claim follows.

## Booking integration verification (2026-11-19)

Master `cb90d30` was fast-forwarded into this branch without creating a commit.
Existing local work was backed up outside the repository and preserved. PR #9 is
already merged; PR #10 (`booking/evidence`) remains open and was not imported or
merged by this work.

Executed checks:

- Booking: 143 tests passed against a separately generated PostgreSQL project;
  tests use private schemas, disabled messaging and mounted Catalogue fixtures.
- Gateway: 15 boundary tests; browser: 20 DOM tests; verification helpers: 6 tests.
- Real Discovery integration: all five services ready, separate databases,
  create/update/cancel, duplicate/stale messages, model fallback/recovery,
  restart persistence and seven monitoring targets passed.
- Working-tree startup: fresh five-service startup, four dashboards, live
  availability, hold retry/conflict, customer ownership checks, saved price,
  asynchronous checkout, idempotent Payment, valid ticket and captured email passed.
- Application recreation and all five database restarts preserved booking,
  payment, ticket and Catalogue state.
- Real simulated-provider failure cases passed: decline releases the seat with
  no valid ticket; event cancellation prevents checkout; a 10-second charge with
  a 7-second Booking safety net releases the original seat, preserves a second
  customer's replacement hold, and eventually refunds the original charge.

Raw results/logs are outside Git at `/tmp/booking-startup-checkout-20261119.json`,
`/tmp/booking-discovery-integration-20261119.json` and `/tmp/booking-tests-20261119.log`.
All generated test stacks and their volumes were removed. These checks use
simulated money, synthetic customers and an HTTP embedding simulator. They do
not establish visual browser QA, learned-model relevance, or a complete 500-buyer
journey. A clean committed archive must be checked after an approved commit;
this batch used the explicit working-tree snapshot option and created no commit.

The separate PR #10 evidence targets Booking directly. Its final reported run
had one winner and 499 conflicts, but 252 of 500 responses exceeded the gateway's
three-second limit. That is peer-provided evidence, not a locally repeated result.
Keep full gateway workload performance open; do not change the timeout silently
or treat the direct-service test as browser-level acceptance.

## Larger seating demo (2026-11-21)

The integration branch was fast-forwarded to master `c83faf1`, including both
Booking evidence and checkout integration. New local work adds labelled seat rows
and columns without changing the shared seat schema or existing seeds. Within
one section, labels A1/A10/B1 define placement. Numeric columns are ordered
naturally; missing numbers leave gaps. Different sections keep independent row
maps. Custom/ambiguous labels fall back to selectable seat lists without dropping
seats. Large sparse labels cannot allocate unbounded placeholder grids.

`tools/create_demo_event.py` generates a new fictional venue/event, defaulting to
five rows and ten columns. It prints JSON unless `--publish` is supplied. Publish
uses Catalogue's public API and refuses to overwrite an existing event. Its
limits are 52 rows, 40 columns and 500 seats. They are helper limits, not service
capacity guarantees. Wider maps scroll inside the panel; a separate browser
refreshes Catalogue's projection to see another customer's latest hold.

```sh
python3 tools/create_demo_event.py --rows 5 --columns 10 --publish
python3 tools/verify_development_startup.py --working-tree --demo-seating --output /tmp/seating-startup.json
```

Verification passed: 25 DOM checks and 10 tool checks, plus an isolated working-tree
startup/restart run that created a real 50-seat event, bought E10, rejected a
competing hold, observed 49 available seats and issued a valid E10 ticket. Existing
purchase/email, owner, seed and application/database restart checks also passed.
Raw results are in `/tmp/seating-startup-20261121.json`, outside Git.

A separate disposable Safari preview verified aligned rows A-E and all ten columns
on the desktop, seat selection, checkout, confirmation and ticket-code display.
A second tab showed E10 disabled with 49 seats remaining. This is desktop Safari
verification, not a mobile-device or 500-buyer test. Both generated test projects
were removed. The original seed maps, existing databases and teammate branches
were not changed. No staging, commit or push was performed.
