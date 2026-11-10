import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from db_helper import make_db

from app.main import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.db_url = make_db(self)
        self.body = {"booking_id": "bk-api", "amount": 4500, "currency": "EUR"}
        self.env = patch.dict(os.environ, {"PAYMENT_MODE": "ok", "PAYMENT_DELAY": "0", "PAYMENT_ERROR_RATE": "0"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def client(self):
        return TestClient(create_app(self.db_url, amqp_url=""))

    def test_api_restart(self):
        with self.client() as client:
            first = client.post("/payments", json=self.body).json()
            self.assertEqual(first["status"], "paid")
            self.assertEqual(client.post("/payments", json=self.body).json(), first)
            self.assertEqual(client.post("/payments", json={**self.body, "amount": 100}).status_code, 409)
            url = f"/payments/{first['payment_id']}/refunds"
            refund = client.post(url).json()
            self.assertEqual(refund["status"], "refunded")
            self.assertEqual(client.post(url).json(), refund)
        with self.client() as client:
            self.assertEqual(client.get(f"/payments/{first['payment_id']}").json(), refund)
            self.assertEqual(client.get("/ready").status_code, 200)

    def test_metric_labels(self):
        with self.client() as client:
            client.post("/payments", json=self.body)
            client.get("/payments/private-test-id")
            result = client.get("/metrics")
            self.assertEqual(result.status_code, 200)
            self.assertIn('payment_records{status="paid"} 1.0', result.text)
            self.assertIn('path="/payments/{payment_id}"', result.text)
            self.assertNotIn("private-test-id", result.text)
            self.assertIn('payment_outbox_pending 1.0', result.text)
            self.assertIn('payment_http_errors_total{status="404"}', result.text)

    def test_error_recovery(self):
        with patch.dict(os.environ, {"PAYMENT_ERROR_RATE": "1"}):
            with self.client() as client:
                self.assertEqual(client.post("/payments", json=self.body).status_code, 503)
        with self.client() as client:
            self.assertEqual(client.post("/payments", json=self.body).json()["status"], "paid")

    def test_bad_inputs(self):
        with self.client() as client:
            for body in ({**self.body, "amount": 0}, {**self.body, "amount": 1.5},
                         {**self.body, "booking_id": "   "}, {**self.body, "currency": "eur"}):
                self.assertEqual(client.post("/payments", json=body).status_code, 422)

    def test_bad_error_rate(self):
        with patch.dict(os.environ, {"PAYMENT_ERROR_RATE": "1.1"}):
            with self.assertRaises(ValueError):
                create_app(self.db_url, amqp_url="")
