import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from db_helper import make_db

from app import config, messages, provider, store


class MessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_url = make_db(self)
        self.row = store.charge(self.db_url, "bk-msg", 4500, "EUR", email="buyer@example.com")
        self.settings = config.Settings()

    def env(self, message_id="msg-1", **payload):
        return SimpleNamespace(type="refund.requested", version=1, message_id=message_id,
                               payload={"booking_id": "bk-msg", "payment_id": self.row["payment_id"], **payload})

    async def test_duplicate_messages(self):
        with patch.object(provider, "refund", wraps=provider.refund) as call:
            await messages.process_refund(self.db_url, self.settings, self.env())
            await messages.process_refund(self.db_url, self.settings, self.env())
            await messages.process_refund(self.db_url, self.settings, self.env("msg-2"))
            store.refund(self.db_url, self.row["payment_id"])
            self.assertEqual(call.call_count, 1)
        rows = [json.loads(row["body"]) for row in store.pending_events(self.db_url)]
        self.assertEqual([row["type"] for row in rows], ["payment.result", "refund.completed"])
        self.assertEqual(rows[1]["payload"]["amount"], "45.00")
        self.assertEqual(rows[1]["payload"]["customer_email"], "buyer@example.com")

    async def test_refund_recovery(self):
        with self.assertRaises(ConnectionError):
            await messages.process_refund(self.db_url, config.Settings(error_rate=1), self.env())
        self.assertEqual(store.get_payment(self.db_url, self.row["payment_id"])["status"], "paid")
        await messages.process_refund(self.db_url, self.settings, self.env())
        self.assertEqual(store.get_payment(self.db_url, self.row["payment_id"])["status"], "refunded")

    async def test_bad_message(self):
        with self.assertRaises(store.Conflict):
            await messages.process_refund(self.db_url, self.settings, self.env(booking_id="someone-else"))
        env = self.env()
        env.version = 2
        with self.assertRaises(ValueError):
            await messages.process_refund(self.db_url, self.settings, env)
        self.assertEqual(store.get_payment(self.db_url, self.row["payment_id"])["status"], "paid")

    async def test_refund_rejection(self):
        await messages.process_refund(self.db_url, config.Settings(mode="decline"), self.env())
        await messages.process_refund(self.db_url, self.settings, self.env())
        self.assertEqual(store.get_payment(self.db_url, self.row["payment_id"])["status"], "paid")
        await messages.process_refund(self.db_url, self.settings, self.env("new-request"))
        names = [json.loads(row["body"])["type"] for row in store.pending_events(self.db_url)]
        self.assertEqual(names, ["payment.result", "refund.failed", "refund.completed"])

    async def test_publish_retry(self):
        ids = []

        async def failed(body):
            ids.append(body["message_id"])
            raise ConnectionError("Broker down")

        with self.assertRaises(ConnectionError):
            await messages.send_events(self.db_url, failed)
        self.assertEqual(len(store.pending_events(self.db_url)), 1)

        async def success(body):
            ids.append(body["message_id"])

        await messages.send_events(self.db_url, success)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(store.pending_events(self.db_url), [])

    async def test_publish_crash(self):
        ids = []

        async def publish(body):
            ids.append(body["message_id"])

        with patch.object(store, "mark_sent", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                await messages.send_events(self.db_url, publish)
        await messages.send_events(self.db_url, publish)
        self.assertEqual(ids[0], ids[1])

    async def test_payment_rollback(self):
        with patch.object(store, "save_event", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                store.charge(self.db_url, "bk-other", 100, "EUR")
        counts = store.get_totals(self.db_url)
        self.assertEqual(counts["payments"], {"paid": 1})
        self.assertEqual(counts["outbox"], 1)

    async def test_parallel_refunds(self):
        rows = await asyncio.gather(*(
            asyncio.to_thread(store.refund, self.db_url, self.row["payment_id"])
            for _ in range(10)))
        self.assertEqual(len({row["refund_id"] for row in rows}), 1)
        self.assertEqual(len(store.pending_events(self.db_url)), 2)
