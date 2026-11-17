import json
import unittest

from db_helper import make_db
from app import config, messages, store
from types import SimpleNamespace


class SchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_url = make_db(self)

    def tables(self):
        with store.connect(self.db_url) as db:
            rows = db.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall()
            return {row["tablename"] for row in rows}

    async def test_three_tables(self):
        self.assertEqual(self.tables(), {"payments", "processed_messages", "outbox"})

    async def test_schema_upgrade(self):
        row = store.charge(self.db_url, "bk-upgrade", 4500, "EUR")
        before = store.pending_events(self.db_url)
        with store.connect(self.db_url) as db, db.transaction():
            # Recreate the previous five-table layout, with representative records.
            db.execute("ALTER TABLE payments DROP COLUMN customer_email, DROP COLUMN refund_failed")
            db.execute("CREATE TABLE contacts (payment_id TEXT PRIMARY KEY REFERENCES payments, email TEXT NOT NULL)")
            db.execute("CREATE TABLE refund_failures (payment_id TEXT PRIMARY KEY REFERENCES payments)")
            db.execute("INSERT INTO contacts VALUES (%s, %s)", (row["payment_id"], "buyer@example.com"))
            db.execute("INSERT INTO refund_failures VALUES (%s)", (row["payment_id"],))
            db.execute("INSERT INTO processed_messages VALUES ('old-message', %s)", (row["payment_id"],))
        store.setup_db(self.db_url)
        store.setup_db(self.db_url)  # Startup migrations must be safe to repeat.
        self.assertEqual(self.tables(), {"payments", "processed_messages", "outbox"})
        self.assertEqual(store.get_payment(self.db_url, row["payment_id"]), row)
        self.assertEqual(store.pending_events(self.db_url), before)
        with store.connect(self.db_url) as db:
            saved = db.execute("SELECT customer_email, refund_failed FROM payments").fetchone()
            self.assertEqual(saved["customer_email"], "buyer@example.com")
            self.assertTrue(saved["refund_failed"])
            self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM processed_messages").fetchone()["n"], 1)

        # Previously reported failure must not create another rejection event.
        env = SimpleNamespace(type="refund.requested", version=1, message_id="new-request",
                              payload={"booking_id": "bk-upgrade", "payment_id": row["payment_id"]})
        await messages.process_refund(self.db_url, config.Settings(mode="decline"), env)
        self.assertEqual(store.pending_events(self.db_url), before)
        await messages.process_refund(self.db_url, config.Settings(),
                                    SimpleNamespace(**{**vars(env), "message_id": "recovery"}))
        event = json.loads(store.pending_events(self.db_url)[-1]["body"])
        self.assertEqual(event["type"], "refund.completed")
        self.assertEqual(event["payload"]["customer_email"], "buyer@example.com")
