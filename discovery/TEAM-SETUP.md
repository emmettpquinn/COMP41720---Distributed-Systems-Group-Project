# Discovery: team setup and integration

Discovery provides date-filtered semantic search, similar events, labelled keyword fallback, versioned indexes and metrics. It owns its PostgreSQL/pgvector projection and consumes `event.changed` from RabbitMQ. It does not authorize purchases or check seat availability.

## Independent demo

Run from the repository root with Docker Desktop running:

```powershell
docker compose -p discovery-real -f discovery/compose.standalone.yml -f discovery/compose.real-model.yml -f discovery/compose.monitoring.yml up --build -d
```

Search API: http://localhost:8004/docs
Dashboard: http://localhost:3004/d/discovery/discovery

First startup downloads images, dependencies and Ollama's nomic embedding model. Search can return keyword fallback until the model and embeddings are ready. The independent stack publishes synthetic events. Existing volumes keep original event dates; old fixtures may no longer match upcoming-date searches. Do not delete existing volumes merely to refresh fixtures.

To stop this project while retaining its data:

```powershell
docker compose -p discovery-real -f discovery/compose.standalone.yml -f discovery/compose.real-model.yml -f discovery/compose.monitoring.yml stop
```

## Integrated Catalogue setup on this branch

The base Compose file now includes real Discovery with a private pgvector DB and
an explicit HTTP model simulator. Booking also runs from the main Compose file;
the development overlay is retained for compatibility. Start the integrated stack with:

```sh
CATALOGUE_MESSAGING_ENABLED=true docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.monitoring.yml up -d --build --wait --wait-timeout 240
```

Catalogue is on port 8001, Discovery on 8003. The independent demo above uses 8004,
which conflicts with integrated Payment; keep the projects/ports separate. The
shared gateway/browser and monitoring now include Discovery. Default simulation
is explicitly labelled and does not run a learned model. For real Ollama, add the
root `docker-compose.ollama.yml` overlay on a fresh project or build a new index
version for existing data; do not delete volumes to switch model identity.

Payloads use `change` (created/updated/cancelled); legacy demo `action` remains
supported. Conflicting action/change/status is rejected. Catalogue inputs now
match the consumer's title/description/tag limits; existing historical oversized
envelopes are not rewritten. Booking has accepted the Catalogue event and
availability contracts and now consumes the shared event messages.
See [integration details](../docs/contracts/discovery-integration.md).

## Frontend and monitoring

Call GET `/search?q=...` or GET `/events/{event_id}/similar`. Render the returned `label`, particularly `Showing keyword results`, and retain `mode`/`fallback_reason` for diagnostics. The root gateway/UI integrates these read routes and their labels.

Metrics are exposed at `/metrics`; Grafana panels and scrape configuration live in `monitoring/`. The root monitoring overlay provisions Discovery beside the existing dashboards
and scrapes the integrated service; independent monitoring remains separate.

Use one API worker: the four-call bulkhead and circuit breaker are process-local. The default Ollama timeout is 1.5 seconds. Admin index routes require `X-Admin-Token`; the Compose default is a local development token, not a deployment secret.

## Automated checks

Use Python 3.12 and run from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r discovery/requirements-lock.txt
.\.venv\Scripts\python.exe -m pytest -c discovery/pytest.ini discovery/tests -q
```

Without `DISCOVERY_TEST_DATABASE_URL`, one PostgreSQL-only concurrency test skips. CI supplies a disposable database and runs all tests against PostgreSQL. Never set that variable to a working application database: the test fixture clears Discovery tables.

With the real Catalogue integration running:

```powershell
.\.venv\Scripts\python.exe -m discovery.scripts.verify_catalogue --output work/catalogue-check.json
```

This creates a unique synthetic event through Catalogue, checks its visibility and update, then cancels it and checks disappearance. It leaves the cancelled record for audit. Override `--catalogue` and `--discovery` for different ports.

## Verified scope on 16 November 2026

All 46 tests passed against a separate PostgreSQL test database. A live Catalogue 7b9524c -> RabbitMQ -> Discovery create/update/cancel check passed in an isolated checkout using simulated embeddings. That integration check shared one disposable PostgreSQL database with distinct service tables; it does not establish the intended database isolation or full group startup. Earlier independent tests used real Ollama with synthetic messages. A combined real-Catalogue/real-Ollama rehearsal, shared frontend and agreement on payload limits remain team integration work.

Integration verification on 2026-11-17 is documented in the root runbook; the
upstream 16 November observations above retain their original scope.
