"""In-network load runner. The host wrapper creates its disposable database/broker."""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import importlib.metadata
import json
import math
import os
import platform
import time
import uuid


def percentile(values, quantile):
    """Nearest-rank empirical percentile; no interpolation or missing-as-zero."""
    if not values:
        return None
    if not 0 < quantile <= 1 or any(not math.isfinite(value) for value in values):
        raise ValueError("Percentile requires finite samples and 0 < quantile <= 1")
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * quantile) - 1]


def distribution(values):
    return {"samples": len(values), "p50": percentile(values, .5),
            "p95": percentile(values, .95), "p99": percentile(values, .99),
            "max": max(values) if values else None}


async def run(args):
    import aio_pika
    import asyncpg
    import httpx

    started_at = datetime.now(timezone.utc).isoformat()
    pool = await asyncpg.create_pool(os.environ['LOAD_DATABASE_URL'], min_size=1, max_size=2, command_timeout=15)
    connection = await aio_pika.connect_robust(os.environ['LOAD_AMQP_URL'])
    async with connection, httpx.AsyncClient(
        base_url='http://catalogue:8000', timeout=15, trust_env=False,
        limits=httpx.Limits(max_connections=args.clients, max_keepalive_connections=args.clients),
    ) as client:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        exchange = await channel.get_exchange('events', ensure=True)
        response = await client.post('/venues', json={'venue_id':'venue-load','name':'Synthetic load venue','city':'Dublin'})
        response.raise_for_status()
        event_ids = [f'event-load-{index:03d}' for index in range(args.events)]
        for event_id in event_ids:
            response = await client.post('/events', json={
                'event_id': event_id, 'venue_id':'venue-load', 'title':'Synthetic load event',
                'starts_at':'2026-12-01T19:00:00Z','timezone':'Europe/Dublin',
                'seats':[{'seat_id':f'S{seat:03d}','section':'A','label':str(seat)} for seat in range(100)],
            })
            response.raise_for_status()

        # Test-only audit captures every projection write, including fast successive updates.
        # This adds one insert per applied update and runs only in the fresh test database.
        async with pool.acquire() as db:
            await db.execute('''CREATE TABLE benchmark_samples (
                event_id TEXT NOT NULL, sequence BIGINT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL, applied_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (event_id, sequence));
                CREATE FUNCTION benchmark_record() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN INSERT INTO benchmark_samples VALUES
                    (NEW.event_id, NEW.sequence, NEW.occurred_at, NEW.applied_at);
                    RETURN NEW; END $$;
                CREATE TRIGGER benchmark_record AFTER INSERT OR UPDATE ON availability
                    FOR EACH ROW EXECUTE FUNCTION benchmark_record();''')

        sequences = {event_id:0 for event_id in event_ids}
        published = []

        async def publish(event_id, measured):
            sequences[event_id] += 1
            sequence = sequences[event_id]
            # Use the projection database clock so host/VM clock offsets cannot fake lag.
            async with pool.acquire() as db:
                occurred_at = await db.fetchval('SELECT clock_timestamp()')
            free = 50 if sequence % 2 == 0 else 90
            envelope = {
                'message_id':str(uuid.uuid4()),'type':'seat.availability-changed',
                'occurred_at':occurred_at.isoformat(),'version':1,'producer':'booking',
                'payload':{'event_id':event_id,'sequence':sequence,'seats_remaining':free,
                           'available_seat_ids':[f'S{seat:03d}' for seat in range(free)]},
            }
            await exchange.publish(aio_pika.Message(
                json.dumps(envelope).encode(), content_type='application/json',
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ), routing_key=envelope['type'], mandatory=True, timeout=10)
            if measured:
                published.append((event_id, sequence))

        for event_id in event_ids:
            await publish(event_id, False)
        deadline = time.monotonic() + 30
        while True:
            async with pool.acquire() as db:
                initial_count = await db.fetchval('SELECT count(*) FROM benchmark_samples')
            if initial_count == len(event_ids):
                break
            if time.monotonic() > deadline:
                raise RuntimeError('Initial projections did not drain')
            await asyncio.sleep(.05)
        for event_id in event_ids:
            (await client.get('/events/' + event_id)).raise_for_status()
        async with pool.acquire() as db:
            await db.execute('TRUNCATE benchmark_samples')

        gate = asyncio.Event()
        ready = 0
        active = 0
        peak = 0
        clients_started = set()
        latencies = []
        by_route = {'list': [], 'detail': []}
        statuses = Counter()
        errors = Counter()
        send_delays = []
        start = None
        end = None

        async def reader(index):
            nonlocal ready, active, peak
            ready += 1
            await gate.wait()
            clients_started.add(index)
            count = 0
            while time.monotonic() < end:
                route = 'list' if count % 5 == 0 else 'detail'
                path = '/events' if route == 'list' else '/events/' + event_ids[(index+count) % len(event_ids)]
                request_start = time.perf_counter()
                active += 1
                peak = max(peak, active)
                try:
                    response = await client.get(path)
                    statuses[str(response.status_code)] += 1
                    if response.status_code != 200:
                        errors['HTTP ' + str(response.status_code)] += 1
                    else:
                        data = response.json()
                        if route == 'list' and not isinstance(data, list):
                            errors['invalid list response'] += 1
                        if route == 'detail' and data.get('event_id') not in event_ids:
                            errors['invalid detail response'] += 1
                except (httpx.RequestError, ValueError) as error:
                    errors[type(error).__name__] += 1
                finally:
                    active -= 1
                    latency = time.perf_counter() - request_start
                    latencies.append(latency)
                    by_route[route].append(latency)
                count += 1
                await asyncio.sleep(args.think_seconds)

        async def producer():
            await gate.wait()
            for index in range(round(args.seconds * args.updates_per_second)):
                planned = start + index / args.updates_per_second
                await asyncio.sleep(max(0, planned - time.monotonic()))
                send_delays.append(max(0, time.monotonic() - planned))
                await publish(event_ids[index % len(event_ids)], True)

        readers = [asyncio.create_task(reader(index)) for index in range(args.clients)]
        producer_task = asyncio.create_task(producer())
        while ready < args.clients:
            await asyncio.sleep(0)
        start = time.monotonic()
        end = start + args.seconds
        gate.set()
        await asyncio.gather(*readers, producer_task)
        duration = time.monotonic() - start

        # Allow draining after the measured browse window, but count its lag honestly.
        drain_deadline = time.monotonic() + 30
        while True:
            async with pool.acquire() as db:
                rows = await db.fetch('SELECT event_id, sequence, EXTRACT(EPOCH FROM applied_at-occurred_at)::float8 AS lag FROM benchmark_samples')
            if len(rows) == len(published) or time.monotonic() > drain_deadline:
                break
            await asyncio.sleep(.1)
        observed = {(row['event_id'], row['sequence']) for row in rows}
        complete = observed == set(published)
        lags = [row['lag'] for row in rows]
        nonnegative = all(lag >= 0 for lag in lags)
        lag_summary = distribution(lags)
        checks = {
            'all_clients_started':len(clients_started)==args.clients,
            'no_http_errors':not errors,
            'every_generated_update_observed':complete,
            'no_negative_lag':nonnegative,
            'standalone_lag_p95_at_most_2_seconds':bool(lags) and nonnegative and lag_summary['p95'] <= 2,
        }
        result = {
            'scope':'standalone Catalogue, 500-client default browse workload plus synthetic full snapshots; not real buyer checkout',
            'started_at':started_at, 'finished_at':datetime.now(timezone.utc).isoformat(),
            'workload':vars(args) | {'seat_map_size':100,'http_mix':'one list followed by four detail reads per client',
                'closed_loop':True,'warmup':'one initial snapshot and detail read per event',
                'lag_instrumentation':'temporary AFTER INSERT/UPDATE audit trigger; one extra insert per update',
                'clock_basis':'source timestamp and apply timestamp both from test PostgreSQL clock',
                'http_latency_includes':'connection acquisition, network, full response and JSON validation'},
            'runtime':{'python':platform.python_version(),'machine':platform.machine(),
                'packages':{name:importlib.metadata.version(name) for name in ('httpx','aio-pika','asyncpg')}},
            'observed':{'clients_started':len(clients_started),'peak_in_flight':peak,'elapsed_seconds':duration,
                'requests':len(latencies),'requests_per_second':len(latencies)/duration,
                'status_counts':dict(statuses),'errors':dict(errors),'updates_published':len(published),
                'updates_observed':len(rows),'http_seconds':distribution(latencies),
                'list_seconds':distribution(by_route['list']),'detail_seconds':distribution(by_route['detail']),
                'projection_lag_seconds':lag_summary,'producer_schedule_delay_seconds':distribution(send_delays)},
            'checks':checks,'passed':all(checks.values()),
            'samples':{'http_latency_seconds':latencies,'projection_lag_seconds':lags},
        }
    await pool.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clients',type=int,default=500)
    parser.add_argument('--seconds',type=float,default=30)
    parser.add_argument('--events',type=int,default=20)
    parser.add_argument('--updates-per-second',type=float,default=10)
    parser.add_argument('--think-seconds',type=float,default=.2)
    args = parser.parse_args()
    if not 1 <= args.clients <= 2000 or not 1 <= args.events <= 100:
        parser.error('Clients must be 1..2000 and events 1..100')
    if not all(math.isfinite(value) for value in (args.seconds,args.updates_per_second,args.think_seconds)):
        parser.error('Timing parameters must be finite')
    if not 5 <= args.seconds <= 300 or not 1 <= args.updates_per_second <= 100 or not .01 <= args.think_seconds <= 10:
        parser.error('Seconds 5..300, update rate 1..100, think time .01..10')
    print(json.dumps(asyncio.run(run(args))))


if __name__ == '__main__':
    main()
