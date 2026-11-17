# Ticket Booking Platform

New to the project? Start with the [plain-language guide](getting-started/README.md).

COMP41720 Distributed Systems group project. This README describes integration
on `feature/catalogue-integration`; services are also developed on separate
branches. See the [checkpoint tracker](docs/PROJECT_CONTINUITY.md),
[Catalogue runbook](docs/catalogue-operations.md) and
[development integration runbook](docs/integration-development.md).

## What runs here

The base stack starts Catalogue, Ticketing/Notification, Payment and Discovery,
each with its own PostgreSQL database, plus RabbitMQ, MailHog, the gateway/browser
UI and a private HTTP embedding simulator. Discovery uses pgvector. The development
overlay adds Booking's real hold API and its private database; all five business
containers now run service implementations. The monitoring overlay adds Prometheus
and Grafana. The gateway is an extra infrastructure component, not one
of the five business services.

Catalogue provides versioned event/venue APIs, seat-map definitions, an atomic
outbox, availability projection, bounded detail cache, synthetic seeds and metrics.
Payment handles its simulated ledger/refunds/outbox; Ticketing handles tickets and
emails. Booking currently provides holds only: no expiry, checkout/status API,
saga, event consumer or availability publisher. Discovery provides date-filtered
search, similar events, versioned indexing and labelled keyword fallback. The
default model is explicitly simulated; real Ollama is an optional overlay. This stack does not yet perform a complete purchase.

Source credits: Ticketing/shared messaging `bb17236`, Payment `63b8b0e`, Booking
hold source `7996674`, Discovery source `eccabe5`. Integration changes are recorded in the runbook; teammate
branches are untouched. Candidate Catalogue contracts still need consumer
agreement, so `CATALOGUE_MESSAGING_ENABLED` defaults to false.

## Start locally

Install Docker Desktop with Compose support and start Docker Desktop. From the
repository root, for the base stack:

```sh
docker compose up -d --build --wait --wait-timeout 180
```

For all five business containers, live Catalogue-to-Discovery indexing, browser
UI and monitoring (explicitly enables candidate Catalogue publication):

```sh
CATALOGUE_MESSAGING_ENABLED=true docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.monitoring.yml up -d --build --wait --wait-timeout 240
```

Use the same `-p` project name and `-f` file options in every subsequent command.
For example, add `-p ticket-demo` for an isolated named project; alternate projects
also need alternate host ports. Never stop unrelated containers to free ports.
Builds download images/packages; offline startup requires cached dependencies.
Local development credentials are synthetic; no paid API keys are needed.

Base URLs:

- Browser UI: http://localhost:8080
- Catalogue API: http://localhost:8001/docs and `/metrics`
- Payment API: http://localhost:8004/docs and `/metrics`
- Discovery API: http://localhost:8003/docs and `/metrics`
- Ticketing API: http://localhost:8005/docs and `/metrics`
- RabbitMQ management: http://localhost:15672 (`guest` / `guest`)
- RabbitMQ metrics: http://localhost:15692/metrics
- MailHog inbox: http://localhost:8025

The development overlay adds Booking at http://localhost:8002/docs. Discovery
provides `/health`, `/ready`, `/search` and `/events/{event_id}/similar`.
The browser port binds to loopback; set `GATEWAY_PORT` to change it.

The `topology-init`, `catalogue-migrate` and `catalogue-seed` jobs should exit 0.
Catalogue loads one fictional venue/two events/four seats through its own write
and outbox path. Reruns preserve existing IDs and edits. Development Booking
separately loads matching synthetic inventory using its own code and database;
`BOOKING_DEMO_SEED=false` disables this explicit fixture. Restart never resets
existing holds. This fixture does not replace agreed asynchronous event ingestion.

## Browse and reserve a demo seat

Open the browser UI, select an event and choose a seat. The gateway calls
Catalogue for browsing and Booking for the authoritative hold decision. Unknown
availability is labelled unknown. A successful hold displays the actual reference;
a refusal refetches the projection and marks that choice locally. A timeout can
have an unknown outcome: retrying uses the same tab's synthetic customer ID.
Search displays its effective date filter, semantic/keyword label and simulated
provider label. Browse all clears the query. Catalogue supplies current card
metadata while Discovery supplies ranking; search does not authorize a hold.
Checkout is unavailable until the Booking saga/API is integrated. Current holds
do not expire; use synthetic data for this development flow.

The gateway uses FastAPI/httpx for fixed same-origin routes, request-size limits
and bounded timeouts. It exposes no direct payment-creation route. See the
[integration runbook](docs/integration-development.md) for contracts and limits.

## Health and messaging

```sh
docker compose ps -a
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8001/ready
curl -fsS http://localhost:8004/ready
curl -fsS http://localhost:8005/ready
curl -fsS http://localhost:8080/health
```

Catalogue liveness is independent of PostgreSQL; readiness checks only its DB.
Payment readiness checks its DB and enabled broker. Booking's development `/ready`
checks its database and reports the hold-only capability. Gateway `/health` checks
its own process; upstream failures are reported on the requested route.

Catalogue saves outbox rows while messaging is off. Enable the candidate adapter
only in an isolated stack or after contracts are accepted:

```sh
CATALOGUE_MESSAGING_ENABLED=true docker compose up -d catalogue
```

Include the original project/overlay options. This can publish accumulated rows.
The worker uses confirmed mandatory publication and the shared retry/DLQ handler.
See [candidate contracts](docs/contracts/catalogue.md) and the runbook. Payment's
messaging is enabled by Compose; its Booking contracts still need agreement.

## Monitoring

The monitoring overlay starts Prometheus at http://localhost:9090 and Grafana at
http://localhost:3000. Both ports bind to loopback. Set `PROMETHEUS_PORT`,
`GRAFANA_PORT` and, before first startup, `GRAFANA_PASSWORD` as needed. Default local
Grafana login is `admin` / `stagedoor-demo`; existing volumes retain their login.

Six exporters are configured: Catalogue, Discovery, Payment, Ticketing, RabbitMQ
and Prometheus. The datasource and separate Catalogue/Payment/Discovery dashboards
are provisioned automatically. Booking's imported hold API has no metrics target. Catalogue lag panels have 2/10-second reference thresholds;
notification alert rules remain open. Discovery reports input tokens and notional
cost; simulator values are synthetic, not actual charges.

```sh
curl -fsS http://localhost:9090/-/ready
curl -fsS http://localhost:3000/api/health
curl -fsS 'http://localhost:9090/api/v1/targets?state=active'
```

## Persistence, logs and shutdown

Each implemented business service has its own private database/volume. Catalogue
runs immutable numbered migrations before seeds/API; migrations 001/002/003 store
metadata, outbox and inbox/projection state. Do not edit applied migration history.
Booking's startup schema and Payment's schema upgrade remain service-owned.

```sh
docker compose logs --tail 100 catalogue payment gateway
docker compose stop
```

With development/monitoring overlays, include those same files. `stop` and normal
`down` preserve named database/monitoring volumes. Adding `-v` deletes that project's
volumes. The upstream Payment broker-volume change is deferred; RabbitMQ data
persistence across container recreation is not established here.

## Tests and evidence

Run service-local tests separately because each service has its own `app` package.
The Catalogue database suite uses uniquely named temporary schemas and needs a
role allowed to create schemas. From the root, after building/starting:

```sh
docker compose run --rm --no-deps \
  -e CATALOGUE_TEST_DB=postgresql://catalogue:catalogue@catalogue-db:5432/catalogue \
  -v "$PWD/catalogue/tests:/srv/tests:ro" \
  -v "$PWD/catalogue/pytest.ini:/srv/pytest.ini:ro" \
  -v "$PWD/catalogue/requirements-dev.txt:/srv/requirements-dev.txt:ro" \
  catalogue-migrate sh -c 'python -m pip install -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider'
```

Catalogue: 60 tests. Discovery: 49 against a dedicated disposable pgvector test DB,
with no skips. Gateway: 10 boundary tests and 6 DOM tests. Previous checks: Payment
25 including the live-broker case; Booking 6 including a 20-buyer race; Ticketing 14.
The unchanged service suites are not represented as newly rerun checks.
Commands are in the integration runbook. Dependencies can emit a non-failing
Starlette/httpx deprecation warning.

The [failure harness](tools/verify_catalogue_failures.py) creates a fresh private
Catalogue stack, injects process/broker/database failures and removes only its own
synthetic project/volumes. Generated JSON/logs must stay outside Git. Queue probes
are not actual Booking/Discovery consumers in that older harness. A separate
[Discovery integration harness](tools/verify_discovery_integration.py) now exercises
the actual Catalogue-to-Discovery consumer with separate databases, outage
fallback, restart and shared monitoring. Full-system purchase, real 500-buyer
lag, learned-model rehearsal and final submission acceptance remain pending.

## Independent verification and submission preparation

The following checks require Docker Desktop and Python 3.9+ on the host. They
create fresh private projects and remove only their own synthetic containers and
volumes. Keep result JSON and adjacent logs outside the repository:

```sh
python3 tools/benchmark_catalogue.py --output /tmp/catalogue-load-500.json
python3 tools/verify_development_startup.py --output /tmp/catalogue-startup.json
python3 -m unittest discover -s tools/tests -v
```

The default benchmark runs 500 concurrent browse clients for 30 seconds against
Catalogue directly, alongside 10 synthetic full snapshots/second. Its separate
load image uses httpx for requests, aio-pika for publication and asyncpg for
measurement; it adds no production-service dependency. The clean-start check
builds committed HEAD from an archive and checks seeds, holds, application
recreation, database recovery and monitoring. Uncommitted runtime changes are
excluded from that archive by design.

On 2026-11-17, the standalone run completed 29,598 requests without HTTP errors;
all 300 updates were observed with 22.455 ms p95 projection-write lag. See the
[workload, hardware and limits](docs/evidence/catalogue-load-startup.md). This is
Catalogue-only evidence, not the full 500-buyer or purchase acceptance test.

[contribution](docs/submission/catalogue-contribution-draft.md) drafts are ready
for student review. The [demo/viva checklist](docs/demo-viva-checklist.md) separates
implemented paths from final group work. Exact module citations, student-owned
judgments, ownership confirmation and signatures are still required.

## Discovery integration and optional real model

See [Discovery integration details](docs/contracts/discovery-integration.md) for
source attribution, payload limits, legacy-data handling and model switching.
Catalogue now rejects titles over 500 characters, descriptions over 8000, more
than 50 tags or tags over 200 characters. No existing data or immutable historical
outbox message is silently truncated or rewritten.

The default HTTP simulator runs no learned model. For a fresh real-model project,
add `-f docker-compose.ollama.yml` to the full startup command and choose a distinct
`-p` project name/free ports. First startup downloads Ollama/model assets; offline
use requires them cached. Keep simulator-bound and real-model indexes separate,
or explicitly build/activate a new index version. The real overlay is configuration-
validated; learned-model execution was not verified in this integration session.

```sh
python3 tools/verify_discovery_integration.py --output /tmp/discovery-integration.json
npm --prefix gateway ci --ignore-scripts
npm --prefix gateway run test:ui
```

Node is needed only for the jsdom development tests (supported versions are in
`gateway/package.json`); production containers remain Python. Discovery's 49-test
PostgreSQL check used a separate test database because its fixtures clear tables.
Never set `DISCOVERY_TEST_DATABASE_URL` to an application database. Visual browser
QA was blocked by Safari's locked authentication screen; the six passing DOM
checks do not claim visual/device verification.
