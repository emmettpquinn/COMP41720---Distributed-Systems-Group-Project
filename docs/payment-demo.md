# Payment demonstration guide

Run from the repository root in PowerShell, using a local synthetic-data stack. Failure controls affect every new operation in that Payment container, so coordinate before using a shared running stack. These are reproduction steps, not a claim that a new measurement has been completed. Save console output and the commit ID with your submission evidence.

## 1. Start normally

```powershell
$env:PAYMENT_MODE = "ok"
$env:PAYMENT_DELAY = "0"
$env:PAYMENT_ERROR_RATE = "0"
$env:BOOKING_SAFETY_NET_SECONDS = "60"
docker compose up -d --build
docker compose ps
git rev-parse HEAD
```

Wait for Payment, Booking and Catalogue `/ready` to succeed. Optional metrics/dashboard startup uses `-f docker-compose.yml -f docker-compose.monitoring.yml`. Browser checkout is now available at http://localhost:8080: reserve a seat, choose **Pay and complete booking**, wait for the booking result and choose **Check ticket** after confirmation. The steps below also use the direct Booking API so you can capture the states and timings. The gateway never calls Payment directly; it forwards checkout to Booking. See the [checkout contract](contracts/checkout-preview.md).

## 2. Create a new event and hold

Run this block again for each purchase demonstration so previous payments do not affect it. `venue-demo-001` is provided by the default Catalogue seed.

```powershell
$eventId = "pay-demo-" + [guid]::NewGuid().ToString("N")
$customer = "buyer-" + [guid]::NewGuid().ToString("N")
$event = @{
  event_id = $eventId
  venue_id = "venue-demo-001"
  title = "Payment demo"
  description = "Synthetic Payment integration event"
  starts_at = [DateTimeOffset]::UtcNow.AddDays(30).ToString("o")
  timezone = "Europe/Dublin"
  seats = @(@{seat_id = "A1"; section = "A"; label = "A1"})
} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://localhost:8001/events -Method Post -ContentType application/json -Body $event

$holdBody = @{
  event_id = $eventId
  seat_id = "A1"
  customer_id = $customer
  customer_email = "buyer@example.test"
  customer_name = "Demo Buyer"
} | ConvertTo-Json
```

Catalogue-to-Booking delivery is asynchronous. Submit the hold below; if it returns `404 seat_not_found`, wait briefly and repeat the same hold request. Do not create another event just because delivery is still pending.

```powershell
$hold = Invoke-RestMethod http://localhost:8002/holds -Method Post -ContentType application/json -Body $holdBody
$bookingId = $hold.booking_id
$checkoutBody = @{customer_id = $customer} | ConvertTo-Json
```

## 3. Normal purchase

```powershell
Invoke-RestMethod "http://localhost:8002/bookings/$bookingId/checkout" -Method Post -ContentType application/json -Body $checkoutBody
for ($i = 0; $i -lt 30; $i++) {
  $status = Invoke-RestMethod "http://localhost:8002/bookings/${bookingId}?customer_id=$customer"
  $status | ConvertTo-Json -Compress
  if ($status.booking_status -notin @("held", "payment_pending")) { break }
  Start-Sleep -Seconds 1
}
```

Expected: checkout returns `payment_pending`, then Booking becomes `confirmed`. Check `GET http://localhost:8005/tickets/{booking_id}` and MailHog at http://localhost:8025 after Notification catches up. Payment's saved amount is 4500 and currency EUR. Repeating the same Payment request must return the same payment ID.

## 4. Late success and automatic refund

After the normal purchase has settled:

```powershell
$env:PAYMENT_DELAY = "10"
$env:BOOKING_SAFETY_NET_SECONDS = "7"
docker compose up -d --no-deps --force-recreate booking payment
```

Wait for both services to be ready, then repeat section 2 with a fresh event and hold. Start checkout and capture states:

```powershell
$timer = [Diagnostics.Stopwatch]::StartNew()
Invoke-RestMethod "http://localhost:8002/bookings/$bookingId/checkout" -Method Post -ContentType application/json -Body $checkoutBody
for ($i = 0; $i -lt 80; $i++) {
  $status = Invoke-RestMethod "http://localhost:8002/bookings/${bookingId}?customer_id=$customer"
  "{0:N1}s {1}" -f $timer.Elapsed.TotalSeconds, $status.booking_status
  if ($status.booking_status -eq "refunded") { break }
  Start-Sleep -Milliseconds 500
}
```

Expected path: `payment_pending -> released -> refund_required -> refunded`, with no valid ticket. Polling can miss a short intermediate state. The charge's late `payment.result` triggers a queued refund; no seat is re-held. The ten-second provider delay applies to the refund too. Booking's owner reported roughly 9 seconds to release, 10.6 seconds to refund request and 21 seconds to completion with these settings; measure your own run rather than treating those numbers as guaranteed.

Check the final ledger and event history with PostgreSQL. Do not interpret the old Notification cancellation email as proof of no charge; its wording is a separate open correction.

## 5. Standalone failed refund and DLQ recovery

Restore normal settings before creating an independent synthetic Payment record. This record intentionally has no Booking row; it tests Payment recovery without directly refunding a confirmed customer's booking.

```powershell
$env:PAYMENT_MODE = "ok"
$env:PAYMENT_DELAY = "0"
$env:PAYMENT_ERROR_RATE = "0"
$env:BOOKING_SAFETY_NET_SECONDS = "60"
docker compose up -d --no-deps --force-recreate booking payment
$chargeBody = @{
  booking_id = "refund-demo-" + [guid]::NewGuid().ToString("N")
  amount = 4500
  currency = "EUR"
  customer_email = "buyer@example.test"
} | ConvertTo-Json
$testPay = Invoke-RestMethod http://localhost:8004/payments -Method Post -ContentType application/json -Body $chargeBody

$env:PAYMENT_MODE = "down"
docker compose up -d --no-deps --force-recreate payment
```

After readiness succeeds, define and publish a refund request through the same broker as Payment:

```powershell
$publish = @'
import asyncio, os
from messaging import Envelope, connect_with_retry

async def send():
    conn = await connect_with_retry(os.environ["AMQP_URL"])
    async with conn:
        channel = await conn.channel(publisher_confirms=True, on_return_raises=True)
        exchange = await channel.get_exchange("events")
        msg = Envelope.new("refund.requested", {
            "booking_id": os.environ["DEMO_BOOKING"],
            "payment_id": os.environ["DEMO_PAYMENT"]
        }, producer="payment-demo")
        for _ in range(int(os.getenv("DEMO_COPIES", "1"))):
            await exchange.publish(msg.to_amqp(), routing_key=msg.type, mandatory=True)
        print("Requested refund:", msg.message_id)

asyncio.run(send())
'@
$publish | docker compose exec -T -e "DEMO_BOOKING=$($testPay.booking_id)" -e "DEMO_PAYMENT=$($testPay.payment_id)" payment python -
Start-Sleep -Seconds 12
docker compose exec payment python -m messaging.replay payment.refund-requests --list
```

Expected: a parked request after the initial attempt plus three retries; the payment remains `paid`. Restore the provider and replay:

```powershell
$env:PAYMENT_MODE = "ok"
docker compose up -d --no-deps --force-recreate payment
# Wait for readiness, then:
docker compose exec payment python -m messaging.replay payment.refund-requests
Invoke-RestMethod "http://localhost:8004/payments/$($testPay.payment_id)"
```

Allow the consumer to finish if the immediate read still says `paid`. Expected final state: `refunded` with a refund ID.

Now publish three copies of a new request for the same already-refunded payment:

```powershell
$publish | docker compose exec -T -e "DEMO_BOOKING=$($testPay.booking_id)" -e "DEMO_PAYMENT=$($testPay.payment_id)" -e DEMO_COPIES=3 payment python -
```

The refund ID must stay unchanged and there must still be one `refund.completed` event for this payment. The copies share a message ID; the already-refunded ledger state also protects requests with a different ID.

## 6. Terminal rejection recovery

For another standalone record, create it while Payment is `ok`, then switch to `PAYMENT_MODE=decline` and send its refund using the block above. Expected: payment remains `paid`, `refund_failed=true`, one `refund.failed` event, no DLQ entry for that terminal rejection.

Switch back to `ok` and execute the publish block again. It constructs a new message ID and the refund can complete. Replaying the already-processed rejected message cannot recover it. For a real Booking saga in `refund_pending`, use Booking's own command instead:

```powershell
$refundBookingId = "copy-a-refund-pending-booking-id"
docker compose exec booking python refund_recovery.py $refundBookingId
```

Use a real refund-pending booking ID there, not the standalone synthetic ID. The command writes a new request to Booking's outbox; status becomes `refunded` only after Payment's completion event.

## 7. Evidence and cleanup

Open the database with `docker compose exec payment-db psql -U payment -d payment`. Substitute the demo payment UUID below:

```sql
SELECT booking_id, payment_id, amount, status, refund_id, refund_failed
FROM payments WHERE payment_id = '<payment UUID>';
SELECT body::jsonb->>'type' AS type, count(*)
FROM outbox WHERE body::jsonb->'payload'->>'payment_id' = '<payment UUID>'
GROUP BY 1;
```

Save the payment/refund IDs, booking states, event counts, DLQ/replay output and relevant dashboard screenshot outside the repository. A preserved refund ID plus one completion event is the duplicate-refund evidence. Do not record the steps as passed until you run them. Reset mode `ok`, delay/error rate `0` and safety net `60`, then recreate Booking/Payment. Do not use `docker compose down -v` as demo cleanup: it deletes database volumes.
