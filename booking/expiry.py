"""Hold expiry sweeper (contract section 4, row 4).

Every SWEEP_SECONDS (5 s) the sweeper releases held bookings whose
held_until has passed. "Passed" is decided by the DATABASE clock
(held_until < now()), the same clock that set held_until, so this machine's
clock never matters.

- One transaction per EVENT (db.release_expired_holds): all its expired held
  bookings are released together, with one availability snapshot. A failure
  in one event's transaction is logged and counted, and the other events are
  still swept.
- Only bookings still 'held' are touched; a booking in payment_pending never
  expires (row 4 applies only to 'held').
- It only writes to the database and the outbox, so it runs whether or not
  RabbitMQ is configured; the relay publishes later.
- The loop never dies on an error: it logs, counts and tries again on the
  next sweep. main.py's lifespan starts it and cancels it at shutdown.
- One Booking instance is assumed (as for the outbox relay). A second one
  would be safe (every update is conditional) but would only compete.
"""
import asyncio
import logging

import db
import metrics

log = logging.getLogger("booking.expiry")

SWEEP_SECONDS = 5


async def sweep_once():
    """One sweep. Returns (bookings released, snapshots written, events that failed).

    Tests call this directly instead of running the loop.
    """
    event_ids = await asyncio.to_thread(db.expired_hold_events)
    released = snapshots = failed = 0
    for event_id in event_ids:
        try:
            count, snapshot = await asyncio.to_thread(db.release_expired_holds, event_id)
        except Exception:  # noqa: BLE001 - one event must not stop the others
            failed += 1
            metrics.EXPIRY_ERRORS.inc()
            log.exception("Could not release expired holds of event %s; the next sweep tries again", event_id)
            continue
        released += count
        snapshots += snapshot
    if released or failed:
        log.info("Expiry sweep: released %d held booking(s), %d snapshot(s), %d event(s) failed",
                 released, snapshots, failed)
    return released, snapshots, failed


async def run():
    """Sweep now, then every SWEEP_SECONDS, forever. Cancel the task to stop it."""
    log.info("Expiry sweeper started (every %s s)", SWEEP_SECONDS)
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - e.g. the database is down; try again later
            metrics.EXPIRY_ERRORS.inc()
            log.exception("Expiry sweep failed; trying again in %s s", SWEEP_SECONDS)
        await asyncio.sleep(SWEEP_SECONDS)
