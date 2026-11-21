# Catalogue standalone load and clean-start evidence

Verified 2026-11-17. Application/runtime configuration source is committed
`970258f`; new verification tools are uncommitted. Runtime source was unchanged
for these checks. Raw JSON and execution logs remain outside Git. These checks
advance V03/V04/I07; they do not close full-system acceptance.

## Reproduce

```sh
python3 tools/benchmark_catalogue.py --output /tmp/catalogue-load-500.json
python3 tools/verify_development_startup.py --output /tmp/catalogue-startup.json
python3 -m unittest discover -s tools/tests -v
```

The commands create fresh randomly named projects, private databases/brokers and
loopback-only ephemeral host ports. They remove only their generated containers
and synthetic volumes afterwards. Each result has an adjacent `.log`; failure
is a nonzero exit with a failed result, not an omitted or invented measurement.
No teammate service, database or branch is modified.

## Workload and environment

The standalone benchmark used 500 concurrent closed-loop client tasks for a
30-second measurement window, with 0.2 seconds of think time between
requests. Each client issues one event-list request followed by four detail
requests, repeatedly, against Catalogue directly. There are 20 synthetic events,
100 seats per event, and a synthetic producer sending 10 full availability
snapshots per second. Each event receives an initial snapshot/detail warmup
before measurement. The production detail cache keeps its default 0.5-second TTL.

The generator runs in a separate Python container on the same Docker network and
shares the same machine resources with the system under test. Docker Desktop
29.7.2 reported 10 virtual CPUs, 7.75 GiB memory and `aarch64`.
Application versions: FastAPI 0.162.7, Uvicorn 0.54.0, asyncpg
0.32.0 and aio-pika 10.2.3. There were no previously running
containers before the independent checks began; the clean-start check ran after
the benchmark completed.

## Measured results

- All 500 client tasks started; peak outstanding client requests: 500.
- 29,598 HTTP requests completed, all with status 200; zero request/validation
  errors. Actual completion interval: 30.421 seconds, including the tail of
  requests begun inside the measurement window. Observed throughput was
  972.9 requests/second under this closed-loop workload.
- Client HTTP latency: p50 214.4 ms, p95 889.8 ms,
  p99 1388.6 ms, maximum 4146.5 ms. These include connection
  acquisition, network, full response and JSON validation; they are not just the
  server's handler execution time.
- All 300 generated updates were observed in the projection audit; none missing.
  Projection-write lag: p50 16.301 ms,
  p95 22.455 ms, p99 30.001 ms,
  maximum 46.724 ms. No negative lag samples occurred.
- Producer schedule delay p95: 6.64 ms. Every scheduled update was
  generated and observed. No fixed-rate HTTP arrival or capacity limit is inferred.

Percentiles use the nearest-rank empirical definition over all samples. Empty
samples remain unknown and nonfinite samples are rejected; three host-side unit
checks verify the reporting boundary and outside-repository result restriction.

## Measurement limits

Lag uses the synthetic source timestamp and projection `applied_at`, both from the
same test PostgreSQL clock. A temporary AFTER INSERT/UPDATE audit trigger captures
every projection change and adds one insert per applied update. It exists only
in the disposable test database. Rows are read after commit, but the timestamp
itself is the projection-write timestamp before transaction commit. This matches
the current application metric's basis; it is not a commit-visibility measurement.

The p95 result is below two seconds for this Catalogue slice. It excludes actual
Booking hold processing, Booking's source transaction/outbox, distributed clock
offsets, gateway/UI latency and cache-visible freshness. Full snapshots come from
a synthetic producer; these are 500 browse clients, not 500 completed purchases.
The closed-loop generator waits between requests and can reduce its offered rate
when responses slow. It shares CPU/memory with the service, is not independently
calibrated and does not establish maximum capacity or production performance.
The single 30-second run is baseline evidence, not a soak/repetition study.
V03's real 500-buyer requirement therefore remains open.

## Clean committed-source startup and recovery

The second harness extracted only committed HEAD `970258f` into a temporary source
archive, then built and started the development/monitoring overlays with empty
private volumes. No untracked application file or host Python environment was
needed. Builds reused available images/package caches; offline clean builds and
full version locking were not established.

The passing assertions cover:

- Four real business implementations ready, Discovery explicitly in bootstrap
  mode, three successful one-shot jobs, five UP scrape targets and both dashboards.
- Two seeded events, null availability, gateway browse/hold, stable hold retry,
  competing-buyer 409, and checkout/search capabilities correctly unavailable.
- Catalogue seed rerun preserves an API-edited title and exactly three outbox
  rows; Booking's own seed preserves the held seat/reference.
- Forced recreation of Catalogue, Booking, Payment, Ticketing and gateway
  preserves owned state and Payment's saved idempotent result. Monitoring recovers;
  default Catalogue messaging stays OFF.
- All four owned database containers can be stopped independently. Application
  liveness stays up, readiness returns 503, and restarting databases restores
  readiness/state and all five monitoring targets.

These checks harden the implemented development baseline. They do not show a
complete purchase, Booking expiry/refund saga, Discovery AI, broker-container
replacement persistence or accepted cross-service contracts. V04/I07 remain open.
