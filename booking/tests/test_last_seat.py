"""Unit tests for last_seat.py's own logic (step 8a): the PASS/FAIL rules and the numbers.

No database, no network, no RabbitMQ: each test builds a Result by hand, as if a
run had happened, and checks what evaluate() and format_report() make of it.
"""
import unittest

import last_seat
from last_seat import Reply, Result

N = 500
EVENT = "evt-test-lastseat-unit"
WIN_ID = "booking-test-winner"
WIN_CUSTOMER = "cust-test-unit-137"
DLQS = {f"q{i}.dlq": 0 for i in range(6)}


def clean_result():
    """A run that should PASS: one 201, 499 409 seat_taken, everything consistent."""
    replies = []
    for i in range(N):
        cid = f"cust-test-unit-{i:03d}"
        on_wire = 0.01 + i / 10000                 # all on the wire by 0.06 s, before the first reply
        if cid == WIN_CUSTOMER:
            replies.append(Reply(cid, 201, None, WIN_ID, sent=0.0, done=0.1, on_wire=on_wire))
        else:
            replies.append(Reply(cid, 409, "seat_taken", None, sent=0.0, done=0.2 + i / 1000, on_wire=on_wire))
    return Result(
        run_id="unit", event_id=EVENT, attempts=N, replies=replies,
        before={"sequence": 1, "outbox_count": 1},
        after={"sequence": 2, "seat": ("held", WIN_ID, WIN_CUSTOMER),
               "bookings": [(WIN_ID, WIN_CUSTOMER, "held")],
               "availability_rows": [{"sequence": 1, "seats_remaining": 1, "published": True},
                                     {"sequence": 2, "seats_remaining": 0, "published": True}]},
        catalogue={"sequence": 2, "seats_remaining": 0},
        dlqs_before=dict(DLQS), dlqs_after=dict(DLQS),
        sampler={"peak_connections": 43, "peak_lock_waiters": 39, "longest_lock_wait": 0.3},
        release_at=0.0,
    )


def failed(result):
    """Names of the checks that fail."""
    return {c.name for c in last_seat.evaluate(result) if not c.ok}


def replace_reply(result, index, reply):
    result.replies[index] = reply
    return result


class EvaluateTests(unittest.TestCase):
    def test_clean_run_passes(self):
        result = clean_result()
        result.checks = last_seat.evaluate(result)
        self.assertEqual(failed(result), set())
        self.assertTrue(last_seat.passed(result))
        self.assertIn("OVERALL: PASS", last_seat.format_report(result))

    def test_any_503_fails(self):
        result = replace_reply(clean_result(), 0, Reply("cust-test-unit-000", 503,
                                                        "Booking database is unavailable", None, 0, 2.0))
        self.assertEqual(failed(result), {"replies"})

    def test_transport_error_fails(self):
        result = replace_reply(clean_result(), 0, Reply("cust-test-unit-000", None, "ReadTimeout", None, 0, 30))
        self.assertEqual(failed(result), {"replies"})

    def test_missing_reply_fails(self):
        result = clean_result()
        result.replies.pop(0)
        self.assertIn("replies", failed(result))

    def test_two_winners_fail(self):
        result = replace_reply(clean_result(), 0, Reply("cust-test-unit-000", 201, None, "booking-test-2", 0, 0.1))
        # Not exactly one 201: no winner, so the booking and seat checks fail too.
        self.assertTrue({"replies", "booking", "seat"} <= failed(result))

    def test_two_bookings_in_db_fail(self):
        result = clean_result()
        result.after["bookings"].append(("booking-test-2", "cust-test-unit-000", "held"))
        self.assertEqual(failed(result), {"booking"})

    def test_booking_not_the_winners_fails(self):
        result = clean_result()
        result.after["bookings"] = [("booking-test-other", WIN_CUSTOMER, "held")]
        self.assertEqual(failed(result), {"booking"})

    def test_other_409_fails(self):
        result = replace_reply(clean_result(), 0, Reply("cust-test-unit-000", 409, "event_cancelled", None, 0, 0.1))
        self.assertEqual(failed(result), {"replies"})

    def test_seat_held_by_another_booking_fails(self):
        result = clean_result()
        result.after["seat"] = ("held", "booking-test-other", "cust-test-unit-000")
        self.assertEqual(failed(result), {"seat"})

    def test_sequence_plus_two_fails(self):
        result = clean_result()
        result.after["sequence"] = 3
        self.assertEqual(failed(result), {"sequence"})

    def test_two_new_outbox_rows_fail(self):
        result = clean_result()
        result.after["availability_rows"].append({"sequence": 3, "seats_remaining": 0, "published": True})
        self.assertEqual(failed(result), {"outbox"})

    def test_unpublished_outbox_fails(self):
        result = clean_result()
        result.after["availability_rows"][1]["published"] = False
        self.assertEqual(failed(result), {"outbox"})

    def test_catalogue_not_zero_fails(self):
        result = clean_result()
        result.catalogue = {"sequence": 1, "seats_remaining": 1}
        self.assertEqual(failed(result), {"catalogue"})

    def test_catalogue_missing_fails(self):
        result = clean_result()
        result.catalogue = None
        self.assertEqual(failed(result), {"catalogue"})

    def test_dlq_nonzero_fails(self):
        result = clean_result()
        result.dlqs_after["q3.dlq"] = 1
        self.assertEqual(failed(result), {"dlq"})

    def test_dlq_unreadable_fails(self):
        result = clean_result()
        result.dlqs_after["q3.dlq"] = None
        self.assertEqual(failed(result), {"dlq"})


class ReportTests(unittest.TestCase):
    def test_slow_lock_wait_on_pass_is_a_warning_not_a_fail(self):
        result = clean_result()
        result.sampler["longest_lock_wait"] = 1.4
        result.checks = last_seat.evaluate(result)
        report = last_seat.format_report(result)
        self.assertIn("OVERALL: PASS", report)
        self.assertIn("WARNING: longest lock wait 1.400 s", report)
        self.assertIn("headroom: longest lock wait 1.400 s of the 2 s limit", report)

    def test_failing_report_names_the_check(self):
        result = replace_reply(clean_result(), 0, Reply("cust-test-unit-000", 503, "x", None, 0, 2.0))
        result.checks = last_seat.evaluate(result)
        self.assertFalse(last_seat.passed(result))
        self.assertIn("OVERALL: FAIL (replies)", last_seat.format_report(result))

    def test_precondition_problem_fails(self):
        result = Result(run_id="unit", event_id=EVENT, attempts=N, problem="DLQs not 0 before the run")
        self.assertFalse(last_seat.passed(result))
        self.assertIn("OVERALL: FAIL", last_seat.format_report(result))


class TimingTests(unittest.TestCase):
    def test_release_to_send_spread_throughput_and_in_flight(self):
        result = clean_result()
        t = last_seat.timing(result.replies, result.release_at)
        self.assertAlmostEqual(t["to_wire_spread"], 499 / 10000)
        self.assertAlmostEqual(t["to_wire_max"], 0.01 + 499 / 10000)
        self.assertAlmostEqual(t["wall"], 0.2 + 499 / 1000)
        self.assertAlmostEqual(t["throughput"], 500 / (0.2 + 499 / 1000))
        self.assertEqual(last_seat.in_flight_at_first_reply(result.replies), 500)

    def test_in_flight_counts_only_requests_sent_before_the_first_reply(self):
        replies = [Reply("a", 201, None, "b1", 0, 1.0, on_wire=0.1),
                   Reply("b", 409, "seat_taken", None, 0, 2.0, on_wire=0.5),
                   Reply("c", 409, "seat_taken", None, 0, 3.0, on_wire=1.5),     # sent after the first reply
                   Reply("d", None, "ConnectError", None, 0, 0.2, on_wire=None)]  # never sent; not a reply
        self.assertEqual(last_seat.in_flight_at_first_reply(replies), 2)

    def test_replies_over_the_gateway_limit_are_counted_but_do_not_fail(self):
        result = clean_result()
        for i in (10, 11, 12):
            result.replies[i].done = 3.5
        self.assertEqual(last_seat.timing(result.replies, 0.0)["over_gateway_limit"], 3)
        result.checks = last_seat.evaluate(result)
        report = last_seat.format_report(result)
        self.assertIn("Replies slower than the gateway's 3 s limit: 3 of 500 (informational)", report)
        self.assertIn("holds/s over the wall time", report)
        self.assertIn("OVERALL: PASS", report)

    def test_report_shows_release_to_send_spread(self):
        result = clean_result()
        result.checks = last_seat.evaluate(result)
        self.assertIn("release-to-send spread 0.050 s", last_seat.format_report(result))


class NumberTests(unittest.TestCase):
    def test_percentile(self):
        values = list(range(1, 101))          # 1..100
        self.assertEqual(last_seat.percentile(values, 50), 50)
        self.assertEqual(last_seat.percentile(values, 95), 95)
        self.assertEqual(last_seat.percentile(values, 100), 100)
        self.assertEqual(last_seat.percentile([7], 95), 7)
        self.assertIsNone(last_seat.percentile([], 50))

    def test_server_latency_from_metrics(self):
        def text(c1, c2, c3, total):
            name = "booking_http_request_duration_seconds"
            labels = 'method="POST",path="/holds"'
            return (f"# TYPE {name} histogram\n"
                    f'{name}_bucket{{{labels},le="0.1"}} {c1}\n'
                    f'{name}_bucket{{{labels},le="0.5"}} {c2}\n'
                    f'{name}_bucket{{{labels},le="+Inf"}} {c3}\n'
                    f"{name}_count{{{labels}}} {c3}\n"
                    f"{name}_sum{{{labels}}} {total}\n"
                    f'{name}_count{{method="GET",path="/health"}} 999\n')
        before = last_seat.histogram(text(5, 5, 5, 1.0))
        after = last_seat.histogram(text(55, 100, 105, 31.0))   # 100 new: 50 <=0.1, 45 <=0.5, 5 more
        latency = last_seat.server_latency(before, after)
        self.assertEqual(latency["count"], 100)
        self.assertAlmostEqual(latency["mean"], 0.3)
        self.assertEqual(latency["p50_at_most"], 0.1)
        self.assertEqual(latency["p95_at_most"], 0.5)
        self.assertIsNone(last_seat.server_latency(after, after))


if __name__ == "__main__":
    unittest.main()
