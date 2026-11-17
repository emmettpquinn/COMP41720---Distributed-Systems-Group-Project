# Catalogue reliability evidence for ADR-002

Verified 2026-11-14 using `tools/verify_catalogue_failures.py`, with the batch's
working-tree source based on `7b9524c`. This records observations and limits for
student evaluation; it is not the final ADR or a claim of shared contract approval.

## Reproduction

```sh
python3 tools/verify_catalogue_failures.py --output /tmp/catalogue-faults.json
```

The harness creates a generated `catalogue-fault-*` project with fresh PostgreSQL,
RabbitMQ, topology/migration/seed jobs and Catalogue. Host ports are ephemeral and
rediscovered after container restart. It injects failures only in that generated
project, then removes that project's containers and synthetic volumes in cleanup.
No existing Catalogue, Payment, Booking or Ticketing database is used.
Raw JSON and logs stay outside Git. Record hardware/workload separately before
making performance comparisons; this run is a correctness/recovery check.

## Observations from the passing run

1. **Process killed before publication.** With its broker stopped, an event was
   created through HTTP and its pending message ID read from the owned database.
   Catalogue was killed with SIGKILL and restarted while the broker was still
   down. Event metadata remained readable. Broker recovery delivered the saved
   message ID to both event-change queues and cleared the pending marker.
2. **Process killed after broker confirmation.** A temporary PostgreSQL trigger
   paused the sent-marker update. While that statement was active, the process
   was killed. The already accepted message was present in both queues. Removing
   the trigger and restarting caused redelivery of the same message ID to both
   queues, followed by the committed sent marker. The trigger/function existed
   only in the generated test database and were removed.
3. **Timed broker outage.** The broker was stopped for 60.001 measured seconds.
   A new event write remained pending; all 29 browse probes succeeded during
   the outage. Broker restart drained the pending event to both queues.
4. **Duplicate/reordered availability.** Sequences 100, 101, duplicate 101 and
   older 99 were published as synthetic full snapshots. Final state remained at
   101 with two available seats; three unique message IDs were processed.
   The newer full release could increase the count, while an older update could
   not overwrite it.
5. **Database outage.** With Catalogue PostgreSQL stopped, liveness remained 200,
   readiness returned 503, and metrics stayed available with database health 0.
   Restart recovered the saved event and sequence 101 projection.

## Supporting implementation checks

The Catalogue's 54-test suite covers migration rollback/repeatability, constraints,
atomic event/outbox writes, expected-version conflicts, cancellation, inbox/apply
rollback, duplicate/stale handling, concurrent snapshots/dispatchers and bounded
cache invalidation. Separate live tests previously checked invalid payloads through
shared 1/2/4-second retries to DLQ and 100 deliveries of one availability ID.

## Interpretation limits and questions for the ADR

Both event-change queues were inspected by probes. Real Booking/Discovery
consumers did not process these candidate payloads. The availability producer was
synthetic; Booking does not yet implement the durable sequence or outbox needed
for that path. These results support local at-least-once publication and ordered,
idempotent projection, not end-to-end saga or accepted contract correctness.

The post-confirm kill deliberately shows a duplicate delivery. Consumers still
need durable duplicate/version handling; publisher confirms alone cannot make a
remote side effect exactly once. Container stop/start preserves the broker's
current filesystem; broker replacement/durable-volume migration was not tested.
No 500-buyer throughput/lag measurement or full purchase is claimed. A 60-second
outage recovery check does not establish the <=2-second normal-load p95 target.

For the final ADR, students should explain why browsing uses a local projection,
what stale/unknown availability means, why full snapshots allow older sequences
to be skipped, how cache TTL contributes to freshness, how sequence resets and
backfill are recovered, and which observed trade-offs would reopen the design.
The current candidate contract and ownership decisions still need team review.
