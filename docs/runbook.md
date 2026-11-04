# Runbook

One place for starting the system and running every failure demonstration.
Emmett keeps this file. Each owner writes their own entries.

Commands run from the repository root. `X` is shorthand for:

```sh
X="docker compose exec -T ticketing"
```

## Starting the system *(Advaya)*

```sh
docker compose up -d --build
```

`topology-init` declares every exchange and queue, then exits. Services start only after it succeeds.

| What | Where |
|---|---|
| MailHog inbox | http://localhost:8025 |
| RabbitMQ management | http://localhost:15672 (guest / guest) |
| Ticketing API | http://localhost:8005/tickets/{booking_id} |

*Seeding data: to be written by Advaya.*

---

## Dead-letter replay *(Emmett)*

Use this when a dashboard DLQ panel shows a queue above 0.

1. **See what is parked and why.** The `error=` column holds the last failure.
   ```sh
   $X python -m messaging.replay <queue> --list
   ```
   `<queue>` is the consumer queue, without `.dlq`. The queues are:
   - `ticketing.booking-updates`
   - `booking.payment-updates`
   - `booking.event-changes`
   - `catalogue.seat-availability`
   - `discovery.event-changes`
   - `payment.refund-requests`
2. **Fix the cause first**, for example restart the dependency or deploy the fix. Replaying before the fix sends the messages straight back to the DLQ after about 7 s.
3. **Replay.** Use `--limit N` to try a few first.
   ```sh
   $X python -m messaging.replay <queue>
   ```
4. **Confirm** that the DLQ panel returns to 0 and the owning service's state is right.

Replaying is always safe because every consumer is idempotent. A message that was in fact handled already is recorded as a duplicate and changes nothing.

## Failure demo: MailHog outage *(Emmett)*

**Shows:** at-least-once delivery, retry with backoff, the DLQ, replay, and exactly one email per customer (ADR-005).

**Dashboard:** Ticketing panel, which shows DLQ depth, email delivery errors and duplicates ignored.

```sh
$X python -m scripts.check_mailhog --clear       # empty inbox
docker compose stop mailhog                      # 1. inject the failure
$X python -m scripts.publish_bookings --count 5  # 2. confirm 5 bookings (or book through the UI)
#    wait ~10 s: each message retries after 1 s, 2 s, 4 s, then parks
$X python -m messaging.replay ticketing.booking-updates --list   # 3. 5 parked; DLQ panel = 5
curl localhost:8005/tickets/<booking_id>         #    the ticket is already issued and valid
docker compose start mailhog                     # 4. remove the failure
$X python -m messaging.replay ticketing.booking-updates         # 5. replay
$X python -m scripts.check_mailhog               # 6. 5 emails, 0 duplicated (exit code 0)
```

**What to point out:**

- Tickets were issued during the outage. Only the email waited.
- The replay did not create new tickets. The messages were recognised as duplicates, and only the pending emails were sent.

**Last run (2026-11-04):** 5 parked, 5 replayed, 5 emails and 0 duplicated.

## Duplicate delivery check *(Emmett)*

```sh
$X python -m scripts.check_mailhog --clear
$X python -m scripts.publish_bookings --count 1 --copies 100 --booking bk-dup-100
curl localhost:8005/tickets/bk-dup-100           # one valid ticket
$X python -m scripts.check_mailhog               # 1 email
```

`ticketing_duplicates_ignored_total` rises by 99.

**Last run (2026-11-04):** 1 ticket, 1 email, 99 duplicates ignored.

---

## Concurrency test and late-success demo *(Erwan)*

*To be written.*

## Catalogue failure demo *(Advaya)*

*To be written.*

## Ollama failure demo and re-indexing *(Eoin)*

*To be written.*

## Payment simulator controls and replaying a failed refund *(John)*

*To be written.* A failed refund parks in `payment.refund-requests.dlq` only if the consumer itself fails. A refund that Payment has given up on is a `refund.failed` message, not a parked one.
