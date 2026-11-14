# Project continuity

Last reviewed: 2026-11-14. Working branch: `feature/catalogue-integration`.

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
- Python/FastAPI is confirmed. Catalogue uses asyncpg for its current readiness
  check; its domain persistence/migration approach has not been chosen yet.
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
| `origin/booking/skeleton` | `7996674` | Unchanged hold implementation; not integrated. `POST /holds`, conditional seat update, one seats table, 50-customer print-only race script. No expiry/saga/outbox/consumer; `contracts.md` contains no payload definitions. |
| `origin/feature/payment-service` | `63b8b0e` | New PostgreSQL Payment service, outbox/refund consumer, tests and monitoring. Reviewed in an isolated snapshot: 24 tests passed, optional live-broker test skipped. Monitoring subset reused; application branch not merged. |
| `origin/feature/catalogue-integration` | `be21a88` | Remote still contains only the first personal checkpoint. `ed6b9f9`, the master merge and later local commits have not been pushed. |

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

A full Payment merge would touch the same root Compose and build-ignore files as
this branch; it needs explicit reconciliation, preserving Catalogue and replacing
the Payment bootstrap entry. No conflicting merge was attempted. Its new broker
data volume is also not silently applied to existing broker data.

Monitoring integration in `852ce2d` uses the upstream pinned images and copies
the Grafana datasource exactly. The optional overlay preserves application
definitions. Prometheus scrapes itself, Ticketing and RabbitMQ; the Payment target
and Payment dashboard remain with the real Payment integration. Catalogue metrics,
the full group dashboard and AI-cost panels are not complete.

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
- Decide snapshot versus delta and sequence reset/scope before implementing
  stale-message rejection. Skipping old deltas can lose changes; full snapshots
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

Progress: 32 tracked checkpoints, comprising 6 fully complete, 1 completed draft
awaiting contract agreement, and 25 pending. This tracks this branch's personal
backlog, not project-wide completion. Checkpoints vary in effort.

| Checkpoint | Original objective | Current status | Evidence | Next action |
|---|---|---|---|---|
| INF01 | Reuse Docker baseline safely | Complete | Inherited `bb17236`; README/build ignore in `be21a88` | Preserve baseline ownership/boundaries. |
| INF02 | Catalogue shell and own database | Complete | `be21a88`; 5 tests and historical live outage/recovery checks | Extend through Catalogue checkpoints. |
| INF03 | Five-container development startup | Complete, development scope only | `ed6b9f9`; labelled shells under `infra/bootstrap/` | Replace shells with reviewed owner implementations. |
| INF04 | Wire monitoring containers/datasource | Complete, wiring scope only | `852ce2d`; config/API/scrape/persistence checks | Full panels remain C12/I07. |
| INF05 | Verify/document development baseline | Complete, development scope only | `852ce2d` source archive starts successfully; initially empty isolated volumes and later container recreation checked | Full business startup remains V04. |
| C01 | Propose Catalogue API/event contracts | Draft complete; consumer agreement pending | `docs/contracts/catalogue.md` and 6 validated synthetic examples | Review fields/semantics with consumers before implementing dependent contracts. |
| C03 | Persist venues/events/seat maps | Pending, 60-90 min | Catalogue has no domain tables/migrations | Implement constraints after C01 agreement. |
| C04 | Event list/detail APIs | Pending, 60-90 min | Only health/readiness exist | Build PostgreSQL-backed reads and missing-record tests. |
| C05 | Event creation plus atomic outbox | Pending, 60-90 min | No write API or Catalogue outbox | Save event/map/message in one transaction. |
| C06 | Versioned event updates | Pending, 60-90 min | No update API | Agree conflict/seat-map rules; test atomic versions. |
| C07 | Event cancellation intent | Pending, 30-60 min | Lifecycle propagation not implemented | Coordinate Booking cancellation behaviour. |
| C08 | Reliable Catalogue outbox dispatcher | Pending, 60-90 min | Shared publisher envelope available; dispatcher absent | Persistent confirmed publish; retain IDs on retry. |
| C09 | Ordered/idempotent availability projection | Pending, 60-90 min | No inbox/projection schema | Agree semantics; test PostgreSQL races/reordering. |
| C10 | Availability consumer wiring | Pending, 60-90 min | Queue exists; Catalogue consumer absent | Use shared retry/DLQ after transaction commit. |
| C11 | Busy-page cache and freshness | Pending, 45-75 min | No browse/cache layer | Bound/invalidate cache; expose honest freshness. |
| C12 | Catalogue metrics and panel | Pending, 60-90 min | No Catalogue /metrics | Export HTTP/lag/outbox metrics; extend shared dashboard. |
| I02 | Repeatable synthetic seeds | Pending, 45-75 min | No Catalogue business seed flow | Seed through owned writes/outbox, never other DB tables. |
| I03 | Gateway routes and timeouts | Pending, 45-75 min | No gateway | Wire agreed service APIs. |
| I04 | Browsing UI and freshness labels | Pending, 60-90 min | No UI | Consume Catalogue contract. |
| I05 | Refused-hold refresh UI | Pending, 60-90 min | Booking 409 lacks refreshed free seats | Agree response with Booking; no Catalogue-to-Booking call. |
| I06 | Checkout/status UI | Pending, 60-90 min | Booking checkout/status absent on fetched branch | Follow saga contract once available. |
| I07 | Complete real-service startup | Pending, 60-90 min slices | Three temporary shells; Payment elsewhere | Integrate real images, owner databases, provider/Ollama and complete monitoring. |
| V01 | Prove event-outbox crash recovery | Pending, 60-90 min | Catalogue outbox absent | Inject before/after publish crashes; label probes. |
| V02 | Prove 60-second outage/reordering recovery | Pending, 60-90 min | Projection/producer path absent | Real queue/DB recovery and newest-sequence evidence. |
| V03 | Measure lag under 500 buyers | Pending, 60-90 min | No complete inventory/projection workload | Measure <=2 s p95; capture workload/hardware. |
| V04 | Verify full-system clean startup/runbook | Pending, 60-90 min | Development startup is not full booking flow | Clean clone, real seeds and end-to-end purchase/failure demo. |
| D01 | ADR-002 evidence | Pending, 45-75 min | No Catalogue failure/lag evidence yet | Student-owned reasoning with measured constraints. |
| D02 | Requirements/contribution/AI disclosure | Pending, 45-75 min | Submission sources not in fetched tree | Record actual assistance/contributions without transcripts. |
| D03 | Final report consistency and viva rehearsal | Pending, 90-120 min split | Group sections/evidence still needed | Check page limit, code/diagram alignment and demo timing. |
| INT01 | Align with accepted master history | Complete | `352e117` merges `be6954d`; no source changes | Retain normal merge ancestry; do not rebase published work. |
| INT02 | Review Payment contract fit | Pending, 30-60 min | `63b8b0e`; synchronous/proposed interfaces | Agree fields/states/timing with Booking before connecting them. |
| INT03 | Integrate real Payment and its dashboard | Pending, 60-90 min slices | Reviewed code/tests exist on remote | Reconcile root config/build ignores and replace shell; stop on conflict. |

## Verification and current runtime

Checks on 2026-11-14:

- Catalogue: 5 tests passed in Python 3.12; Ticketing: 14 SQLite/fake-mail tests passed.
- Payment snapshot: 25 discovered tests, 24 passed, 1 optional live-broker test
  skipped; isolated PostgreSQL test schemas. No Payment application files integrated.
- Merged Compose validates and preserves original application service definitions.
  `promtool check config` passes; all three expected exporters are UP.
- Grafana datasource provisioning, health and proxy queries work. Ticketing request
  metrics appear in Prometheus. Historical samples and Grafana preferences survive
  monitoring-container restart; preferences were restored after testing.
- Fresh isolated stack: all five app containers respond; Catalogue/Ticketing are
  ready and the other three identify bootstrap mode. No full purchase claim.
- Clean archive of committed source `852ce2d`: build/start passed after recreating
  only the verification project's containers, retaining its named volumes. All five
  app checks, three scrape targets and Grafana health passed. No untracked files
  were needed. This is development-baseline evidence, not a full-system V04 result.
- C01 draft: all six JSON examples pass ID/timestamp/version/seat-map/count checks;
  five message examples round-trip through the current Envelope and match existing
  queue bindings. No draft messages were published. Consumer acceptance and actual
  Catalogue endpoints remain pending; completion of the draft is not acceptance
  of the proposed contract.

Verification uses Docker project `catalogue-resume-check`, leaving unrelated
containers and prior project volumes untouched. Alternate local ports are 18201-18205
for apps, 19090 for Prometheus, 13000 for Grafana, 25672 for AMQP and 25673 for the
broker management UI. A temporary port override and build logs remain outside Git.
Normal README commands use the standard ports and may need local overrides when
other projects already occupy them. Never stop unrelated containers to free a port.

## Exact next step and remaining effort

C01 draft and validation are complete. Review the proposed fields and snapshot
semantics with Booking/Discovery. Resolve consumer decisions before C03 domain
persistence and C05/C08-C10 messaging-dependent work. INT02 Payment contract review
can proceed alongside this discussion. Do not build handlers on invented contracts.
Inspect new remote work before proceeding; obtain permission for further commits.

Current estimate, based on the absent Catalogue business layer and fetched interfaces:
Catalogue backend/contracts/metrics 12-18 focused hours; real-service integration,
gateway/UI/seeds 6-10; verification/submission work 8-12; debugging contingency 4-6.
Total approximately 30-46 focused hours, excluding team response time. No actual
focused time was supplied. Re-estimate after C01 and real Payment integration.
