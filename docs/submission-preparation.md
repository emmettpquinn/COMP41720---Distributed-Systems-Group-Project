# Submission preparation and assistance record

This is a preparation checklist, not a completed report, signed contribution
statement or final AI disclosure. Keep personal names out of repository files.
Students must verify contributions and follow the brief's submission process.

## Requirements and implementation evidence

- Five independently deployable business services: Catalogue, Ticketing and
  Payment run in the base stack; Booking's hold API runs in the development
  overlay. Discovery is still a bootstrap shell here. The full saga is pending.
- Meaningful asynchronous processing: Catalogue/Payment transactional outboxes,
  shared RabbitMQ retry/DLQ and Ticketing handlers are implemented. Catalogue's
  candidate consumer stays off by default pending agreement; real Booking
  event/availability integration remains open.
- Separate persistence: Catalogue, Ticketing, Payment and Booking use isolated
  databases/volumes. Services never write one another's database.
- Resilience: refer to the failure harness and Catalogue evidence notes. Retain
  raw generated results outside Git and reference their measured scope accurately.
- Performance: p95 projection lag <=2 seconds under real 500-buyer activity remains
  a target. Standalone 500-client browsing with synthetic snapshots now has
  measured evidence in `docs/evidence/catalogue-load-startup.md`. Outage recovery tests do not establish normal-load latency.
- AI: Discovery's local model, index versioning, fallback and token-cost metrics
  await its implementation. No paid API credentials are required by this branch.
- Reproducibility: root README and integration runbook describe Compose, seeds,
  readiness, gateway, tests and limitations. Full-system clean-clone acceptance
  still requires real Discovery and the Booking saga.

## Submission items to complete

The supplied brief requires an at-most-eight-page core report with diagrams,
five one-page ADRs outside that limit, an AI-disclosure appendix and a signed
one-page contribution statement. Confirm the calendar deadline from the module
source; this repository does not establish it.

Use the report to explain request/event paths, ownership, consistency, failure
behaviour and measured evidence. Check diagrams against final code after service
integration. ADR-002 evidence is collected separately; students supply and defend
the final decision, alternatives, trade-offs and reopening conditions. Rehearse
browsing, a real purchase, cancellation/refund, failure recovery and AI fallback
only when each path exists.

## Assistance and contribution facts to verify

AI assistance in this branch included repository/branch inspection, checkpoint
planning, Catalogue API/persistence/messaging/cache/metrics implementation,
Docker integration, gateway/browser implementation, tests, failure harnesses and
technical documentation drafts. Students should review this scope and disclose
the actual tools/assistance in the required format. Test execution and generated
code are supporting evidence; they do not replace understanding or evaluation.

Ticketing/shared messaging originate from `bb17236`; Payment from `63b8b0e`;
Booking's hold implementation from `7996674`. Preserve those service-role credits
when describing shared integration. Imported service code is not newly authored
Catalogue functionality. The current integration adds startup configuration,
readiness/timeouts, non-resetting demo inventory, browser routes/UI and evidence.

No actual focused hours were supplied. Do not infer hours from commits, chat
length or timestamps. Record actual role contributions and hours separately, then
prepare signatures outside these name-free repository files.

## Independent preparation completed 2026-11-17

The clean committed-source development stack passed start/seed/recreation/database
restart checks. The standalone 500-client benchmark recorded all request and
snapshot counts, hardware, timing basis and limitations. V03/V04/I07 remain open
for real Booking/Discovery and end-to-end acceptance.

