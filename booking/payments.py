"""Checkout's background Payment call (contract sections 6, 8 and 9).

POST /bookings/{id}/checkout commits payment_pending, replies 202 and then
starts run() below as a background task. That task:
1. reads the booking's price, currency and email;
2. calls Payment (up to 3 HTTP attempts, each through the circuit breaker);
3. if Payment answered (HTTP 200 paid / failed / refunded), applies the answer
   in ONE transaction (db.apply_payment_answer: rows 5, 6 or 7).
If no attempt gets an answer, the booking stays payment_pending; a later
payment.result message or the safety net (safety_net.py, which makes ONE more
attempt with PaymentClient.charge_once, through the same breaker) moves it.

Timing (all fixed in code):
- each attempt has a 2 s TOTAL deadline (asyncio.timeout). httpx's own
  Timeout(2.0) is a per-phase limit (connect, read, write, pool), so it is
  only a backstop;
- before attempt 2 a random pause of 0-250 ms, before attempt 3 0-500 ms
  ("full jitter"), so a whole call never takes more than 6.75 s.

Circuit breaker: opens after 5 consecutive failed attempts; after 30 s it lets
exactly one trial attempt through (half-open); success closes it, failure
re-opens it for another 30 s. Every HTTP attempt counts, not every booking.
A 409 or 422 from Payment counts neither way (it means Booking sent something
wrong, not that Payment is down) and is not retried.

One Booking instance is assumed (as for the outbox relay): the breaker and the
set of running tasks live in this process's memory.
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass

import httpx

import db
import metrics

log = logging.getLogger("booking.payments")

ATTEMPTS = 3
ATTEMPT_TIMEOUT_S = 2.0
PAUSE_CAPS_S = (0.25, 0.5)      # upper bound of the random pause before attempt 2 and attempt 3
FAILURE_THRESHOLD = 5
OPEN_SECONDS = 30

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
_STATE_NUMBER = {CLOSED: 0, OPEN: 1, HALF_OPEN: 2}   # for the booking_payment_breaker_state gauge


class CircuitBreaker:
    """Own circuit breaker. Used only from the event loop, so it needs no lock."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state = CLOSED
        self.failures = 0            # consecutive failed attempts while closed
        self.opened_at = None
        self.trial_running = False   # half-open: the one trial attempt is in flight
        self._show()

    def _show(self):
        metrics.BREAKER_STATE.set(_STATE_NUMBER[self.state])

    def _cooled_down(self):
        return self.clock() - self.opened_at >= OPEN_SECONDS

    def accepts_checkout(self):
        """Checkout check 5: refuse while open under 30 s, or while a trial is in flight."""
        if self.state == CLOSED:
            return True
        if self.state == OPEN and not self._cooled_down():
            return False
        return not self.trial_running

    def try_acquire(self):
        """May an HTTP attempt be made now? If the 30 s are over, this attempt becomes the trial."""
        if self.state == CLOSED:
            return True
        if self.state == OPEN and not self._cooled_down():
            return False
        if self.trial_running:
            return False
        self.state = HALF_OPEN
        self.trial_running = True
        self._show()
        return True

    def record_success(self):
        """Payment answered: close (also ends a trial)."""
        self.state = CLOSED
        self.failures = 0
        self.trial_running = False
        self._show()

    def record_failure(self):
        """No answer from one attempt (timeout, connection error, 5xx, ...)."""
        self.trial_running = False
        if self.state == HALF_OPEN:
            self._open()                 # the trial failed: open for another 30 s
        elif self.state == CLOSED:
            self.failures += 1
            if self.failures >= FAILURE_THRESHOLD:
                self._open()
        # Already open (an attempt that started before it opened): nothing changes.

    def record_neutral(self):
        """409/422, or a cancelled attempt: counts neither way; frees the trial if this was it."""
        self.trial_running = False

    def _open(self):
        self.state = OPEN
        self.opened_at = self.clock()
        self.failures = 0
        self._show()


@dataclass(frozen=True)
class PaymentAnswer:
    status: str        # paid, failed or refunded
    payment_id: str


class PaymentClient:
    """Calls POST /payments with retries, through the breaker.

    sleep, rand and attempt_timeout are parameters so tests can run without
    real waiting; production uses the defaults.
    """

    def __init__(self, http, base_url, breaker, sleep=asyncio.sleep, rand=random.uniform,
                 attempt_timeout=ATTEMPT_TIMEOUT_S):
        self.http = http
        self.url = base_url.rstrip("/") + "/payments"
        self.breaker = breaker
        self.sleep = sleep
        self.rand = rand
        self.attempt_timeout = attempt_timeout

    @staticmethod
    def _body(booking_id, amount, currency, customer_email):
        return {"booking_id": booking_id, "amount": amount, "currency": currency,
                "customer_email": customer_email}

    async def charge(self, booking_id, amount, currency, customer_email):
        """The background call: up to 3 attempts. Returns a PaymentAnswer, or None if Payment gave no answer."""
        body = self._body(booking_id, amount, currency, customer_email)
        for attempt in range(ATTEMPTS):
            if attempt:
                await self.sleep(self.rand(0, PAUSE_CAPS_S[attempt - 1]))
            answer, stop = await self._one_attempt(body, f"attempt {attempt + 1}")
            if stop:
                return answer
        return None

    async def charge_once(self, booking_id, amount, currency, customer_email):
        """The safety net's call (contract section 9): exactly ONE attempt, same body, same
        2 s deadline, same breaker. Returns a PaymentAnswer, or None (no answer, including
        a breaker refusal or a 409/422)."""
        answer, _ = await self._one_attempt(self._body(booking_id, amount, currency, customer_email),
                                            "safety-net attempt")
        return answer

    async def _one_attempt(self, body, name):
        """One attempt through the breaker. Returns (PaymentAnswer or None, stop retrying?)."""
        booking_id = body["booking_id"]
        if not self.breaker.try_acquire():
            # No call is made. The booking stays payment_pending.
            metrics.PAYMENT_ATTEMPTS.labels("short_circuited").inc()
            return None, True
        try:
            outcome, answer = await self._attempt(body)
        except asyncio.CancelledError:
            self.breaker.record_neutral()      # shutdown: free the trial, count nothing
            raise
        metrics.PAYMENT_ATTEMPTS.labels(outcome).inc()
        if answer is not None:
            self.breaker.record_success()
            return answer, True
        if outcome == "rejected":
            # Should never happen (the price is fixed at hold time): log it, no retry.
            self.breaker.record_neutral()
            log.error("Payment rejected booking %s (409/422) on the %s", booking_id, name)
            return None, True
        self.breaker.record_failure()
        log.warning("Payment %s for booking %s: %s", name, booking_id, outcome)
        return None, False

    async def _attempt(self, body):
        """One HTTP attempt. Returns (outcome label, PaymentAnswer or None)."""
        try:
            async with asyncio.timeout(self.attempt_timeout):     # a true 2 s total
                response = await self.http.post(self.url, json=body)
        except (TimeoutError, httpx.TimeoutException):
            return "timeout", None
        except httpx.HTTPError:
            return "error", None                  # connection refused, DNS failure, broken reply
        code = response.status_code
        if code in (409, 422):
            return "rejected", None
        if code >= 500:
            return "server_error", None
        if code != 200:
            return "client_error", None
        try:
            data = response.json()
        except ValueError:
            return "bad_response", None
        if not isinstance(data, dict):
            return "bad_response", None
        status, payment_id = data.get("status"), data.get("payment_id")
        if (status not in db.PAYMENT_STATUSES or not isinstance(payment_id, str)
                or not payment_id.strip() or len(payment_id) > 64
                or data.get("booking_id") != body["booking_id"]):
            return "bad_response", None
        return status, PaymentAnswer(status, payment_id)


class CheckoutRunner:
    """Starts and keeps track of the background calls (one per checkout)."""

    def __init__(self, client):
        self.client = client
        self.breaker = client.breaker
        self._tasks = set()     # keeps a reference, so a running task is never garbage-collected

    def start(self, booking_id):
        task = asyncio.create_task(self.run(booking_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, booking_id):
        try:
            amount, currency, email = await asyncio.to_thread(db.payment_request, booking_id)
            answer = await self.client.charge(booking_id, amount, currency, email)
            if answer is None:
                metrics.PAYMENT_ANSWERS.labels("no_answer").inc()
                log.warning("No answer from Payment for booking %s; it stays payment_pending", booking_id)
                return
            result = await asyncio.to_thread(db.apply_payment_answer, booking_id, answer.status, answer.payment_id)
            metrics.PAYMENT_ANSWERS.labels(result).inc()
            log.info("Booking %s: Payment answered %s -> %s", booking_id, answer.status, result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a background task must log, never crash silently
            metrics.PAYMENT_APPLY_ERRORS.inc()
            log.exception("Background payment for booking %s failed; it stays payment_pending", booking_id)

    async def shutdown(self):
        """Cancel calls still running, then close the HTTP client. Their bookings stay payment_pending."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.http.aclose()


def create_runner(base_url):
    """The production runner. main.py's lifespan calls this; tests patch it."""
    http = httpx.AsyncClient(timeout=httpx.Timeout(ATTEMPT_TIMEOUT_S), trust_env=False, follow_redirects=False)
    return CheckoutRunner(PaymentClient(http, base_url, CircuitBreaker()))
