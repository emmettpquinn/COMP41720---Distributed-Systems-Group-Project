# Ticket Booking Platform

COMP41720 Distributed Systems group project.

## Current implementation

Docker starts two application services: Ticketing and Notification, and
the Event Catalogue startup shell. Each has its own PostgreSQL database.
RabbitMQ, a topology initialization job and MailHog support them.

Catalogue currently provides health and database-readiness checks only. Event
APIs, event tables and messaging are later checkpoints. Booking, Discovery,
Payment, the provider simulator, Ollama, gateway/UI, seed data and
Prometheus/Grafana are not integrated into this Compose stack yet.

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

`topology-init` should exit with code 0; it is a one-time job, not a long-running
service. Catalogue `/health` checks the application process. `/ready` executes
`SELECT 1` against Catalogue's database within a bounded timeout; it returns 503
when the database is unavailable. It does not claim messaging or business readiness.
The current shell does not connect to RabbitMQ, although Compose waits for topology
initialization so future consumer startup can use the same ordering.

Catalogue settings are `DATABASE_URL` and `DATABASE_TIMEOUT_SECONDS` (default 2).
The app uses a small asyncpg connection pool. Its database is reachable only inside
the Docker network and has a dedicated `catalogue-db` named volume; Ticketing keeps
its existing database and volume. Named database volumes survive container restarts
and ordinary Compose shutdown. RabbitMQ message persistence across container
recreation is not established by this configuration.

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

The tests use a fake pool to check liveness independence, readiness, acquisition/query
timeouts and safe error responses. Live PostgreSQL startup, outage/recovery and
volume checks are separate Docker integration checks.
