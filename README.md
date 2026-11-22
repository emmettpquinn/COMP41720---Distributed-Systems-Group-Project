# Ticket Booking Platform

COMP41720 Distributed Systems group project. This branch integrates the five
service implementations. See the [Catalogue runbook](docs/catalogue-operations.md),
[development integration guide](docs/integration-development.md)
and [Booking contract](docs/contracts/booking.md).

## What runs here

The base Compose file starts Catalogue, Booking, Discovery, Payment and
Ticketing/Notification, each with its own private PostgreSQL database. It also
starts RabbitMQ, MailHog, the gateway/browser UI and a private HTTP embedding
simulator. Discovery uses pgvector. The monitoring overlay adds Prometheus and
Grafana. The development overlay remains compatible but adds no extra service.

Catalogue owns events, seat-map definitions and a projected availability view.
Booking consumes event changes, owns seat inventory, holds and bookings, and
publishes availability updates. Checkout runs in the background with payment
retries, a circuit breaker, expiry/safety-net handling and late-success refunds.
Payment owns its simulated ledger and refunds. Ticketing issues tickets and
captures emails in MailHog. Discovery provides date-filtered vector search,
similar events and labelled keyword fallback. The default embedding endpoint is
simulated; real Ollama remains an optional overlay.

The website supports browsing, search, holds, checkout, booking-status polling
and ticket lookup. Payments are simulated and email stays in MailHog. No real
money or external email delivery is involved.

Source credits are preserved in Git: Ticketing/shared messaging `bb17236`, Payment
`63b8b0e`, Discovery `eccabe5`, and Booking through `b069ca8` and the master merge
`cb90d30`. The local branch was fast-forwarded to master; no teammate branch was
modified. Subsequent gateway/UI integration changes remain subject to review.

## Start locally

Start Docker Desktop, then run from the repository root:

```sh
docker compose up -d --build --wait --wait-timeout 240
```

This starts all five business services and the website. To include monitoring:

```sh
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d --build --wait --wait-timeout 240
```

Use the same `-p` project name and `-f` options in subsequent commands. Alternate
projects also need different host ports. Never stop unrelated containers to free
ports. Builds need downloaded images/packages; offline builds need them cached.

Default URLs:

- Browser: http://localhost:8080
- Catalogue: http://localhost:8001/docs
- Booking: http://localhost:8002/docs
- Discovery: http://localhost:8003/docs
- Payment: http://localhost:8004/docs
- Ticketing: http://localhost:8005/docs
- RabbitMQ: http://localhost:15672 (`guest` / `guest`)
- MailHog: http://localhost:8025
- Prometheus: http://localhost:9090 (monitoring overlay)
- Grafana: http://localhost:3000 (`admin` / `stagedoor-demo`, monitoring overlay)

`GATEWAY_PORT`, `PROMETHEUS_PORT`, `GRAFANA_PORT` and `GRAFANA_PASSWORD` configure
the supported host settings. Existing Grafana volumes retain their login.

The topology, migration and seed jobs should exit 0. Catalogue seeds one fictional
venue, two events and four seats through its own API/outbox code. Booking consumes
those messages and publishes its initial availability; allow a short catch-up.
`CATALOGUE_MESSAGING_ENABLED` defaults to true. Keep `BOOKING_DEMO_SEED=false`
while the real consumer runs. Seeds skip existing records and preserve edits.

## Browse, reserve and check out

1. Open the website, select an event and choose a seat. Availability is a
   projection; Booking makes the authoritative hold decision.
2. Reserve the seat. **Your booking** shows the price saved at hold time, EUR
   45.00 per seat by default.
3. Select **Pay and complete booking**. Checkout sends the same customer ID used
   for the hold. A 202 means processing; the page polls Booking for the result.
4. After confirmation, select **Check ticket**. Ticketing may take a short time
   to process the confirmation. Check MailHog for the email.

A competing hold is refused and the page refreshes projected availability. A
checkout timeout is an unknown outcome; the page reads status rather than
sending another payment POST. An explicit `payment_service_unavailable` 503
leaves the hold intact and allows a manual retry with the same booking ID.

Holds expire after `HOLD_MINUTES` (default 10). They do not expire while payment
is pending; the page shows no payment countdown. `BOOKING_SAFETY_NET_SECONDS`
(default 60) controls Booking's later reconciliation/release step. A late charge
after release is refunded without taking the seat back. Refund rejection can
require the service-owned recovery command; see the Booking contract.

Confirmed bookings on subsequently cancelled events retain valid tickets under
the current team decision. A cancellation before confirmation releases/refunds
the unconfirmed booking. The gateway exposes no direct Payment creation/refund
route. See the [checkout handoff](docs/contracts/checkout-preview.md).

Search displays its effective date window and semantic/keyword/provider labels.
Use **Browse all events** to clear a query. The seeded events are dated 27 and
28 November 2026; date queries can correctly exclude them. Ranking uses simulated
embeddings by default, so it does not establish learned semantic relevance.

## Create a larger demo seating map

With the stack running, create a new fictional venue/event with five rows and
ten seats per row:

```sh
python3 tools/create_demo_event.py --rows 5 --columns 10 --publish
```

The tool prints the new event ID. Open the website, select **Browse all events**
and **Refresh**, then choose **Demo Theatre Performance**. Allow a short time for
Booking inventory and availability to arrive. Rows A-E and columns 1-10 stay
aligned; wider maps scroll horizontally. Unavailable seats are grey and disabled.
Another browser needs a refresh to receive the latest projected availability.

Without `--publish`, the tool only prints the venue/event JSON. Use `--title`,
`--venue-name`, `--section`, `--starts-at` (future ISO timestamp with offset),
`--event-id` and `--venue-id` to customise it. A start a week ahead is chosen by
default. `--catalogue-url` changes the API address if you use alternate ports.
The helper permits up to 52 rows, 40 columns and 500 total seats. These are demo
tool limits, not a claim about tested production capacity.

Seat labels such as A1/A10 define row/column placement within each section using
the existing event schema. Custom labels still render as selectable seats.
Existing event IDs are refused rather than changing their seats/bookings. New
demo events are created through Catalogue and its normal message pipeline; the
original seed events and saved bookings are not reset.

## Health and monitoring

```sh
docker compose ps -a
curl -fsS http://localhost:8001/ready
curl -fsS http://localhost:8002/ready
curl -fsS http://localhost:8080/api/capabilities
```

Booking readiness checks its DB and reports `holds` and `checkout`; it does not
require Payment to be up. Payment readiness checks its DB and configured broker.
Gateway health describes its own process. Upstream failures are reported per request.

Seven scrape targets are configured: Catalogue, Booking, Discovery, Payment,
Ticketing, RabbitMQ and Prometheus. Catalogue, Booking, Discovery and Payment
have provisioned dashboards using the shared Prometheus datasource. Expanded
Booking panels and last-seat evidence are in the separate `booking/evidence` PR;
its results are separate from the integration checks described here.

## Persistence and shutdown

```sh
docker compose logs --tail 100 catalogue booking payment gateway
docker compose stop
```

Include the same monitoring/project options used at startup. `stop` and ordinary
`down` preserve named database volumes. `down -v` deletes all that project's saved
data and is only an intentional full test reset. Never reset only Booking's
sequence counters against an existing Catalogue projection. RabbitMQ currently
has no named data volume; durability across container recreation is not proven.

On upgrade from the old hold-only code, Booking frees legacy held seats without
booking records. New holds/bookings and availability sequences survive normal
restarts. Published Booking outbox rows are retained. Recovery of set-aside
outbox rows remains an operational open item.

## Tests and verification

Run each service suite separately: several services have a package named `app`.
From the root, with Booking's image and database available:

```sh
docker compose run --rm --no-deps -T \
  -e AMQP_URL= \
  -e BOOKING_TEST_DB=postgresql://booking:booking@booking-db:5432/booking \
  -v "$PWD/booking/tests:/srv/tests:ro" \
  -v "$PWD/catalogue/examples/contracts:/catalogue/examples/contracts:ro" \
  booking python -m unittest discover -s tests -v
```

The tests create unique temporary schemas and disable RabbitMQ consumption.
They must never consume a shared application's queues. Other service commands
and dated evidence are in the integration/Catalogue runbooks.

```sh
npm --prefix gateway ci --ignore-scripts
npm --prefix gateway run test:ui
python3 -m unittest discover -s tools/tests -v
python3 tools/verify_discovery_integration.py --output /tmp/discovery-integration.json
python3 tools/verify_development_startup.py --output /tmp/committed-startup.json
```

The startup harness normally verifies committed HEAD from an archive. Before
committing, test the current tracked/untracked source snapshot with:

```sh
python3 tools/verify_development_startup.py --working-tree --failure-cases --output /tmp/checkout-startup.json
```

These integration checks create fresh private projects on ephemeral host ports
and remove only their own synthetic containers/volumes. The working-tree option
creates no commit and does not stage files. Failure cases cover payment decline,
event cancellation and a real delayed simulated charge/refund after seat release.
Generated JSON/logs stay outside the repository. DOM tests do not establish visual
browser/device verification. No end-to-end 500-buyer or learned-model claim follows.

Earlier [Catalogue-only workload evidence](docs/evidence/catalogue-load-startup.md)
and [crash/outage evidence](docs/evidence/catalogue-reliability.md) retain their
original scope. The final group report, contribution/signature work and academic
AI disclosure remain student/group responsibilities.

## Optional real model

See [Discovery integration details](docs/contracts/discovery-integration.md).
For a fresh real-model project, add `-f docker-compose.ollama.yml` to the startup
command and use a distinct project/free ports. First startup downloads model assets.
Keep simulator and learned-model indexes separate or explicitly activate a new
index version. Do not delete existing volumes just to change model identity.
