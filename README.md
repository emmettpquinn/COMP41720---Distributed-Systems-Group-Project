# Ticket Booking Platform

COMP41720 Distributed Systems group project.

See [project continuity and checkpoints](docs/PROJECT_CONTINUITY.md) for the
recovered plan, verified milestones, integration dependencies and next step.
The [proposed Catalogue contracts](docs/contracts/catalogue.md) provide HTTP/event
examples for consumer review; they do not add endpoints or establish team agreement.

## Docker setup on this branch

This README describes local startup on `feature/catalogue-integration`. Services
are developed across separate branches, so the containers included here describe
this branch's integration setup rather than the team's overall progress.

The base Compose file starts two application services: Ticketing and Notification,
and Event Catalogue. Each has its own PostgreSQL database.
RabbitMQ, a topology initialization job and MailHog support them. An optional
development overlay adds health-only shells for Booking, Discovery and Payment.

Catalogue in this branch exposes health and database-readiness endpoints, with
versioned storage for venues, events and event seat maps. Business endpoints are
the next checkpoint. Booking, Discovery and Payment entries in the development
overlay provide startup checks while service configurations are brought together from their development
branches. The provider simulator, Ollama, gateway/UI and seed data can be connected
as part of the shared integration work. An optional monitoring overlay connects
Prometheus and Grafana to the exporters currently available in this branch.

The Ticketing service and shared messaging baseline come from commit
`bb17236`. Catalogue and shared startup additions are developed on
`feature/catalogue-integration`; teammate branches are not modified.

## Start locally

Install Docker Desktop with Docker Compose support, and make sure Docker Desktop
is running. From the repository root:

```sh
docker compose up -d --build --wait --wait-timeout 180
```

The first build downloads images and Python packages. Offline startup requires
these to be cached. No paid API credentials are needed. Database credentials in
Compose are local development values; use synthetic data only.

For an isolated development stack, use a project name such as `catalogue-dev`.
Include the same `-p` option after `docker compose` in every command. For example:

```sh
docker compose -p catalogue-dev up -d --build --wait --wait-timeout 180
```

Use one project at a time: two stacks would compete for the same host ports.

## Five-service development startup

To start all five application containers, explicitly include the development
overlay with the base file:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait --wait-timeout 180
```

If you already use a named project, include that same `-p` option to extend its
existing stack. This overlay preserves Catalogue, Ticketing and their databases;
it adds Booking on port 8002, Discovery on 8003 and Payment on 8004. All containers
share the default Compose network.

The three added containers use the shared development image recipe under
`infra/bootstrap/`. They expose only `GET /health`, with an explicit identity:

```json
{"service": "booking", "mode": "bootstrap", "status": "ok"}
```

These responses are container startup checks. In this overlay, `/health` is the
only endpoint; `/ready` and business endpoints return 404. The temporary shells
have no database or messaging connections. Booking, search and payment behaviour
comes from each service's own implementation when its container configuration is
integrated, along with its database and messaging requirements.

Use the same file options when inspecting or stopping the development stack:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps -a
curl -fsS http://localhost:8002/health
curl -fsS http://localhost:8003/health
curl -fsS http://localhost:8004/health
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs --tail 50 booking discovery payment
docker compose -f docker-compose.yml -f docker-compose.dev.yml stop
```

The shells also carry the Docker label `com.citytickets.mode=bootstrap` and identify
themselves in startup logs. To use a service implementation from its development
branch, update the corresponding overlay entry with that service's Dockerfile,
configuration, readiness check and database requirements. Keep the shared bootstrap
application focused on startup checks. The overlay does not modify service
application files.

## Monitoring startup

The monitoring overlay reuses the pinned images and Grafana datasource from
commit `63b8b0e`, with an adjusted scrape configuration for this branch. Start it
with the base stack:

```sh
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d --build --wait --wait-timeout 180
```

To include the five application containers as well:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.monitoring.yml up -d --build --wait --wait-timeout 180
```

Keep the same project name and file options for subsequent Compose commands.
Prometheus is available at http://localhost:9090 and Grafana at
http://localhost:3000. These two ports bind to the local machine only. Override
`PROMETHEUS_PORT` or `GRAFANA_PORT` if they are occupied. The local Grafana login
is `admin` / `stagedoor-demo`; set `GRAFANA_PASSWORD` before the first startup to
use a different password. Existing Grafana volumes retain their original login.

Grafana's default `Prometheus` datasource is provisioned automatically. Prometheus
scrapes itself, Ticketing and RabbitMQ every five seconds. Use Grafana Explore or
the Prometheus query page to inspect `up` and Ticketing's `http_requests_total`.
Catalogue and the three bootstrap shells do not export metrics yet, so they are
not configured as scrape targets. The Payment scrape target and its dashboard
remain with the Payment implementation until that service is integrated; they
are not replaced with fabricated metrics from the shell. A complete group
dashboard, Catalogue metrics and AI token-cost panels remain later checkpoints.

Prometheus and Grafana each have a named data volume. Restarting their containers
preserves stored settings and time-series data. Check their health with:

```sh
curl -fsS http://localhost:9090/-/ready
curl -fsS http://localhost:3000/api/health
curl -fsS 'http://localhost:9090/api/v1/targets?state=active'
```

The ports above assume no overrides. These checks confirm the monitoring wiring;
they do not establish the full application resilience or performance targets.

## Check the services

```sh
docker compose ps -a
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8001/ready
curl -fsS http://localhost:8005/ready
```

- Catalogue: http://localhost:8001/docs
- Ticketing API: http://localhost:8005/docs
- MailHog inbox: http://localhost:8025
- RabbitMQ management: http://localhost:15672 (local login: `guest` / `guest`)
- RabbitMQ metrics: http://localhost:15692/metrics

`topology-init` and `catalogue-migrate` should exit with code 0; they are one-time
jobs, not long-running services. Catalogue `/health` checks the application process.
`/ready` executes `SELECT 1` against Catalogue's database within a bounded timeout; it returns 503
when the database is unavailable. It does not claim messaging or business readiness.
The current shell does not connect to RabbitMQ, although Compose waits for topology
initialization so future consumer startup can use the same ordering.

Catalogue settings are `DATABASE_URL` and `DATABASE_TIMEOUT_SECONDS` (default 2).
The app uses a small asyncpg connection pool. Its database is reachable only inside
the Docker network and has a dedicated `catalogue-db` named volume; Ticketing keeps
its existing database and volume. Named database volumes survive container restarts
and ordinary Compose shutdown. RabbitMQ message persistence across container
recreation is not established by this configuration.

## Catalogue database and migrations

Catalogue has its own `catalogue-db` PostgreSQL container and named volume. It
stores `venues`, `events` and `event_seats`, plus a `schema_migrations` ledger.
Seat-map rows define seat IDs, sections and labels; Booking still owns holds,
free/held/booked inventory and bookings in its own database.

`catalogue-migrate` runs the SQL files from `catalogue/migrations/` after PostgreSQL
is healthy and before the Catalogue application starts. Applied filenames are
recorded, so repeated startup preserves existing rows. The runner serializes
concurrent migrations with a database advisory lock and commits all pending SQL
and ledger entries together; failure rolls that transaction back. Applied SQL
files are immutable: add the next numbered migration rather than editing history.

To apply migrations separately to an already configured Catalogue database:

```sh
docker compose run --rm catalogue-migrate
docker compose exec -T catalogue-db psql -U catalogue -d catalogue -c '\dt'
```

Use the same project/file options as startup. No host database port is published.
The schema enforces venue/event references, unique seat IDs per event, nonblank
required text, positive event versions and allowed event states. `TIMESTAMPTZ`
stores event instants; HTTP offset/IANA-timezone validation, version increments
and nonempty seat-map creation will be implemented with their API checkpoints.
An outbox and availability projection are separate later migrations.

## Logs and shutdown

```sh
docker compose logs --tail 100 catalogue catalogue-db
docker compose stop
```

`stop` retains containers and volumes. `docker compose down` removes this project's
containers/network and retains named database volumes. Do not add `-v` unless you
intend to delete that project's stored data.

## Catalogue tests

Use Python 3.12, matching the Docker image. Run these separately from Ticketing's
tests so the two service-local `app` packages stay isolated:

```sh
cd catalogue
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The health tests use a fake pool to check liveness independence, readiness,
acquisition/query timeouts and safe error responses. Database tests use a real
PostgreSQL URL from `CATALOGUE_TEST_DB`; without it those tests are explicitly
skipped. Each database test creates and removes a uniquely named schema, leaving
existing Catalogue rows alone. The database role needs permission to create schemas.

To run the full suite in Python 3.12 against the private Catalogue database, first
start/migrate the stack, then run from the repository root:

```sh
docker compose run --rm --no-deps \
  -e CATALOGUE_TEST_DB=postgresql://catalogue:catalogue@catalogue-db:5432/catalogue \
  -v "$PWD/catalogue/tests:/srv/tests:ro" \
  -v "$PWD/catalogue/pytest.ini:/srv/pytest.ini:ro" \
  -v "$PWD/catalogue/requirements-dev.txt:/srv/requirements-dev.txt:ro" \
  catalogue-migrate sh -c 'python -m pip install -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider'
```

Again, include the startup project's `-p`/`-f` options when applicable. The suite
checks migration repeatability, rollback, concurrent runners, references, field
constraints and persistence across connections. Container restart/persistence is
also verified separately as a live Docker check.
