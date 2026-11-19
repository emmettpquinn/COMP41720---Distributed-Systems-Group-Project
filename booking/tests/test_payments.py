"""Unit tests for payments.py: the circuit breaker and the retries (step 6b-2).

No database and no network: Payment is an httpx.MockTransport, and the clock,
the pause and the random jitter are fakes passed in, so nothing really waits.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from prometheus_client import REGISTRY

import config
import payments

BOOKING = 'booking-test-1'


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def reply(status='paid', booking_id=BOOKING, payment_id='pay-test-1'):
    """Payment's real 200 body shape (payment/app/store.py format_payment)."""
    return httpx.Response(200, json={'payment_id': payment_id, 'booking_id': booking_id, 'amount': 4500,
                                     'currency': 'EUR', 'status': status, 'refund_id': None,
                                     'created_at': '2026-11-18T12:00:00+00:00'})


def attempts(outcome):
    return REGISTRY.get_sample_value('booking_payment_attempts_total', {'outcome': outcome}) or 0


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.breaker = payments.CircuitBreaker(clock=self.clock)

    def fail_attempts(self, times):
        for _ in range(times):
            self.assertTrue(self.breaker.try_acquire())
            self.breaker.record_failure()

    def test_opens_on_the_fifth_consecutive_failure_and_a_success_resets_the_count(self):          # P1
        self.fail_attempts(4)
        self.breaker.record_success()
        self.fail_attempts(4)
        self.assertEqual(self.breaker.state, payments.CLOSED)
        self.fail_attempts(1)
        self.assertEqual(self.breaker.state, payments.OPEN)
        self.assertFalse(self.breaker.try_acquire())
        self.assertFalse(self.breaker.accepts_checkout())
        self.assertEqual(REGISTRY.get_sample_value('booking_payment_breaker_state'), 1)

    def test_after_30_seconds_exactly_one_trial(self):                                              # P2
        self.fail_attempts(5)
        self.clock.now += 29.9
        self.assertFalse(self.breaker.accepts_checkout())
        self.assertFalse(self.breaker.try_acquire())
        self.clock.now += 0.1
        self.assertTrue(self.breaker.accepts_checkout())
        self.assertTrue(self.breaker.try_acquire())                 # the trial
        self.assertEqual(self.breaker.state, payments.HALF_OPEN)
        self.assertFalse(self.breaker.accepts_checkout())           # a trial is in flight
        self.assertFalse(self.breaker.try_acquire())                # no second trial

    def test_trial_success_closes_and_trial_failure_reopens_for_30_seconds(self):                   # P3
        self.fail_attempts(5)
        self.clock.now += 30
        self.assertTrue(self.breaker.try_acquire())
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, payments.CLOSED)
        self.assertEqual(REGISTRY.get_sample_value('booking_payment_breaker_state'), 0)
        self.fail_attempts(5)
        self.clock.now += 30
        self.assertTrue(self.breaker.try_acquire())
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, payments.OPEN)
        self.clock.now += 29
        self.assertFalse(self.breaker.try_acquire())
        self.clock.now += 1
        self.assertTrue(self.breaker.try_acquire())

    def test_neutral_result_frees_the_trial_and_changes_no_count(self):                            # P2b
        self.fail_attempts(4)
        self.breaker.record_neutral()
        self.assertEqual(self.breaker.failures, 4)
        self.fail_attempts(1)
        self.clock.now += 30
        self.assertTrue(self.breaker.try_acquire())
        self.breaker.record_neutral()
        self.assertEqual(self.breaker.state, payments.HALF_OPEN)
        self.assertTrue(self.breaker.accepts_checkout())
        self.assertTrue(self.breaker.try_acquire())                 # a new trial may start


class PaymentClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.breaker = payments.CircuitBreaker(clock=self.clock)
        self.requests = []
        self.sleeps = []
        self.bounds = []

    def charge(self, answer, booking_id=BOOKING, timeout=payments.ATTEMPT_TIMEOUT_S, once=False):
        """Run one PaymentClient.charge (or charge_once, the safety net's call) against a
        fake Payment. answer(request) -> Response."""
        async def handler(request):
            self.requests.append(request)
            result = answer(request)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        async def sleep(seconds):
            self.sleeps.append(seconds)

        def rand(low, high):
            self.bounds.append((low, high))
            return high                     # the longest pause full jitter allows

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = payments.PaymentClient(http, 'http://payment-test:8000/', self.breaker,
                                                sleep=sleep, rand=rand, attempt_timeout=timeout)
                call = client.charge_once if once else client.charge
                return await call(booking_id, 4500, 'EUR', 'buyer-test@example.test')
        return asyncio.run(go())

    def test_answers_are_returned_after_one_attempt(self):                                         # P7
        for status in ('paid', 'failed', 'refunded'):
            with self.subTest(status):
                self.requests.clear()
                self.assertEqual(self.charge(lambda r, s=status: reply(s)),
                                 payments.PaymentAnswer(status, 'pay-test-1'))
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.breaker.failures, 0)

    def test_request_goes_to_payments_with_the_booking_fields(self):                                # P10
        self.charge(lambda r: reply())
        request = self.requests[0]
        self.assertEqual((request.method, str(request.url)), ('POST', 'http://payment-test:8000/payments'))
        import json
        self.assertEqual(json.loads(request.content), {'booking_id': BOOKING, 'amount': 4500, 'currency': 'EUR',
                                                       'customer_email': 'buyer-test@example.test'})

    def test_409_and_422_are_not_retried_and_not_counted_by_the_breaker(self):                       # P4
        for code in (409, 422):
            with self.subTest(code):
                self.requests.clear()
                before = attempts('rejected')
                self.assertIsNone(self.charge(lambda r, c=code: httpx.Response(c, json={'detail': 'x'})))
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.sleeps, [])
                self.assertEqual(self.breaker.failures, 0)
                self.assertEqual(attempts('rejected'), before + 1)

    def test_failures_are_retried_three_times_with_full_jitter(self):                                # P5, P6
        async def hang(request):
            await asyncio.sleep(1)          # longer than the 0.05 s deadline below
            return reply()

        def refused(request):
            raise httpx.ConnectError('refused', request=request)
        cases = {'server_error': (lambda r: httpx.Response(503, json={'detail': 'x'}), 2.0),
                 'error': (refused, 2.0),
                 'timeout': (hang, 0.05)}
        for outcome, (answer, timeout) in cases.items():
            with self.subTest(outcome):
                self.breaker = payments.CircuitBreaker(clock=self.clock)
                self.requests.clear(); self.sleeps.clear(); self.bounds.clear()
                before = attempts(outcome)
                self.assertIsNone(self.charge(answer, timeout=timeout))
                self.assertEqual(len(self.requests), 3)
                self.assertEqual(self.bounds, [(0, 0.25), (0, 0.5)])
                self.assertLessEqual(sum(self.sleeps), 0.75)       # so 3 x 2 s + 0.75 s = 6.75 s at most
                self.assertEqual(self.breaker.failures, 3)
                self.assertEqual(attempts(outcome), before + 3)

    def test_bad_answers_count_as_failures(self):                                                     # P7
        cases = {'missing status': lambda r: httpx.Response(200, json={'payment_id': 'p', 'booking_id': BOOKING}),
                 'unknown status': lambda r: reply('pending'),
                 'other booking': lambda r: reply(booking_id='booking-test-other'),
                 'not json': lambda r: httpx.Response(200, text='<html>'),
                 'not found': lambda r: httpx.Response(404, json={'detail': 'x'})}
        for name, answer in cases.items():
            with self.subTest(name):
                self.breaker = payments.CircuitBreaker(clock=self.clock)
                self.requests.clear()
                self.assertIsNone(self.charge(answer))
                self.assertEqual(len(self.requests), 3)
                self.assertEqual(self.breaker.failures, 3)

    def test_breaker_opening_mid_retry_stops_the_remaining_attempts(self):                         # P8
        self.breaker.failures = 4
        before = attempts('short_circuited')
        self.assertIsNone(self.charge(lambda r: httpx.Response(503)))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.breaker.state, payments.OPEN)
        self.assertEqual(attempts('short_circuited'), before + 1)

    def test_every_http_attempt_counts_not_every_booking(self):                                       # P9
        self.charge(lambda r: httpx.Response(503), booking_id='booking-test-a')
        self.assertEqual(self.breaker.state, payments.CLOSED)
        self.charge(lambda r: httpx.Response(503), booking_id='booking-test-b')
        self.assertEqual(len(self.requests), 5)         # opened on the 5th attempt; the 6th was never made
        self.assertEqual(self.breaker.state, payments.OPEN)

    def test_open_breaker_makes_no_call_and_a_successful_trial_closes_it(self):                     # P2, P3
        self.breaker.failures = 4
        self.breaker.record_failure()
        self.assertIsNone(self.charge(lambda r: reply()))
        self.assertEqual(self.requests, [])
        self.clock.now += 30
        self.assertEqual(self.charge(lambda r: reply()).status, 'paid')
        self.assertEqual(self.breaker.state, payments.CLOSED)

    # --- The safety net's ONE call (step 7b; contract section 9) ---

    def test_charge_once_makes_exactly_one_attempt_with_the_same_body(self):                      # S1
        for status in ('paid', 'failed', 'refunded'):
            with self.subTest(status):
                self.requests.clear()
                self.assertEqual(self.charge(lambda r, s=status: reply(s), once=True),
                                 payments.PaymentAnswer(status, 'pay-test-1'))
                self.assertEqual(len(self.requests), 1)
        import json
        self.assertEqual(json.loads(self.requests[0].content), {'booking_id': BOOKING, 'amount': 4500,
                                                                'currency': 'EUR',
                                                                'customer_email': 'buyer-test@example.test'})
        self.assertEqual((self.sleeps, self.bounds), ([], []))            # no jitter pause

    def test_charge_once_failure_is_one_attempt_counted_once_by_the_breaker(self):                # S2
        async def hang(request):
            await asyncio.sleep(1)
            return reply()
        for outcome, answer, timeout in (('server_error', lambda r: httpx.Response(503), 2.0),
                                         ('timeout', hang, 0.05)):
            with self.subTest(outcome):
                self.breaker = payments.CircuitBreaker(clock=self.clock)
                self.requests.clear()
                before = attempts(outcome)
                self.assertIsNone(self.charge(answer, timeout=timeout, once=True))
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.breaker.failures, 1)
                self.assertEqual(attempts(outcome), before + 1)
        # 3 background failures + 1 safety-net failure stay under the threshold of 5 (section 9 demo).
        self.breaker = payments.CircuitBreaker(clock=self.clock)
        self.charge(lambda r: httpx.Response(503))
        self.charge(lambda r: httpx.Response(503), once=True)
        self.assertEqual((self.breaker.state, self.breaker.failures), (payments.CLOSED, 4))

    def test_charge_once_open_breaker_is_no_answer_and_no_request(self):                         # S3
        self.breaker.failures = 4
        self.breaker.record_failure()
        before = attempts('short_circuited')
        self.assertIsNone(self.charge(lambda r: reply(), once=True))
        self.assertEqual(self.requests, [])
        self.assertEqual(attempts('short_circuited'), before + 1)

    def test_charge_once_409_is_no_answer_and_neutral(self):                                     # S4 (I3)
        self.assertIsNone(self.charge(lambda r: httpx.Response(409, json={'detail': 'x'}), once=True))
        self.assertEqual((len(self.requests), self.breaker.failures), (1, 0))


class ConfigTests(unittest.TestCase):
    def test_startup_refuses_a_safety_net_below_7_seconds(self):                                     # S5 (D1)
        for seconds in (6.99, 0, -1, float('nan')):
            with self.subTest(seconds), patch('config.BOOKING_SAFETY_NET_SECONDS', seconds):
                with self.assertRaises(ValueError):
                    config.validate()
        for seconds in (7, 7.0, 60):
            with self.subTest(seconds), patch('config.BOOKING_SAFETY_NET_SECONDS', seconds):
                config.validate()

    def test_startup_refuses_a_bad_currency_or_price(self):                                          # P11
        for currency in ('eur', 'EURO', 'EU', '', 'E1R'):
            with self.subTest(currency), patch('config.BOOKING_CURRENCY', currency):
                with self.assertRaises(ValueError):
                    config.validate()
        for price in (0, -1):
            with self.subTest(price), patch('config.BOOKING_PRICE_MINOR', price):
                with self.assertRaises(ValueError):
                    config.validate()
        with patch('config.BOOKING_CURRENCY', 'GBP'), patch('config.BOOKING_PRICE_MINOR', 1):
            config.validate()


if __name__ == '__main__':
    unittest.main()
