"""Payment safety net (contract section 4 row 8, section 9).

If checkout's background call (payments.py) got no answer, the booking stays
payment_pending until a payment.result message arrives. If that never comes,
the safety net decides:

- At least every CHECK_SECONDS (0.5 s) it asks the database for bookings still
  payment_pending whose checkout_started_at + S has passed (S =
  BOOKING_SAFETY_NET_SECONDS, default 60). "Passed" uses the DATABASE clock,
  the same clock that set checkout_started_at.
- For each one it makes ONE more POST /payments (PaymentClient.charge_once:
  same body, 2 s total deadline, the SAME circuit breaker and HTTP client as
  checkout). The call is made outside any transaction, in its own task, so a
  slow call never delays the check for other bookings.
  - Payment answered (HTTP 200 paid/failed/refunded) -> db.apply_payment_answer,
    exactly as the background call does (rows 5, 6, 7).
  - No answer (timeout, error, breaker refusal, 409/422) -> row 8:
    db.release_payment_pending releases the booking and frees its seat, but
    only if it is still payment_pending.
  If Payment did charge after all, its payment.result later arrives for a
  released booking -> row 9 refunds it (db.py); the seat is never re-held.
- in_flight: the booking_ids whose safety-net call is still running. A check
  skips them, so checking every 0.5 s never starts a second call for the same
  booking. It lives in memory: after a restart it is empty and a booking may
  get one more call, which is harmless (Payment returns the stored result for
  the same booking_id and never charges twice).
- If handling a booking fails with an error (e.g. the database is down), it
  stays payment_pending and is not tried again for ERROR_BACKOFF_S, so a
  lasting error does not send Payment a request every 0.5 s.
- Shutdown cancels the loop and every call still running; their bookings stay
  payment_pending and are picked up after the restart. main.py stops the
  safety net BEFORE it closes the shared HTTP client, so a running call is
  cancelled (counted neither way) rather than failing (which would release).
- One Booking instance is assumed (as for the outbox relay and the breaker).
"""
import asyncio
import logging
import time

import db
import metrics

log = logging.getLogger("booking.safety_net")

CHECK_SECONDS = 0.5
ERROR_BACKOFF_S = 30


class SafetyNet:
    def __init__(self, client, seconds, clock=time.monotonic):
        self.client = client          # payments.PaymentClient (shared with checkout)
        self.seconds = seconds        # S
        self.clock = clock            # only for the error backoff
        self.in_flight = set()        # booking_ids with a safety-net call running
        self._tasks = set()           # keeps a reference, so a running task is never garbage-collected
        self._backoff = {}            # booking_id -> clock() time before which it is not tried again
        self._loop_task = None
        self._check_failing = False

    def start(self):
        """Start the checking loop (main.py's lifespan)."""
        self._loop_task = asyncio.create_task(self.run())

    async def check_once(self, wait=False):
        """One check: start a call for every due booking not in flight and not backing off.

        Returns the booking_ids started. wait=True also waits for their calls to
        finish (tests use this instead of running the loop).
        """
        due = await asyncio.to_thread(db.due_safety_net, self.seconds)
        now = self.clock()
        # Forget the backoff of bookings that are no longer due (they moved on).
        for booking_id in set(self._backoff) - set(due):
            del self._backoff[booking_id]
        started = []
        for booking_id in due:
            if booking_id in self.in_flight:
                metrics.SAFETY_NET_SKIPPED.inc()
                continue
            if now < self._backoff.get(booking_id, now):
                continue
            self._backoff.pop(booking_id, None)
            self.in_flight.add(booking_id)
            task = asyncio.create_task(self._handle(booking_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started.append(booking_id)
        if wait:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        return started

    async def _handle(self, booking_id):
        """The ONE call for one booking, then rows 5-7 (answer) or row 8 (no answer)."""
        try:
            amount, currency, email = await asyncio.to_thread(db.payment_request, booking_id)
            answer = await self.client.charge_once(booking_id, amount, currency, email)
            if answer is None:
                result = await asyncio.to_thread(db.release_payment_pending, booking_id, self.seconds)
                detail = "no answer"
            else:
                result = await asyncio.to_thread(db.apply_payment_answer, booking_id,
                                                 answer.status, answer.payment_id)
                detail = f"answered {answer.status}"
            metrics.SAFETY_NET.labels(result).inc()
            log.log(logging.WARNING if result in (db.PAYMENTS_UNAVAILABLE, db.LATE_SUCCESS) else logging.INFO,
                    "Safety net, booking %s: %s -> %s", booking_id, detail, result)
            return result
        except asyncio.CancelledError:
            raise                      # shutdown: the booking stays payment_pending
        except Exception:  # noqa: BLE001 - a background task must log, never crash silently
            metrics.SAFETY_NET.labels("error").inc()
            self._backoff[booking_id] = self.clock() + ERROR_BACKOFF_S
            log.exception("Safety net for booking %s failed; it stays payment_pending (next try in %s s)",
                          booking_id, ERROR_BACKOFF_S)
            return "error"
        finally:
            self.in_flight.discard(booking_id)

    async def run(self):
        """Check now, then about every CHECK_SECONDS, forever. Cancel the task to stop it."""
        log.info("Safety net started (S = %s s, checked every %s s)", self.seconds, CHECK_SECONDS)
        loop = asyncio.get_running_loop()
        while True:
            started_at = loop.time()
            try:
                await self.check_once()
                if self._check_failing:
                    log.info("Safety net check works again")
                    self._check_failing = False
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - e.g. the database is down; try again shortly
                if not self._check_failing:      # log once, not twice a second
                    log.exception("Safety net check failed; retrying every %s s", CHECK_SECONDS)
                    self._check_failing = True
            await asyncio.sleep(max(0.0, CHECK_SECONDS - (loop.time() - started_at)))

    async def shutdown(self):
        """Cancel the loop and every call still running. Their bookings stay payment_pending."""
        tasks = list(self._tasks)
        if self._loop_task is not None:
            tasks.append(self._loop_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
