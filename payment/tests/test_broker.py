"""Optional live test. Set PAYMENT_TEST_URL and PAYMENT_TEST_AMQP to enable."""

import asyncio
import json
import os
import sys
import unittest
import uuid
from pathlib import Path

import httpx


@unittest.skipUnless(os.getenv("PAYMENT_TEST_URL") and os.getenv("PAYMENT_TEST_AMQP"),
                     "Set PAYMENT_TEST_URL and PAYMENT_TEST_AMQP for the live broker test")
class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_refund_messages(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
        import aio_pika
        from messaging import Envelope

        url = os.environ["PAYMENT_TEST_URL"]
        conn = await aio_pika.connect_robust(os.environ["PAYMENT_TEST_AMQP"])
        async with conn:
            channel = await conn.channel(publisher_confirms=True, on_return_raises=True)
            exchange = await channel.get_exchange("events")
            queue = await channel.declare_queue(exclusive=True, auto_delete=True)
            await queue.bind(exchange, routing_key="payment.result")
            await queue.bind(exchange, routing_key="refund.completed")
            booking_id = "broker-test-" + uuid.uuid4().hex[:12]
            async with httpx.AsyncClient(base_url=url, timeout=10) as client:
                response = await client.post("/payments", json={
                    "booking_id": booking_id, "amount": 4500, "currency": "EUR",
                    "customer_email": "test@example.com"})
                self.assertEqual(response.status_code, 200, response.text)
                row = response.json()
                env = Envelope.new("refund.requested", {
                    "booking_id": booking_id, "payment_id": row["payment_id"]}, "booking")
                # Same message ID twice, then a different request for the same refund.
                for msg in (env, env, Envelope.new("refund.requested", env.payload, "booking")):
                    await exchange.publish(msg.to_amqp(), routing_key=msg.type, mandatory=True)

                received = []
                end = asyncio.get_running_loop().time() + 15
                while asyncio.get_running_loop().time() < end:
                    msg = await queue.get(fail=False)
                    if msg:
                        body = json.loads(msg.body)
                        await msg.ack()
                        if body["payload"]["booking_id"] == booking_id:
                            received.append(body)
                    if len(received) >= 2:
                        break
                    await asyncio.sleep(0.1)
                self.assertEqual([msg["type"] for msg in received], ["payment.result", "refund.completed"])
                self.assertEqual(received[1]["payload"]["amount"], "45.00")
                result = await client.get(f"/payments/{row['payment_id']}")
                self.assertEqual(result.json()["status"], "refunded")
                self.assertEqual(result.json()["refund_id"], received[1]["payload"]["refund_id"])
                await asyncio.sleep(1)
                while (msg := await queue.get(fail=False)) is not None:
                    body = json.loads(msg.body)
                    await msg.ack()
                    self.assertNotEqual(body["payload"]["booking_id"], booking_id,
                                        "Duplicate request produced another event")
