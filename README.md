# Ticket Booking Platform

COMP41720 Distributed Systems group project. This README describes integration
on `feature/catalogue-integration`; services are also developed on separate
branches. See the [checkpoint tracker](docs/PROJECT_CONTINUITY.md),
[Catalogue runbook](docs/catalogue-operations.md) and
[development integration runbook](docs/integration-development.md).

## What runs here

The base stack starts Catalogue, Ticketing/Notification and Payment, each with
its own PostgreSQL database, plus RabbitMQ, MailHog and the gateway/browser UI.
The development overlay adds Booking's real hold API and its private database,
plus a clearly labelled Discovery health-only shell. The monitoring overlay adds
Prometheus and Grafana. The gateway is an extra infrastructure component, not one
of the five business services.

Catalogue provides versioned event/venue APIs, seat-map definitions, an atomic
outbox, availability projection, bounded detail cache, synthetic seeds and metrics.
Payment handles its simulated ledger/refunds/outbox; Ticketing handles tickets and
emails. Booking currently provides holds only: no expiry, checkout/status API,
saga, event consumer or availability publisher. Discovery's AI/search integration
is pending on this branch. This stack does not yet perform a complete purchase.

Source credits: Ticketing/shared messaging `bb17236`, Payment `63b8b0e`, Booking
hold source `7996674`. Integration changes are recorded in the runbook; teammate
branches are untouched. Candidate Catalogue contracts still need consumer
agreement, so `CATALOGUE_MESSAGING_ENABLED` defaults to false.

## Start locally

Install Docker Desktop with Compose support and start Docker Desktop. From the
repository root, for the base stack:

```sh
docker compose up -d --build --wait --wait-timeout 180
```

For all five business containers, browser UI and monitoring:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.monitoring.yml up -d --build --wait --wait-timeout 180
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
- Ticketing API: http://localhost:8005/docs and `/metrics`
- RabbitMQ management: http://localhost:15672 (`guest` / `guest`)
- RabbitMQ metrics: http://localhost:15692/metrics
- MailHog inbox: http://localhost:8025

The development overlay adds Booking at http://localhost:8002/docs and Discovery's
bootstrap `/health` at http://localhost:8003/health. Discovery has no business API.
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
Checkout/search are unavailable until their service APIs are integrated. Current
Booking holds do not expire; use synthetic data for this development flow.

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

Five exporters are configured: Catalogue, Payment, Ticketing, RabbitMQ and
Prometheus. The datasource and separate Catalogue/Payment dashboards are
provisioned automatically. Booking's imported hold API and the Discovery shell
have no metrics target. Catalogue lag panels have 2/10-second reference thresholds;
notification alert rules and group AI-cost panels remain open.

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

Catalogue: 54 tests at last verification. Payment: 25 including the optional live
broker test enabled for the integration check. Gateway: 7 boundary tests. Booking:
6 added integration tests including a 20-buyer race and non-resetting seeds.
Commands are in the integration runbook. Dependencies can emit a non-failing
Starlette/httpx deprecation warning.

The [failure harness](tools/verify_catalogue_failures.py) creates a fresh private
Catalogue stack, injects process/broker/database failures and removes only its own
synthetic project/volumes. Generated JSON/logs must stay outside Git. Queue probes
are not actual Booking/Discovery consumers. Full-system purchase, 500-buyer lag,
Discovery AI and final submission acceptance remain pending.
