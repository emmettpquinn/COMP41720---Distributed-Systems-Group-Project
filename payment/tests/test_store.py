import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from db_helper import make_db

from app import provider, store


class PaymentTests(unittest.TestCase):
    def setUp(self):
        self.db_url = make_db(self)

    def pay(self):
        return store.charge(self.db_url, "bk-1", 4500, "EUR")

    def test_parallel_retries(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.pay(), range(20)))
        self.assertEqual(len({row["payment_id"] for row in rows}), 1)
        self.assertEqual(self.pay()["payment_id"], rows[0]["payment_id"])

    def test_changed_request(self):
        self.pay()
        with self.assertRaises(store.Conflict):
            store.charge(self.db_url, "bk-1", 5000, "EUR")

    def test_duplicate_refund(self):
        row = self.pay()
        first = store.refund(self.db_url, row["payment_id"])
        second = store.refund(self.db_url, row["payment_id"])
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "refunded")
        self.assertEqual(self.pay()["status"], "refunded")

    def test_payment_retry(self):
        with self.assertRaises(ConnectionError):
            store.charge(self.db_url, "bk-1", 4500, "EUR", "down")
        self.assertEqual(self.pay()["status"], "paid")

    def test_refund_retry(self):
        row = self.pay()
        with self.assertRaises(ConnectionError):
            store.refund(self.db_url, row["payment_id"], "down")
        self.assertEqual(store.get_payment(self.db_url, row["payment_id"])["status"], "paid")
        self.assertEqual(store.refund(self.db_url, row["payment_id"])["status"], "refunded")

    def test_saved_decline(self):
        row = store.charge(self.db_url, "bk-1", 4500, "EUR", "decline")
        self.assertEqual(self.pay(), row)
        with self.assertRaises(store.Conflict):
            store.refund(self.db_url, row["payment_id"])

    def test_parallel_payments(self):
        started = Event()
        release = Event()

        def slow(mode, delay, rate):
            started.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release the slow provider")
            return "paid"

        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(provider, "pay", side_effect=slow):
                first = pool.submit(self.pay)
                self.assertTrue(started.wait(5))
                try:
                    with store.connect(self.db_url) as db:
                        rows = db.execute(
                            "SELECT state, xact_start FROM pg_stat_activity "
                            "WHERE application_name = 'stagedoor-payment' AND pid <> pg_backend_pid() "
                            "AND query LIKE 'SELECT * FROM payments WHERE booking_id%%'"
                        ).fetchall()
                        self.assertTrue(rows)
                        self.assertTrue(all(row["xact_start"] is None for row in rows))
                    with patch.object(provider, "pay", return_value="paid"):
                        other = pool.submit(store.charge, self.db_url, "bk-other", 100, "EUR")
                        self.assertEqual(other.result(timeout=5)["status"], "paid")
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5)["status"], "paid")

    def test_single_charge(self):
        with patch.object(provider, "pay", wraps=provider.pay) as call:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: self.pay(), range(20)))
            self.assertEqual(call.call_count, 1)

    def test_saved_payment(self):
        row = self.pay()
        with store.connect(self.db_url) as db:
            saved = db.execute("SELECT * FROM payments WHERE payment_id = %s", (row["payment_id"],)).fetchone()
            self.assertEqual(saved["booking_id"], "bk-1")
            self.assertEqual(saved["amount"], 4500)
