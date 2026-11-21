# Project continuity

Last reviewed: 2026-11-19. Evidence dates are stated by batch below. Working branch: `feature/catalogue-integration`.

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
- Browser traffic goes through the gateway to Catalogue, Discovery and
  Booking. Booking calls Payment. Catalogue does not synchronously call Booking.
- The team plan uses asynchronous availability to avoid coupling busy browse pages
  to Booking. Target event-to-projection lag is at most 2 seconds at p95, warning
  above 10 seconds. Booking still rejects unavailable seats. Discovery may lag 30 seconds.
- Booking implements 10-minute holds and a 5-second expiry sweeper. Holds do not
  expire during payment. A configurable safety net reconciles/releases uncertain
  payments; late success is refunded without re-holding the seat.
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

## Branch review and integration state (2026-11-19)

The working branch was fast-forwarded from `7fc6f67` to `origin/master` `cb90d30`,
preserving all original Booking commits and existing local changes. No new commit
was created, and no remote PR was merged by this integration work. Local edits
remain unstaged. PR #8 (checkout) and PR #9 (expiry) are already merged upstream.
The only open PR at review was #10, `booking/evidence`, with last-seat evidence,
a load harness and expanded Booking dashboard panels; it is not imported here.

The base stack now starts all five services, each with its own database. Booking
consumes Catalogue metadata and Payment/refund messages and publishes availability.
Catalogue messaging defaults on and Booking's legacy demo seed defaults off.
The gateway enables holds and checkout in Compose; standalone Python configuration
remains opt-in. Seven scrape targets and four service dashboards are provisioned.
Discovery's default HTTP embedding endpoint is explicitly simulated.

Booking checkout returns 202 and calls Payment in the background. Status reads
require the same customer ID and return `booking_status`. The UI reads price and
currency from the saved booking; these are additive fields on the status reply.
Payment HTTP amounts are integer minor units and event amounts are decimal
strings. A declined payment can return HTTP 200 with status `failed`; Booking
checks that status. Browser timeouts are unknown outcomes and never trigger an
automatic payment POST retry.

## Important contracts and unresolved questions

Catalogue's event and full availability contracts are now agreed with Booking.
Event versions and per-event availability sequences are separate from envelope
schema version. Booking keeps its sequence durable and never resets it against
retained Catalogue state. A full synthetic reset uses the whole isolated project;
partial reset/epoch recovery is outside the current agreement.

Booking keeps `booking_ref` equal to `booking_id`. It stores the flat price and
customer details at hold time, with synthetic recipient defaults. Confirmed
bookings on subsequently cancelled events retain valid tickets. Late charges on
released bookings are refunded and never re-hold the seat. Refund rejection can
require manual recovery. Published Booking outbox rows are retained; recovery of
set-aside rows remains open. RabbitMQ container-recreation persistence remains
unproven because there is no broker data volume.

The shared consumer uses one initial attempt plus three retries at 1/2/4 seconds.
The full purchase/last-seat workload through the three-second gateway, learned
model relevance, visual browser QA and final report/ADR/signature work retain
their own acceptance requirements. Do not extrapolate unit tests or a direct
Booking load test to full-system performance.

## Checkpoint tracker

Statuses apply to this branch unless otherwise stated. Estimates are implementation
sessions, usually 30-90 minutes; split a task further if it grows beyond one session.

All 32 original checkpoints are retained. Earlier batches delivered 25 local
scopes; contract acceptance and checkout have since advanced as recorded below.
Implementation, working-tree verification and final group acceptance are distinct.
No new integration commit or percentage claim follows from the updated table.

| Checkpoint | Original objective | Current status | Evidence | Next action |
|---|---|---|---|---|
| INF01 | Reuse Docker baseline safely | Complete | Inherited `bb17236`; README/build ignore in `be21a88` | Preserve baseline ownership/boundaries. |
| INF02 | Catalogue shell and own database | Complete | `be21a88`; 5 tests and historical live outage/recovery checks | Extend through Catalogue checkpoints. |
| INF03 | Five-container development startup | Complete, development scope only | `ed6b9f9`; labelled shells under `infra/bootstrap/` | Replace shells with reviewed owner implementations. |
| INF04 | Wire monitoring containers/datasource | Complete, wiring scope only | `852ce2d`; Catalogue target/panels added in current batch | Full group monitoring remains I07. |
| INF05 | Verify/document development baseline | Complete, development scope only | `852ce2d` source archive starts successfully; initially empty isolated volumes and later container recreation checked | Full business startup remains V04. |
| C01 | Catalogue API/event contracts | Event/availability agreement recorded | Booking accepts sections 7/11; actual consumers and matching fixtures | Preserve agreed payloads and coordinate additive changes. |
| C03 | Persist venues/events/seat maps | Complete | `1c109a1`; immutable migration 001; live persistence/repeat checks | Preserve migration history. |
| C04 | Event list/detail APIs | Implemented | PostgreSQL reads, consistent detail snapshot, 404 and unknown-availability checks | Review candidate response shapes. |
| C05 | Event creation plus atomic outbox | Implemented | Venue/event APIs, seat map and immutable message in one transaction; rollback tests | Review candidate create fields. |
| C06 | Versioned event updates | Implemented | Row lock, expected-version conflict, no-op and concurrent-update tests | Keep ID/venue/seat-map changes excluded. |
| C07 | Event cancellation intent | Implemented | Cancelled snapshot, no reactivation, repeat/no-op tests | Booking lifecycle propagation awaits integration. |
| C08 | Reliable Catalogue outbox dispatcher | Implemented; gated | Confirmed mandatory publishing, SKIP LOCKED, stable-ID retries, live marker-failure/outage probes | Accept contract; process-kill crash proof remains V01. |
| C09 | Ordered/idempotent availability projection | Implemented; candidate | Atomic inbox/projection, full snapshots, duplicate/reordering/concurrency tests | Agree durable sequence/reset and backfill rules. |
| C10 | Availability consumer wiring | Implemented; enabled by default in Compose | Real Booking producer and Catalogue projection are wired | Retain duplicate/ordering and reset safety checks. |
| C11 | Busy-page cache and freshness | Implemented | Bounded TTL/deep-copy cache, commit invalidation and in-flight generation guard | Evaluate measured lag with real workload. |
| C12 | Catalogue metrics and panel | Implemented | Seven scrape targets and four service dashboards | Final group monitoring and alert review. |
| I02 | Repeatable synthetic seeds | Implemented; message-driven Booking inventory | Catalogue-owned seeds/outbox; Booking demo seed off | Preserve edits and published history on reruns. |
| I03 | Gateway routes and timeouts | Implemented | Fixed same-origin proxy, bounded calls/body, no hold auto-retry; seven tests | Extend only when agreed APIs exist. |
| I04 | Browsing UI and freshness labels | Implemented with search and live availability | Discovery ranking/current Catalogue metadata and Booking snapshots | Visual browser/device verification remains separate. |
| I05 | Refused-hold refresh UI | Local flow implemented | Real competing-buyer 409 refetches projection and marks refused choice; safe retry reference | Authoritative refreshed free-seat payload still needs Booking agreement. |
| I06 | Checkout/status UI | Implemented and verified with real peer APIs | Owner-aware routes, 202 polling, 20 DOM checks, real checkout/ticket/email and late refund | Visual browser/device QA remains separate. |
| I07 | Complete real-service startup | Five-service base startup and purchase implemented | Private DBs, seven monitoring targets, HTTP embedding simulator | Retain learned-model and full-workload acceptance work. |
| V01 | Prove event-outbox crash recovery | Verified Catalogue scope (harness included) | Real SIGKILL before publish and after confirmation; stable-ID recovery to both queues | Real downstream idempotency acceptance remains open. |
| V02 | Prove 60-second outage/reordering recovery | Verified Catalogue scope (harness included) | 60.001 s outage, 29 successful browse probes; DB restart and sequence 101 recovery | Repeat with actual Booking producer and end-to-end workload. |
| V03 | Measure lag under 500 buyers | Partial; standalone Catalogue benchmark passed | 500 browse clients, 29,598 HTTP 200s, 300/300 synthetic updates, 22.455 ms p95 write lag | Repeat accepted workload with real Booking producer, gateway and full purchase flow. |
| V04 | Verify full-system clean startup/runbook | Working-tree five-service purchase/persistence/failure run passed | Fresh volumes, seven targets, ticket/email, DB restarts and late refund | Verify clean committed archive after approved commit. |
| D01 | ADR-002 evidence | Evidence prepared | Reproducible fault harness, measured observations and limits in docs/evidence/ | Students write/defend final ADR and complete load evidence. |
| D02 | Requirements/contribution/AI disclosure | Drafts prepared; student/group completion pending | Role-based contribution draft and significant AI assist/prompt/adoption records | Complete exact module citations, personal evaluation, verified ownership and signatures. |
| D03 | Final report consistency and viva rehearsal | Preparation implemented; final review pending | Demo/code-location/question checklist, accurate read/hold diagram and evidence limitations | Check actual group report/ADR files, page limits, final diagrams and rehearse individual answers. |
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

Historical preview project from 2026-11-14: `catalogue-next-check`. UI 38080; APIs 38201-38205;
Prometheus 39090; Grafana 33000; broker 45672/45673/45692; MailHog 38025. It owns
fresh databases/volumes and synthetic fixtures. Catalogue messaging is enabled
only in this isolated test stack; the default remains OFF. Older verification
stacks/unrelated containers were not stopped. First-event seats are held by the
browser probes; the second event remains available for a fresh hold demo.

## Independent verification and documentation (2026-11-17, uncommitted)

No production service source or runtime Compose configuration changed. Docker
Desktop was started for the checks; all newly generated test projects/volumes
were removed afterwards. Older preview port notes are historical, not a claim of
a currently running deployment. No teammate branch or database was modified.

- Added a reusable private-project helper, separate load image/runner and host
  wrapper. Defaults: 500 client tasks, 30 seconds, 20 events/100 seats each, 0.2 s
  think time, 10 synthetic full snapshots/second. Lag uses a test-only audit
  trigger and the shared test-DB clock; it excludes real Booking source work,
  gateway/UI, distributed clocks and cache-visible freshness.
- Passing standalone result: 29,598 HTTP 200s, no errors, 972.9 requests/s;
  HTTP p95 889.8 ms. All 300 updates observed; projection-write lag p95 22.455 ms.
  Docker Desktop reported 10 vCPUs/7.75 GiB/aarch64. See the evidence note for exact
  dependency versions, raw-data scope, closed-loop limitations and timing basis.
- Clean archive of committed `970258f` passed startup on fresh owned volumes:
  four real services plus explicit Discovery shell, jobs, five scrape targets and
  two dashboards. Gateway browse/hold/refusal, seed preservation, app recreation,
  Payment idempotency and all four DB liveness/readiness/recovery checks passed.
- Three new measurement/output-boundary unit checks passed. Existing runtime code
  was unchanged; the previously passing service suites were not rerun without a
  new runtime-code concern.
- Added name-free AI-disclosure and contribution drafts plus a demo/viva checklist.
  Checked the brief's exact required disclosure fields and nondelegable student
  judgment boundaries. Module citation format/model metadata, student evaluation,
  verified final ownership, final report/ADRs and signatures remain outstanding.

Evidence: [standalone load and startup](evidence/catalogue-load-startup.md).
Commands: root README. Raw JSON/logs are outside Git; test stacks were cleaned up.
No commits or pushes are authorized for this independent batch. HEAD is `970258f`.

## Exact next step and remaining effort

Review the new evidence/drafts, then obtain accepted Booking/Discovery contracts
and real implementations. Complete I06 checkout/status against agreed APIs and
finish I07/V03/V04 with the real saga, producer, gateway and AI path. Students
complete module-format disclosure citations, personal architectural reasoning,
contribution verification/signatures and the actual group report review in D02/D03.
No missing service is replaced with a misleading success path.

Estimated remaining personal effort: contract hardening 1-3 focused hours;
real-service integration 3-5; checkout/status UI 2-3; full workload/end-to-end
verification 2-4; submission/ADR/report work 2-4. Total 10-19 hours, excluding
teammate response and implementation time. No actual focused hours were supplied.
Commit only after explicit instruction; never push automatically or put personal
names in authored repository files.

## Approved Discovery integration (2026-11-17, uncommitted)

The latest fetch found a new `feature/discovery-date-filter` branch (`eccabe5`);
Booking, Payment, Ticketing and master were unchanged. The user authorized this
integration after review. No source branch, master, index or commit history was
modified. The original local uncommitted/untracked preparation files were
snapshotted outside Git before work and remain local; no files were staged.

- Discovery code/tests/standalone assets imported from `eccabe5`, with name-free
  wording, process liveness and bounded DB/broker readiness integration edits.
- Base Compose adds actual Discovery, its own pgvector database and a private HTTP
  embedding simulator; development removes the obsolete shell and retains Booking
  holds. Real Ollama is an optional configuration-validated overlay, not executed.
- Gateway read routes and UI search display the effective date window, keyword/
  semantic label and explicit simulated-provider label. Current Catalogue metadata
  supplies cards; Discovery supplies ranking. Checkout remains unavailable.
- Catalogue limits align with Discovery (500 title, 8000 description, 50 tags,
  200 per tag), including rollback of partial patches that leave legacy text
  oversized. Existing data and historical outbox envelopes are not rewritten.
- All 49 Discovery tests pass against a separate disposable pgvector test DB,
  with no skips. Catalogue 60, gateway 10, six DOM tests and three measurement
  helper tests pass. Node/jsdom is development-only and excluded from Docker/Git.
- The isolated live harness passed separate database ownership, seed indexing,
  create/update/cancel, duplicate/stale replay after cancellation, similar-route
  self exclusion, model fallback/recovery, Discovery restart persistence and
  shared monitoring. Six targets are UP; the Discovery dashboard uses the existing
  datasource. Generated harness resources were removed; raw results stay outside Git.
- Visual QA was blocked by Safari's locked private window and refusal to interact
  with its authentication screen. Automated DOM tests cover behaviour but do not
  establish visual/mobile rendering.

The dedicated local preview `discovery-integration-check` uses UI 48080, APIs
48201-48205, Prometheus 49090, Grafana 43000 and MailHog 48025. Its databases and
fixtures are synthetic. Catalogue publication is explicitly enabled only for
these integration checks. No final full-purchase or learned-model/500-buyer
performance claim is made. See [integration review](contracts/discovery-integration.md).

Current HEAD remains `da0cc24`; this batch is uncommitted and unpushed. All 32
original checkpoint IDs remain. The seven open scopes still require remaining
Booking contracts/checkout/saga, real workload/end-to-end verification and final
student/group submission review. Discovery is no longer an absent implementation.
Next: review this integration, coordinate remaining Booking contracts, then I06.

## Current Booking handoff results (2026-11-19)

The real-service working-tree checks passed purchase/ticket/email, live
availability, owner checks, application/database restarts, payment decline,
event cancellation and late-success refund while preserving a replacement hold.
Booking 143, gateway 15, DOM 20 and helper 6 tests passed. Details and limitations
are in [the integration runbook](integration-development.md#booking-integration-verification-2026-11-19).
No new commit, staging, push or PR merge was performed. All generated test
projects were removed; earlier dated sections retain historical scope.
