# Project continuity

Last reviewed: 2026-11-17. Implementation/test evidence below is from 2026-11-14. Working branch: `feature/catalogue-integration`.

## Objective and source of truth

Build the COMP41720 Ticket Booking Platform under Option A: five independently
deployable services, meaningful asynchronous work, documented consistency and
resilience demonstrated under failure, reproducible local containers, and metrics.
Use synthetic data and free/local infrastructure. Discovery provides the AI feature.

The supplied project brief rev7 defines assessment requirements. The supplied
team plan defines service responsibilities and target behaviour. Repository code
and executed checks determine implementation status; remote branch absence does
not establish whether work exists elsewhere.

Required submission: an at-most-eight-page core report including diagrams; five
one-page ADRs outside that limit; AI-disclosure appendix; a signed one-page
contribution statement. Deadline is end of Week 12; the calendar date is not
established here. Students retain ownership of architectural justification and
critical evaluation; this document records implementation evidence, not ADR arguments.

## Architecture and recovered decisions

- Catalogue owns venues, versioned events, seat maps, an availability projection
  and an event outbox. Booking alone owns authoritative inventory, holds and the saga.
- Discovery owns its versioned search index in PostgreSQL/pgvector and calls Ollama.
  Payment owns its ledger, refunds, idempotency and outbox. Ticketing owns tickets,
  processed-message records and email delivery state. Each service owns its database.
- Browser traffic goes through a planned gateway to Catalogue, Discovery and
  Booking. Booking calls Payment. Catalogue does not synchronously call Booking.
- The team plan uses asynchronous availability to avoid coupling busy browse pages
  to Booking. Target event-to-projection lag is at most 2 seconds at p95, warning
  above 10 seconds. Booking still rejects unavailable seats. Discovery may lag 30 seconds.
- The planned saga has 10-minute holds, a 5-second expiry sweeper and handling of
  late payment success through re-hold or refund. These are planned behaviours,
  not claims that the current Booking skeleton implements them.
- RabbitMQ delivery is at least once. Producers that save state then publish need
  transactional outboxes; consumers record message IDs atomically and ignore stale
  aggregate versions/sequences. Retry/DLQ handling is shared.
- Python/FastAPI is confirmed. Catalogue uses asyncpg and versioned SQL migrations,
  with a one-shot migration job before application startup. It has its own PostgreSQL
  container/volume; migrations now store metadata, outbox, availability and inbox IDs.
- Preserve small checkpoints. C02 was absorbed into INF02; I01 was absorbed into
  the infrastructure wiring checkpoints. INF03 was explicitly limited to health-only
  development shells; their databases and business behaviour accompany real integration.

Personal scope: Catalogue; overall Compose startup, synthetic seeds, gateway/basic
UI, startup documentation, Catalogue metrics/failure evidence, ADR-002,
requirements/constraints contribution and final report editing. Message contract
maintenance belongs to Booking; topology to Ticketing; monitoring base to Payment;
AI/indexing to Discovery. Coordinate shared changes rather than replacing their work.

Keep new file contents free of personal names. Describe this branch's integration
status neutrally. Keep transcripts, temporary logs, secrets and generated evidence
out of commits. Commit only reviewed checkpoint files with explicit authorization;
never push automatically or rewrite shared history.

## Branch review and integration state

| Branch/ref | Evidence at last fetch | Result and integration action |
|---|---|---|
| `origin/master` | `be6954d` merges Ticketing; tree equals `bb17236` | Safely merged into this branch as `352e117`; no source changes from that merge. Local `master` was not switched or updated. |
| `origin/feature/ticketing-notification` | `bb17236` | Already inherited. Ticket API, SQLAlchemy/PostgreSQL state, shared topology/consumer/replay, Docker and runbook. Fourteen SQLite/fake-mail tests passed again. |
| `origin/booking/skeleton` | `7996674` | Hold source imported into this branch with Docker, bounded DB requests/readiness and explicit non-resetting demo seed. Six added isolated-schema tests pass. No expiry/saga/outbox/consumer or checkout/status API. |
| `origin/feature/payment-service` | `63b8b0e` | Payment source/Docker/tests/dashboard imported with reconciled root Compose. All 25 tests pass including real-broker refunds. No Git merge or upstream branch modification. Booking acceptance remains pending. |
| `origin/feature/catalogue-integration` | `7b9524c` | Local/remote matched at batch start. The second batch is included in the newly authorized local commit; no push is authorized. |

Payment exposes `POST /payments`, `GET /payments/{payment_id}` and
`POST /payments/{payment_id}/refunds`, plus health/readiness/metrics. HTTP amounts
are integer minor units; event amounts are decimal strings. HTTP states are
`paid`, `failed`, `refunded`; duplicate booking IDs reuse the stored result and
changed amount/currency returns 409. Tables: `payments`, `processed_messages`,
`outbox`; startup also migrates the prior five-table layout.

Payment consumes `refund.requested`; publishes `payment.result`, `refund.completed`
and `refund.failed` through its outbox, using the existing shared topology.
Its README labels payloads proposed. The current HTTP call returns the completed
simulated result rather than the team's planned immediate accepted response;
the simulator is in-process, and planned unknown-outcome reconciliation, separate
provider container and late-success timing remain integration/requirements work.
Do not interpret passing local tests as a complete Booking saga demonstration.

The latest fetch found no new teammate commits. The current batch imports the
smallest useful Payment service/dashboard paths and Booking hold source into this
branch, preserving Catalogue/Ticketing definitions and shared messaging. Root
Compose and dashboard scan paths are reconciled explicitly. Upstream broker-volume
changes are deferred. There is no pending merge or history rewrite.
See [Payment review](contracts/payment-integration.md) and the
[development runbook](integration-development.md) for exact source/edit boundaries.

The base now runs Catalogue, Payment and Ticketing plus gateway/UI. Development
adds the real Booking hold API/private DB and retains only Discovery as a shell.
Booking's explicit demo seed uses the same synthetic fixture file through its own
DB code; no cross-service DB writes occur. It does not replace message ingestion.
Five exporters and separate Catalogue/Payment dashboards are live; Booking/AI
metrics and full-system monitoring remain open.

## Important contracts and unresolved questions

Shared exchange `events` is durable/topic; routing key equals message type. `dlx`
is durable/direct. Each consumer queue has three retry queues (1/2/4-second TTLs)
and a DLQ. Envelope: `message_id`, `type`, `occurred_at`, `version`, `producer`,
`payload`. See [messaging](messaging.md) and `shared/messaging/` for current code.

- Catalogue publishes `event.changed` to Booking/Discovery; consumes
  `seat.availability-changed` on `catalogue.seat-availability`.
- Catalogue event payloads, availability payloads, bootstrap/replay rules, unknown
  events, cancellation and seat-map mutation rules remain unagreed in fetched code.
  A draft is now prepared in [Catalogue contracts](contracts/catalogue.md), with
  synthetic JSON examples under `catalogue/examples/contracts/`; it is not accepted
  and still requires consumer agreement.
- Agree snapshot versus delta and sequence reset/scope before shared integration.
  The local candidate implements full snapshots and monotonic per-event sequences. Skipping old deltas can lose changes; full snapshots
  have different semantics. A snapshot design remains a proposal, not a recovered agreement.
- Distinguish envelope/schema version, Catalogue event version and Booking's
  per-event availability sequence. The shared envelope alone does not define these.
- Booking's response uses `booking_ref`; downstream messages expect `booking_id`.
  Agree mapping and customer/event metadata flow. Current hold refusal lacks the
  free-seat list expected by the planned UI.
- Shared consumer code makes one attempt plus three retries; the team plan's
  wording about three failures needs reconciliation.
- Full-group architecture still needs zero-cost AI/index versioning/fallback,
  outbox crash proofs, 500-buyer tests, failure evidence and the final dashboard.

## Checkpoint tracker

Statuses apply to this branch unless otherwise stated. Estimates are implementation
sessions, usually 30-90 minutes; split a task further if it grows beyond one session.

Progress: all 32 original checkpoints are retained. 25 have delivered local
code, reviews or evidence (including eight delivered scopes in the current
batch); C01 awaits contract acceptance, and six remain open: I06, I07, V03, V04,
D02 and D03. Several delivered local scopes still need peer acceptance, as shown
below. This is not a group completion percentage. The current batch is included
in the newly authorized local commit. I07 and D02 remain partial/draft.

| Checkpoint | Original objective | Current status | Evidence | Next action |
|---|---|---|---|---|
| INF01 | Reuse Docker baseline safely | Complete | Inherited `bb17236`; README/build ignore in `be21a88` | Preserve baseline ownership/boundaries. |
| INF02 | Catalogue shell and own database | Complete | `be21a88`; 5 tests and historical live outage/recovery checks | Extend through Catalogue checkpoints. |
| INF03 | Five-container development startup | Complete, development scope only | `ed6b9f9`; labelled shells under `infra/bootstrap/` | Replace shells with reviewed owner implementations. |
| INF04 | Wire monitoring containers/datasource | Complete, wiring scope only | `852ce2d`; Catalogue target/panels added in current batch | Full group monitoring remains I07. |
| INF05 | Verify/document development baseline | Complete, development scope only | `852ce2d` source archive starts successfully; initially empty isolated volumes and later container recreation checked | Full business startup remains V04. |
| C01 | Propose Catalogue API/event contracts | Draft complete; consumer agreement pending | Candidate contracts, examples and gated implementation | Obtain consumer acceptance before enabling on shared broker. |
| C03 | Persist venues/events/seat maps | Complete | `1c109a1`; immutable migration 001; live persistence/repeat checks | Preserve migration history. |
| C04 | Event list/detail APIs | Implemented | PostgreSQL reads, consistent detail snapshot, 404 and unknown-availability checks | Review candidate response shapes. |
| C05 | Event creation plus atomic outbox | Implemented | Venue/event APIs, seat map and immutable message in one transaction; rollback tests | Review candidate create fields. |
| C06 | Versioned event updates | Implemented | Row lock, expected-version conflict, no-op and concurrent-update tests | Keep ID/venue/seat-map changes excluded. |
| C07 | Event cancellation intent | Implemented | Cancelled snapshot, no reactivation, repeat/no-op tests | Booking lifecycle propagation awaits integration. |
| C08 | Reliable Catalogue outbox dispatcher | Implemented; gated | Confirmed mandatory publishing, SKIP LOCKED, stable-ID retries, live marker-failure/outage probes | Accept contract; process-kill crash proof remains V01. |
| C09 | Ordered/idempotent availability projection | Implemented; candidate | Atomic inbox/projection, full snapshots, duplicate/reordering/concurrency tests | Agree durable sequence/reset and backfill rules. |
| C10 | Availability consumer wiring | Implemented; disabled by default | Shared retries/DLQ, isolated broker duplicate/invalid-message checks | Test actual Booking producer after agreement. |
| C11 | Busy-page cache and freshness | Implemented | Bounded TTL/deep-copy cache, commit invalidation and in-flight generation guard | Evaluate measured lag with real workload. |
| C12 | Catalogue metrics and panel | Implemented | HTTP/lag/outbox/cache/health metrics, four UP scrape targets, eight-panel dashboard | Alert notifications and group/AI panels remain open. |
| I02 | Repeatable synthetic seeds | Implemented; peer bootstrap pending | One venue/two events/four seats, own write/outbox path, atomic repeat-safe seed job | Verify real Booking/Discovery consume seeds. |
| I03 | Gateway routes and timeouts | Implemented | Fixed same-origin proxy, bounded calls/body, no hold auto-retry; seven tests | Extend only when agreed APIs exist. |
| I04 | Browsing UI and freshness labels | Implemented | Event/detail/seat map, explicit unknown projection, venue-local dates; Safari smoke check | Connect accepted live availability and Discovery later. |
| I05 | Refused-hold refresh UI | Local flow implemented | Real competing-buyer 409 refetches projection and marks refused choice; safe retry reference | Authoritative refreshed free-seat payload still needs Booking agreement. |
| I06 | Checkout/status UI | Pending; unavailable in UI | Current Booking source has no checkout/status API or saga | Agree API, pricing ownership and ID mapping before implementation. |
| I07 | Complete real-service startup | Partial | Four real business implementations/private DBs plus Discovery shell; demo holds work | Integrate Discovery, Booking saga/consumers/provider; replace fixture bootstrap. |
| V01 | Prove event-outbox crash recovery | Verified Catalogue scope (harness included) | Real SIGKILL before publish and after confirmation; stable-ID recovery to both queues | Real downstream idempotency acceptance remains open. |
| V02 | Prove 60-second outage/reordering recovery | Verified Catalogue scope (harness included) | 60.001 s outage, 29 successful browse probes; DB restart and sequence 101 recovery | Repeat with actual Booking producer and end-to-end workload. |
| V03 | Measure lag under 500 buyers | Pending, 60-90 min | No complete inventory/projection workload | Measure <=2 s p95; capture workload/hardware. |
| V04 | Verify full-system clean startup/runbook | Pending, 60-90 min | Development startup is not full booking flow | Clean clone, real seeds and end-to-end purchase/failure demo. |
| D01 | ADR-002 evidence | Evidence prepared | Reproducible fault harness, measured observations and limits in docs/evidence/ | Students write/defend final ADR and complete load evidence. |
| D02 | Requirements/contribution/AI disclosure | Draft preparation, open | Name-free submission checklist and factual assistance/source-credit record | Students verify contributions, finalize disclosure and sign outside repository. |
| D03 | Final report consistency and viva rehearsal | Pending, 90-120 min split | Group sections/evidence still needed | Check page limit, code/diagram alignment and demo timing. |
| INT01 | Align with accepted master history | Complete | `352e117` merges `be6954d`; no source changes | Retain normal merge ancestry; do not rebase published work. |
| INT02 | Review Payment contract fit | Review documented; acceptance pending | Proposed fields/units/final-result timing and missing Booking saga recorded | Obtain Booking agreement before checkout. |
| INT03 | Integrate real Payment and its dashboard | Implemented | Source 63b8b0e, private DB, dashboard/scoping, five UP targets; 25 tests including broker | Future upstream merge must reconcile imported paths. |

## Verification and current runtime

Checks on 2026-11-14:

- Catalogue: 54 tests passed in Python 3.12 against isolated PostgreSQL schemas,
  with no skips and one dependency deprecation warning. Coverage includes API
  validation, atomic writes, conflicts/cancellation, outbox retry/concurrency,
  duplicate/reordered snapshots, cache races and repeat-safe/atomic seeds.
  Ticketing: 14 SQLite/fake-mail tests passed earlier; its application is unchanged.
- Payment integration: all 25 tests pass, including the enabled live-broker refund
  test; database tests use uniquely created/dropped schemas. Seven gateway boundary
  tests and six added Booking integration checks pass, including a 20-buyer race.
- Merged Compose validates and preserves original application service definitions.
  `promtool check config` passes; the new integration stack has all five expected exporters UP.
- Grafana datasource provisioning, health and proxy queries work. Ticketing request
  metrics appear in Prometheus. Historical samples and Grafana preferences survive
  monitoring-container restart; preferences were restored after testing.
- Fresh isolated stack: all five app containers respond; Catalogue/Ticketing are
  ready; that earlier baseline used three shells. The new stack instead runs real
  Payment and Booking holds, with only Discovery still a shell. No full purchase claim.
- Clean archive of committed source `852ce2d`: build/start passed after recreating
  only the verification project's containers, retaining its named volumes. All five
  app checks, three scrape targets and Grafana health passed. No untracked files
  were needed. This is development-baseline evidence, not a full-system V04 result.
- C01 draft: all six JSON examples pass ID/timestamp/version/seat-map/count checks;
  five message examples round-trip through the current Envelope and match existing
  queue bindings. Candidate messages were subsequently published only in an
  isolated test stack, with queue probes in place of Booking/Discovery consumers.
  Local endpoint implementation does not establish consumer acceptance.
- C03: a fresh Catalogue-owned database was initialized by the migration job before
  the app started. Tests cover references/constraints, repeatability, rollback of
  pending migrations, concurrent runners and reconnect persistence. A live database
  restart preserved venue/event/seat-map records; rerunning migrations preserved
  those records and one ledger entry. Temporary rows/schemas were removed. No other
  application's database was used.

Verification uses Docker project `catalogue-resume-check`, leaving unrelated
containers and prior project volumes untouched. Alternate local ports are 18201-18205
for apps, 19090 for Prometheus, 13000 for Grafana, 25672 for AMQP and 25673 for the
broker management UI. A temporary port override and build logs remain outside Git.
Normal README commands use the standard ports and may need local overrides when
other projects already occupy them. Never stop unrelated containers to free a port.

C03 uses a separate `catalogue-schema-check` project with its own Catalogue database
and volume. Its API preview is on port 8001; its test broker and database publish no
host ports. The earlier monitoring verification project remains separate.

## Ten-increment batch

The requested batch implements C04-C12 and I02. The user authorized one commit:
`feat(catalogue): add event APIs and reliable availability pipeline`. No push or
teammate-branch edits are authorized. Changes share supporting modules; the
following breakdown records checkpoint scope, not ten separate commits.

1. C04: `feat(catalogue): add event list and detail APIs`
2. C05: `feat(catalogue): create events with transactional outbox`
3. C06: `feat(catalogue): add optimistic metadata updates`
4. C07: `feat(catalogue): publish versioned cancellation intent`
5. C08: `feat(catalogue): dispatch durable events with publisher confirms`
6. C09: `feat(catalogue): persist idempotent availability snapshots`
7. C10: `feat(catalogue): wire opt-in availability consumer`
8. C11: `perf(catalogue): cache event detail with bounded freshness`
9. C12: `feat(observability): add Catalogue metrics and dashboard`
10. I02: `feat(integration): seed synthetic Catalogue events safely`

Additional live evidence from isolated project `catalogue-batch-check`:

- Creation envelopes reached both Booking/Discovery queues with matching IDs;
  no real peer service consumed these candidate payloads.
- 100 deliveries of one availability ID produced one applied update and 99
  duplicate outcomes. Older sequence 9 could not overwrite sequence 10; a newer
  release at 11 increased the free count. Rebuilt worker then applied sequence 20.
- Unknown-event and unknown-seat probes exhausted shared retries and reached two
  DLQ entries. These synthetic probes remain only in the test project's queue.
- A temporary database trigger rejected the sent marker after confirmation;
  redelivery reused the exact message ID, then drained after the trigger/function
  were removed. This simulates the marker-failure window, not a process-kill test.
- Stopping only this project's broker left event writes/read APIs working and
  outbox rows pending. Restart drained those rows. Outage was not timed at 60 s.
- Seed reruns preserved an edited event and did not add duplicate messages.
- Four Prometheus targets were UP; Grafana exposed all eight Catalogue panels
  and returned real consumer metrics through its datasource proxy.

This stack uses app ports 28201-28205, broker 35672/35673/35692, Prometheus 29090,
Grafana 23000 and MailHog 28025. Temporary overrides, scripts/results and logs are
outside Git. Its data is synthetic. The normal `catalogue-schema-check` preview
at port 8001 also runs the new migrations/seeds/API, with messaging verified OFF.
No unrelated broker or other service database was stopped or used.

## Second ten-checkpoint batch

Ten original scopes were worked: INT02, INT03, I03, I04, I05, I07, V01, V02, D01,
D02. Eight delivered local code/review/evidence; I07 is partial and D02 remains
preparation. Do not count missing teammate functionality or unsigned submission
materials as finished. On 2026-11-17 the user authorized this batch as one commit:
`feat(integration): add Payment, Booking and gateway development flow`. No push
is authorized. Its parent is `7b9524c`; source imports add no Git merge ancestry.

- Payment/Booking provenance and integration edits are documented in the runbook.
  Their source branches and master remain unchanged.
- Browser check: Safari loaded the event list/detail and unknown availability,
  reserved a real synthetic seat, repeated its saved reference, then displayed a
  competing-buyer refusal and projection refresh. Responsive CSS is implemented;
  mobile-device visual verification was not performed.
- Reproducible fault harness passed both real SIGKILL windows, a measured 60.001 s
  broker outage with 29 successful browse probes, duplicate/reordered snapshots
  (final sequence 101), and database outage/recovery. It generated and removed its
  own private stack/volumes; raw results/logs stay outside Git. See ADR evidence.
- Payment 25, gateway 7, Booking 6 and unchanged Ticketing 14 tests passed. Existing Catalogue coverage is
  54 passing tests from the prior batch; its source is unchanged in this batch.
- Five Prometheus targets are UP. Both service dashboards load from separate
  directories, avoiding duplicate provider scans.

Current preview project: `catalogue-next-check`. UI 38080; APIs 38201-38205;
Prometheus 39090; Grafana 33000; broker 45672/45673/45692; MailHog 38025. It owns
fresh databases/volumes and synthetic fixtures. Catalogue messaging is enabled
only in this isolated test stack; the default remains OFF. Older verification
stacks/unrelated containers were not stopped. First-event seats are held by the
browser probes; the second event remains available for a fresh hold demo.

## Exact next step and remaining effort

Review this batch and discuss imported service edits with their owners. Get
Booking/Discovery acceptance for metadata, full availability snapshots, durable
sequence/reset/backfill, cancellation and Payment timing/amounts. Integrate their
next real implementations, then implement I06 checkout/status using accepted
Booking APIs. Complete I07, V03/V04 and D02/D03 with actual end-to-end/AI evidence.
Never replace missing owner functionality with a misleading success path.

Estimated remaining personal effort: contract hardening 1-3 focused hours;
real-service integration 3-5; checkout/status UI 2-3; load/end-to-end verification
3-6; submission/ADR/report work 3-5. Total 12-22 hours, excluding teammate response
and implementation time. No actual focused hours were supplied. Re-estimate when
Booking's saga and Discovery's implementation are available. Commit only after
explicit instruction; never push automatically or put personal names in files.
