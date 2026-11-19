"""Last-seat concurrency test (step 8a, team success criterion):
"500 simultaneous attempts on the last seat produce exactly one booking".

Run it from the repo root, with Booking, Catalogue and RabbitMQ already up
(rebuild the booking image first so this file is in it):

    docker compose run --rm --no-deps -v "${PWD}/booking/evidence:/srv/evidence" booking python last_seat.py

`run` replaces the image's command, so no second Booking starts (no uvicorn, no
lifespan, no relay or sweeper). Requests go straight to the running Booking at
http://booking:8000, not through the gateway.

What it does:
1. Preflight (read-only): Booking and Catalogue /ready, and every DLQ is 0.
   If anything is wrong it stops here and creates nothing.
2. Creates a NEW Catalogue test event with ONE seat (A1), so every run is fresh.
3. Waits until Booking has the event and its first availability snapshot is
   published, and Catalogue shows that snapshot (1 seat left).
4. Prepares N hold requests (each with its own customer_id containing
   "test"), then releases them all at once; each opens its own new
   connection (no warm-up, no reuse, no retries).
5. Reads the result back: Booking's database (SELECT only, read-only session),
   Catalogue's event, every DLQ (passive queue_declare: read-only).
6. Prints the report, saves it to /srv/evidence/last-seat-<run_id>.txt, and
   exits 0 on PASS, 1 on FAIL (or if the test could not run).

The only data it creates: the test event (in Catalogue, and Booking's copy of
it) and the holds. Losers write nothing.
"""
import argparse
import asyncio
import contextlib
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg2

SEAT_ID = "A1"
VENUE_ID = "venue-demo-001"           # seeded by catalogue-seed (catalogue/seed-data.json)
AVAILABILITY = "seat.availability-changed"
STATEMENT_LIMIT_S = 2.0               # Booking's statement_timeout (db.connect_once)
LOCK_WAIT_WARNING_S = 1.0             # a PASS above this gets a WARNING (half the limit)
THREAD_POOL = 40                      # Booking's sync handlers: anyio's default limiter
GATEWAY_LIMIT_S = 3.0                 # the gateway's UPSTREAM_TIMEOUT_SECONDS (docker-compose.yml)
SAMPLE_EVERY_S = 0.05                 # lock sampler interval
READY_WAIT_S = 30                     # step 3
AFTER_WAIT_S = 15                     # step 5: wait for the new snapshot to reach Catalogue
DLQ_SETTLE_S = 8                      # > the shared retry ladder 1 + 2 + 4 s (messaging/topology.py)
LATENCY_PATH = "/holds"


# --- Pure logic (unit-tested in tests/test_last_seat.py) -------------------------

@dataclass
class Reply:
    """One hold attempt as the client saw it."""
    customer_id: str
    status: int | None          # HTTP status, or None for a transport error / timeout
    error: str | None           # the reply's "error" (or "detail"), or the exception class
    booking_id: str | None
    sent: float                 # perf_counter when this request was released (the send call began)
    done: float                 # perf_counter when the reply (or the error) arrived
    on_wire: float | None = None  # perf_counter when its HTTP headers started going out
                                  # (after its own new connection was open); None if never sent

    @property
    def label(self):
        """How the reply is counted, e.g. '201', '409 seat_taken', '503', 'transport ReadTimeout'."""
        if self.status is None:
            return f"transport {self.error}"
        if self.status == 409:
            return f"409 {self.error}"
        return str(self.status)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Result:
    """Everything the report needs. Filled step by step by run()."""
    run_id: str
    event_id: str
    attempts: int
    replies: list = field(default_factory=list)
    before: dict | None = None          # {"sequence", "outbox_count"} just before firing
    after: dict | None = None           # read_booking_state() after the run
    catalogue: dict | None = None       # Catalogue's availability after the run
    dlqs_before: dict | None = None
    dlqs_after: dict | None = None
    sampler: dict | None = None         # peak connections, lock waiters, longest lock wait
    server: dict | None = None          # Booking's own /holds latency (from /metrics)
    release_at: float = 0.0             # perf_counter when the requests were released
    problem: str | None = None          # set if the test could not run at all
    checks: list = field(default_factory=list)


def percentile(values, p):
    """Nearest-rank percentile (p in 0..100). None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))     # ceiling of n*p/100, at least 1
    return ordered[int(rank) - 1]


def in_flight_at_first_reply(replies):
    """How many requests were already on the wire when the first reply came back."""
    first = min((r.done for r in replies if r.status is not None), default=None)
    if first is None:
        return None
    return sum(1 for r in replies if r.on_wire is not None and r.on_wire <= first)


def timing(replies, release_at):
    """The burst's timing numbers for the report (all in seconds; None if not measurable)."""
    latencies = [r.done - r.sent for r in replies]
    to_wire = [r.on_wire - release_at for r in replies if r.on_wire is not None]
    wall = (max(r.done for r in replies) - release_at) if replies else None
    return {
        "latencies": latencies,
        "wall": wall,
        # Release-to-send: from the release until the request's headers went out.
        "to_wire_spread": (max(to_wire) - min(to_wire)) if to_wire else None,
        "to_wire_p50": percentile(to_wire, 50),
        "to_wire_max": max(to_wire) if to_wire else None,
        "throughput": (len(replies) / wall) if wall else None,
        "over_gateway_limit": sum(1 for x in latencies if x > GATEWAY_LIMIT_S),
    }


def winner_of(replies):
    """The single 201 reply, or None if there is not exactly one."""
    wins = [r for r in replies if r.status == 201]
    return wins[0] if len(wins) == 1 else None


def count_labels(replies):
    counts = {}
    for reply in replies:
        counts[reply.label] = counts.get(reply.label, 0) + 1
    return counts


def evaluate(result):
    """The D3 checks. Returns a list of Check (all must pass)."""
    replies, attempts = result.replies, result.attempts
    counts = count_labels(replies)
    winner = winner_of(replies)
    checks = []

    # 1. Replies: exactly one 201, every other reply 409 seat_taken, nothing else.
    unexpected = {k: v for k, v in counts.items() if k not in ("201", "409 seat_taken")}
    ok = (len(replies) == attempts and counts.get("201", 0) == 1
          and counts.get("409 seat_taken", 0) == attempts - 1 and not unexpected)
    detail = (f"201 x{counts.get('201', 0)}, 409 seat_taken x{counts.get('409 seat_taken', 0)}"
              f" of {attempts}")
    if unexpected:
        detail += "; unexpected: " + ", ".join(f"{k} x{v}" for k, v in sorted(unexpected.items()))
    if len(replies) != attempts:
        detail += f"; only {len(replies)} replies recorded"
    checks.append(Check("replies", ok, detail))

    after, before = result.after or {}, result.before or {}
    win_id = winner.booking_id if winner else None
    win_customer = winner.customer_id if winner else None

    # 2. Exactly one booking for the event, held, the winner's.
    bookings = after.get("bookings", [])
    ok = (winner is not None and len(bookings) == 1
          and bookings[0] == (win_id, win_customer, "held"))
    checks.append(Check("booking", ok,
                        f"{len(bookings)} booking(s) {bookings}; winner booking_id={win_id} customer={win_customer}"))

    # 3. The seat is held by that booking.
    seat = after.get("seat")
    ok = winner is not None and seat is not None and seat[0] == "held" and seat[1] == win_id
    checks.append(Check("seat", ok, f"seat {SEAT_ID} = {seat}"))

    # 4. The sequence went up by exactly 1 (losers write nothing).
    seq0, seq1 = before.get("sequence"), after.get("sequence")
    ok = seq0 is not None and seq1 == seq0 + 1
    checks.append(Check("sequence", ok, f"sequence {seq0} -> {seq1}"))

    # 5. Exactly one new availability outbox row, for the new sequence, 0 seats left, published.
    rows = after.get("availability_rows", [])
    new = [r for r in rows if seq0 is not None and r["sequence"] > seq0]
    count0 = before.get("outbox_count")
    ok = (count0 is not None and len(rows) == count0 + 1 and len(new) == 1
          and new[0]["sequence"] == seq0 + 1 and new[0]["seats_remaining"] == 0 and new[0]["published"])
    checks.append(Check("outbox", ok,
                        f"availability rows {count0} -> {len(rows)}; new: {new}"))

    # 6. Catalogue shows the seat taken.
    cat = result.catalogue
    ok = (cat is not None and seq0 is not None and cat.get("seats_remaining") == 0
          and cat.get("sequence") == seq0 + 1)
    checks.append(Check("catalogue", ok, f"Catalogue availability {cat}"))

    # 7. Every DLQ is still 0 (None = the queue could not be read).
    dlqs = result.dlqs_after or {}
    bad = {name: n for name, n in dlqs.items() if n != 0}
    ok = bool(dlqs) and not bad
    checks.append(Check("dlq", ok, f"all {len(dlqs)} DLQs = 0" if ok else f"DLQs not 0: {bad or 'none read'}"))
    return checks


def histogram(metrics_text, method="POST", path=LATENCY_PATH):
    """Booking's request-time histogram for one route, from /metrics text.

    Returns {"buckets": {upper_bound: cumulative count}, "count": n, "sum": seconds}.
    """
    from prometheus_client.parser import text_string_to_metric_families

    out = {"buckets": {}, "count": 0.0, "sum": 0.0}
    for family in text_string_to_metric_families(metrics_text):
        if family.name != "booking_http_request_duration_seconds":
            continue
        for sample in family.samples:
            if sample.labels.get("method") != method or sample.labels.get("path") != path:
                continue
            if sample.name.endswith("_bucket"):
                out["buckets"][float(sample.labels["le"])] = sample.value
            elif sample.name.endswith("_count"):
                out["count"] = sample.value
            elif sample.name.endswith("_sum"):
                out["sum"] = sample.value
    return out


def server_latency(before, after):
    """Booking's own latency for the requests between two /metrics reads.

    The histogram only has buckets, so p50/p95 are bucket upper bounds ("at most").
    Returns {"count", "mean", "p50_at_most", "p95_at_most"} or None if nothing was counted.
    """
    count = after["count"] - before["count"]
    if count <= 0:
        return None
    deltas = sorted((le, after["buckets"].get(le, 0) - before["buckets"].get(le, 0))
                    for le in after["buckets"])

    def at_most(p):
        for le, cumulative in deltas:
            if cumulative >= count * p:
                return le
        return float("inf")

    return {"count": int(count), "mean": (after["sum"] - before["sum"]) / count,
            "p50_at_most": at_most(0.50), "p95_at_most": at_most(0.95)}


def fmt_s(value):
    return "n/a" if value is None else f"{value:.3f} s"


def format_report(result):
    """The report text (also saved to the evidence file)."""
    lines = [f"Last-seat test  run {result.run_id}  event {result.event_id}  seat {SEAT_ID}"]
    if result.problem:
        lines.append(f"COULD NOT FINISH: {result.problem}")
        if not result.replies:
            lines.append("OVERALL: FAIL (the test did not run to the end)")
            return "\n".join(lines) + "\n"

    replies = result.replies
    t = timing(replies, result.release_at)
    latencies, wall = t["latencies"], t["wall"]
    in_flight = in_flight_at_first_reply(replies)
    counts = count_labels(replies)
    fivexx = sum(v for k, v in counts.items() if k[:1] == "5")
    transport = sum(v for k, v in counts.items() if k.startswith("transport"))
    others = {k: v for k, v in counts.items()
              if k not in ("201", "409 seat_taken") and k[:1] != "5" and not k.startswith("transport")}

    lines.append(f"Attempts {result.attempts} (each on its own new connection) | release-to-send spread "
                 f"{fmt_s(t['to_wire_spread'])} (p50 {fmt_s(t['to_wire_p50'])}, max {fmt_s(t['to_wire_max'])})"
                 f" | in flight at first reply {in_flight}")
    lines.append(f"Replies: 201 x{counts.get('201', 0)}, 409 seat_taken x{counts.get('409 seat_taken', 0)}, "
                 f"other {sum(others.values())}{' ' + str(others) if others else ''}, 5xx {fivexx}, "
                 f"transport errors {transport}")
    lines.append(f"Client latency p50 {fmt_s(percentile(latencies, 50))}  p95 {fmt_s(percentile(latencies, 95))}"
                 f"  max {fmt_s(max(latencies) if latencies else None)} | wall {fmt_s(wall)}")
    throughput = t["throughput"]
    lines.append(f"Throughput: {'n/a' if throughput is None else f'{throughput:.1f}'} holds/s over the wall time"
                 " (informational)")
    lines.append(f"Replies slower than the gateway's {GATEWAY_LIMIT_S:.0f} s limit: {t['over_gateway_limit']}"
                 f" of {len(replies)} (informational)")
    server = result.server
    if server:
        client_mean = sum(latencies) / len(latencies) if latencies else None
        lines.append(f"Booking's own /holds latency (its /metrics histogram): {server['count']} requests, "
                     f"mean {fmt_s(server['mean'])}, p50 <= {server['p50_at_most']} s, "
                     f"p95 <= {server['p95_at_most']} s | client mean {fmt_s(client_mean)}")
    else:
        lines.append("Booking's own /holds latency: not available")
    winner = winner_of(replies)
    lines.append(f"Winner: {winner.customer_id}  booking_id {winner.booking_id}" if winner
                 else "Winner: none (not exactly one 201)")
    sampler = result.sampler or {}
    longest = sampler.get("longest_lock_wait")
    lines.append(f"Booking DB (sampled every {SAMPLE_EVERY_S * 1000:.0f} ms, informational): peak connections "
                 f"{sampler.get('peak_connections')}, peak lock waiters {sampler.get('peak_lock_waiters')}, "
                 f"longest lock wait {fmt_s(longest)}")
    lines.append(f"headroom: longest lock wait {fmt_s(longest)} of the {STATEMENT_LIMIT_S:.0f} s limit")

    for check in result.checks:
        lines.append(f"[{'PASS' if check.ok else 'FAIL'}] {check.name}: {check.detail}")

    all_ok = passed(result)
    warnings = []
    if longest is not None and longest > LOCK_WAIT_WARNING_S:
        warnings.append(f"longest lock wait {longest:.3f} s is above {LOCK_WAIT_WARNING_S} s "
                        f"(the limit is {STATEMENT_LIMIT_S:.0f} s)")
    if in_flight is not None and in_flight < THREAD_POOL:
        warnings.append(f"only {in_flight} requests were in flight at the first reply "
                        f"(fewer than Booking's {THREAD_POOL} threads): the burst may not have been simultaneous")
    if server and server["count"] != result.attempts:
        warnings.append(f"Booking counted {server['count']} /holds requests, not {result.attempts}")
    for warning in warnings:
        lines.append(f"WARNING: {warning}")
    if all_ok:
        lines.append("OVERALL: PASS")
    else:
        failed = ", ".join(c.name for c in result.checks if not c.ok) or "the test did not run to the end"
        lines.append(f"OVERALL: FAIL ({failed})")
    return "\n".join(lines) + "\n"


def passed(result):
    return result.problem is None and bool(result.checks) and all(c.ok for c in result.checks)


# --- Reading Booking's database (read-only session, SELECT only) -----------------

def connect_readonly(database_url):
    """A read-only, autocommit session: Postgres itself refuses any write."""
    conn = psycopg2.connect(database_url, connect_timeout=5,
                            options="-c default_transaction_read_only=on -c statement_timeout=5000")
    conn.autocommit = True        # every SELECT sees the latest commits
    return conn


def read_booking_state(conn, event_id):
    """Booking's view of the test event: sequence, seat, bookings, availability outbox rows."""
    with conn.cursor() as cur:
        cur.execute("SELECT sequence FROM events WHERE event_id = %s", (event_id,))
        row = cur.fetchone()
        sequence = row[0] if row else None
        cur.execute("SELECT status, booking_ref, customer_id FROM seats WHERE event_id = %s AND seat_id = %s",
                    (event_id, SEAT_ID))
        seat = cur.fetchone()
        cur.execute("SELECT booking_id, customer_id, status FROM bookings WHERE event_id = %s ORDER BY booking_id",
                    (event_id,))
        bookings = [tuple(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT (envelope::jsonb -> 'payload' ->> 'sequence')::bigint,
                      (envelope::jsonb -> 'payload' ->> 'seats_remaining')::int,
                      published_at IS NOT NULL
               FROM outbox
               WHERE type = %s AND envelope::jsonb -> 'payload' ->> 'event_id' = %s
               ORDER BY 1""",
            (AVAILABILITY, event_id),
        )
        rows = [{"sequence": s, "seats_remaining": n, "published": p} for s, n, p in cur.fetchall()]
    return {"sequence": sequence, "seat": tuple(seat) if seat else None,
            "bookings": bookings, "availability_rows": rows}


class LockSampler:
    """Polls pg_stat_activity (SELECT only) in a thread while the holds run.

    Records the peak number of Booking DB sessions, the peak number waiting on
    a lock, and the longest time any statement had been waiting on a lock.
    """

    QUERY = """SELECT count(*),
                      count(*) FILTER (WHERE wait_event_type = 'Lock'),
                      coalesce(max(EXTRACT(EPOCH FROM clock_timestamp() - query_start))
                               FILTER (WHERE wait_event_type = 'Lock'), 0)::float8
               FROM pg_stat_activity
               WHERE datname = current_database() AND pid <> pg_backend_pid()"""

    def __init__(self, database_url):
        self.conn = connect_readonly(database_url)
        self.stop = threading.Event()
        self.stats = {"peak_connections": 0, "peak_lock_waiters": 0, "longest_lock_wait": 0.0, "samples": 0}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            try:
                with self.conn.cursor() as cur:
                    cur.execute(self.QUERY)
                    total, waiting, longest = cur.fetchone()
                s = self.stats
                s["peak_connections"] = max(s["peak_connections"], total)
                s["peak_lock_waiters"] = max(s["peak_lock_waiters"], waiting)
                s["longest_lock_wait"] = max(s["longest_lock_wait"], longest)
                s["samples"] += 1
            except psycopg2.Error as error:
                self.stats["error"] = str(error).strip()
                return
            self.stop.wait(SAMPLE_EVERY_S)

    def start(self):
        self.thread.start()

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=5)
        with contextlib.suppress(Exception):
            self.conn.close()
        return self.stats


# --- RabbitMQ: DLQ depths (passive declare: read-only) -----------------------------

async def read_dlqs(amqp_url):
    """{dlq name: message count} for every queue in the shared topology; None if unreadable."""
    import aio_pika
    from messaging.topology import QUEUES

    counts = {}
    connection = await aio_pika.connect(amqp_url, timeout=10)
    async with connection:
        for queue in QUEUES:
            channel = await connection.channel()
            try:
                declared = await channel.declare_queue(queue.dlq, passive=True)
                counts[queue.dlq] = declared.declaration_result.message_count
            except Exception:  # noqa: BLE001 - a missing queue closes the channel (404)
                counts[queue.dlq] = None
            finally:
                with contextlib.suppress(Exception):
                    await channel.close()
    return counts


# --- The run ---------------------------------------------------------------------

async def catalogue_availability(client, catalogue_url, event_id):
    reply = await client.get(f"{catalogue_url}/events/{event_id}")
    if reply.status_code != 200:
        return None
    return reply.json().get("availability")


async def read_metrics(booking_url):
    """Booking's /holds histogram, on a short-lived client of its own (no idle connection is reused)."""
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        return histogram((await client.get(f"{booking_url}/metrics")).text)


async def fire(booking_url, event_id, customer_ids, timeout, result):
    """Prepare N holds, release them together, record every reply.

    No warm-up: every request opens its OWN new connection when it is released,
    so no connection ever sits idle (uvicorn closes idle keep-alive connections
    after 5 s, which lost one request in an earlier run). Connections are never
    reused (max_keepalive_connections=0). No retries.
    """
    n = len(customer_ids)
    try:
        metrics_before = await read_metrics(booking_url)
    except (httpx.HTTPError, ValueError):
        metrics_before = None
    limits = httpx.Limits(max_connections=n, max_keepalive_connections=0)
    async with httpx.AsyncClient(base_url=booking_url, limits=limits, timeout=httpx.Timeout(timeout),
                                 trust_env=False) as client:
        release = asyncio.Event()
        on_wire = {}                  # customer_id -> when its headers started going out

        def tracer(cid):
            # httpcore calls this at each step of a request; this one happens
            # after the connection is open, just before the request bytes are written.
            async def trace(name, info):
                if name == "http11.send_request_headers.started" and cid not in on_wire:
                    on_wire[cid] = time.perf_counter()
            return trace

        # Prepare every request before anything is sent.
        requests = [client.build_request("POST", "/holds", json={
            "event_id": event_id, "seat_id": SEAT_ID, "customer_id": cid},
            extensions={"trace": tracer(cid)}) for cid in customer_ids]

        async def one(cid, request):
            await release.wait()
            sent = time.perf_counter()
            try:
                reply = await client.send(request)
            except httpx.HTTPError as error:
                return Reply(cid, None, type(error).__name__, None, sent, time.perf_counter(), on_wire.get(cid))
            done = time.perf_counter()
            try:
                body = reply.json()
            except ValueError:
                body = {}
            error = body.get("error") or (str(body.get("detail")) if "detail" in body else None)
            return Reply(cid, reply.status_code, error, body.get("booking_id"), sent, done, on_wire.get(cid))

        tasks = [asyncio.create_task(one(cid, req)) for cid, req in zip(customer_ids, requests)]
        await asyncio.sleep(0.2)          # let every task reach release.wait()
        result.release_at = time.perf_counter()
        release.set()
        result.replies = await asyncio.gather(*tasks)

    try:
        if metrics_before is not None:
            result.server = server_latency(metrics_before, await read_metrics(booking_url))
    except (httpx.HTTPError, ValueError):
        result.server = None


async def run(args, result):
    database_url = os.environ["DATABASE_URL"]
    amqp_url = os.getenv("AMQP_URL", "")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        # 1. Preflight: nothing is created if any of this fails.
        for name, url in (("Booking", args.booking_url), ("Catalogue", args.catalogue_url)):
            try:
                ready = await client.get(f"{url}/ready")
            except httpx.HTTPError as error:
                result.problem = f"{name} /ready unreachable ({type(error).__name__}); nothing was created"
                return
            if ready.status_code != 200:
                result.problem = f"{name} /ready returned {ready.status_code}; nothing was created"
                return
        if not amqp_url:
            result.problem = "AMQP_URL is not set, so the DLQs cannot be read; nothing was created"
            return
        try:
            result.dlqs_before = await read_dlqs(amqp_url)
        except Exception as error:  # noqa: BLE001
            result.problem = f"RabbitMQ unreadable ({type(error).__name__}: {error}); nothing was created"
            return
        bad = {k: v for k, v in result.dlqs_before.items() if v != 0}
        if bad:
            result.problem = f"DLQs not 0 before the run: {bad}; nothing was created"
            return
        conn = connect_readonly(database_url)
        try:
            # 2. The new test event, one seat.
            body = {
                "event_id": result.event_id, "venue_id": VENUE_ID,
                "title": f"Last-seat test {result.run_id}",
                "description": "Booking step 8a concurrency test event (one seat).",
                "starts_at": (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat(),
                "timezone": "Europe/Dublin",
                "seats": [{"seat_id": SEAT_ID, "section": "A", "label": SEAT_ID}],
            }
            created = await client.post(f"{args.catalogue_url}/events", json=body)
            if created.status_code != 201:
                result.problem = f"Catalogue POST /events returned {created.status_code}: {created.text[:200]}"
                return

            # 3. Wait for Booking's record and first snapshot, published and applied by Catalogue.
            deadline = time.monotonic() + READY_WAIT_S
            while True:
                state = read_booking_state(conn, result.event_id)
                cat = await catalogue_availability(client, args.catalogue_url, result.event_id)
                rows = state["availability_rows"]
                if (state["sequence"] and state["seat"] and state["seat"][0] == "free" and rows
                        and all(r["published"] for r in rows) and cat
                        and cat.get("sequence") == state["sequence"] and cat.get("seats_remaining") == 1):
                    result.before = {"sequence": state["sequence"], "outbox_count": len(rows)}
                    break
                if time.monotonic() > deadline:
                    result.problem = (f"event not ready after {READY_WAIT_S} s (the test event was created): "
                                      f"Booking {state}, Catalogue {cat}")
                    return
                await asyncio.sleep(0.25)

            # 4. Fire.
            customer_ids = [f"cust-test-{result.run_id}-{i:03d}" for i in range(args.attempts)]
            sampler = LockSampler(database_url)
            sampler.start()
            try:
                await fire(args.booking_url, result.event_id, customer_ids, args.timeout, result)
            finally:
                result.sampler = sampler.finish()

            # 5. Wait for the new snapshot to be published and applied, then let any
            # failing message run through its retries before reading the DLQs.
            expected = result.before["sequence"] + 1
            deadline = time.monotonic() + AFTER_WAIT_S
            while time.monotonic() < deadline:
                state = read_booking_state(conn, result.event_id)
                cat = await catalogue_availability(client, args.catalogue_url, result.event_id)
                if (all(r["published"] for r in state["availability_rows"])
                        and cat and cat.get("sequence", 0) >= expected):
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(DLQ_SETTLE_S)
            result.after = read_booking_state(conn, result.event_id)
            result.catalogue = await catalogue_availability(client, args.catalogue_url, result.event_id)
            result.dlqs_after = await read_dlqs(amqp_url)
        finally:
            conn.close()
    result.checks = evaluate(result)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="last_seat.py", description="Last-seat concurrency test (step 8a).")
    parser.add_argument("--attempts", type=int, default=500, help="simultaneous holds (default 500)")
    parser.add_argument("--booking-url", default="http://booking:8000")
    parser.add_argument("--catalogue-url", default="http://catalogue:8000")
    parser.add_argument("--timeout", type=float, default=30.0, help="client timeout per request, seconds")
    parser.add_argument("--report-dir", default="/srv/evidence")
    args = parser.parse_args(argv)
    if args.attempts < 2:
        parser.error("--attempts must be at least 2")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(2)
    result = Result(run_id=run_id, event_id=f"evt-test-lastseat-{run_id}", attempts=args.attempts)
    try:
        asyncio.run(run(args, result))
    except Exception as error:  # noqa: BLE001 - always write a report
        result.problem = f"{type(error).__name__}: {error}"
    report = format_report(result)
    print(report, end="")
    path = Path(args.report_dir) / f"last-seat-{run_id}.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        print(f"Report saved: booking/evidence/{path.name}")
    except OSError as error:
        print(f"Report NOT saved ({error})", file=sys.stderr)
        return 1
    return 0 if passed(result) else 1


if __name__ == "__main__":
    sys.exit(main())
