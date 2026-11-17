# Gateway, browser and Booking development integration

The base stack runs Catalogue, Ticketing, Payment and a gateway. The development
overlay adds the Booking hold implementation and its database; Discovery remains
a labelled health-only shell. This is four real business implementations plus a
shell, not the completed five-service booking workflow.

## Source and changes to discuss with the team

Payment source is imported from `63b8b0e`; see the [contract review](contracts/payment-integration.md).
Booking's `api.py`, `main.py`, `db.py`, `config.py`, `schema.sql` and requirements
come from `7996674`. Integration edits add a Dockerfile, database readiness,
bounded request-time database connections, safe 503 responses and an opt-in
non-resetting synthetic seed. The conditional seat-hold SQL is preserved.
Neither source branch is modified. These are source imports, not Git merges.

The Booking database is private and has its own named volume. Only Booking code
writes its inventory. `BOOKING_DEMO_SEED=true` in the development overlay loads the
same fictional event/seat IDs as Catalogue, from the shared seed JSON file at
build time. `ON CONFLICT DO NOTHING` preserves existing holds/booked rows on
restart. Set the variable to false to disable this development bootstrap.
Catalogue does not initialize another service's database.

This explicit demo seed is temporary until Booking consumes accepted
`event.changed` snapshots. The imported source has no consumer, expiry/saga,
availability publisher or checkout/status routes. Its holds do not expire yet.
Catalogue therefore correctly keeps availability null in this demonstration.
Do not describe this fixture path as asynchronous inventory integration.

## Browser flow

Open http://localhost:8080 after starting the development overlay. `GATEWAY_PORT`
changes this host port. The gateway binds to loopback and serves static HTML/CSS/JS
and fixed same-origin routes:

- `GET /api/catalogue/events` and `/api/catalogue/events/{event_id}`.
- `POST /api/booking/holds`, enabled in the development overlay.
- `GET /api/ticketing/tickets/{booking_id}` for future status integration.
- `GET /api/capabilities` describes available browser operations.

The proxy forwards only these methods/paths to configured origins, applies a
three-second upstream timeout, limits request bodies to 64 KiB and does not
forward cookies/authorization or follow redirects. It never automatically retries
a hold. A timeout/503 can mean an unknown outcome; manual retries reuse this
browser tab's stable synthetic customer ID. A separate tab may be a different
customer. No paid provider or external AI is called by this UI.

Browse cards show venue-local dates and cancellation state. Detail includes the
seat map and source timestamp/count when a projection exists. Unknown availability
is labelled unknown, not zero or all-free. Seat selection asks authoritative
Booking for a hold. Success displays its actual `booking_ref`; it is not a ticket.
A 409 refetches Catalogue's projection and marks the refused choice locally even
if that projection still lags. The backend's old response text alone is not used
to assert that authoritative free seats were returned. A 404 explains missing
Booking inventory. Checkout/search stay unavailable until real APIs exist.

## Checks

Gateway boundary tests:

```sh
docker compose run --rm --no-deps \
  -v "$PWD/gateway/tests:/srv/tests:ro" \
  gateway python -m unittest discover -s tests -v
```

Booking's integration tests (include the development overlay and startup project):

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm --no-deps \
  -e BOOKING_TEST_DB=postgresql://booking:booking@booking-db:5432/booking \
  -v "$PWD/booking/tests:/srv/tests:ro" \
  booking sh -c 'python -m pip install httpx && python -m unittest discover -s tests -v'
```

`httpx` is the test client dependency; production Booking requirements stay as
imported. These six checks include a 20-customer race, repeat-safe holds/seeds,
404/422 responses and safe database-outage readiness.

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

## Verified development preview

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
