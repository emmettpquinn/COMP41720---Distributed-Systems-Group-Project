"""Integration additions use unique schemas, never existing Booking inventory."""

import asyncio
import dataclasses
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import httpx
import psycopg2
from psycopg2 import sql
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import db
import demo_seed
import events
import expiry
import outbox
import payment_updates
import payments
import safety_net
from main import app

# Catalogue's real message examples: <repo>/catalogue/examples/contracts. In the
# container (tests mounted at /srv/tests) mount them at /catalogue/examples/contracts.
EXAMPLES=Path(__file__).resolve().parents[2]/'catalogue'/'examples'/'contracts'

# The seats table as it was before this step (no label column), for the upgrade test.
OLD_SEATS_TABLE="""CREATE TABLE seats (
    event_id TEXT NOT NULL, seat_id TEXT NOT NULL, section TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'free' CHECK (status IN ('free', 'held', 'booked')),
    booking_ref TEXT, customer_id TEXT, held_until TIMESTAMPTZ,
    PRIMARY KEY (event_id, seat_id),
    CHECK (status <> 'free' OR (booking_ref IS NULL AND customer_id IS NULL AND held_until IS NULL)))"""


def example(name):
    """One of Catalogue's example envelopes, e.g. example('created')."""
    return json.loads((EXAMPLES/f'event-{name}.json').read_text())


async def idle_sweeper():
    """Stands in for expiry.run in HTTP tests: never sweeps, stops when cancelled."""
    await asyncio.Future()


async def idle_safety_net(self):
    """Stands in for SafetyNet.run in HTTP tests: never checks, stops when cancelled."""
    await asyncio.Future()


@unittest.skipUnless(os.getenv('BOOKING_TEST_DB'), 'Set BOOKING_TEST_DB for isolated PostgreSQL checks')
class BookingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.url=os.environ['BOOKING_TEST_DB']
        self.schema='booking_test_'+uuid.uuid4().hex
        with psycopg2.connect(self.url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        factory=patch('db.connect_once',self.connect)
        factory.start()
        self.addCleanup(factory.stop)
        # The relay keeps one shared connection (db.py); never let it outlive this test's schema.
        self.addCleanup(db.close_relay_connection)
        db.init_schema()

    def connect(self):
        return psycopg2.connect(self.url,connect_timeout=2,options=f'-c search_path={self.schema} -c statement_timeout=2000')

    def drop_schema(self):
        connection=psycopg2.connect(self.url)
        try:
            with connection,connection.cursor() as cursor:
                cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        finally:
            connection.close()

    def seed_seat(self):
        connection=self.connect()
        try:
            with connection,connection.cursor() as cursor:
                cursor.execute("INSERT INTO seats (event_id,seat_id,section) VALUES ('race-event','A1','A')")
        finally:
            connection.close()

    def query(self,statement,params=()):
        connection=self.connect()
        try:
            with connection,connection.cursor() as cursor:
                cursor.execute(statement,params)
                return cursor.fetchall() if cursor.description else None
        finally:
            connection.close()

    def apply(self,name,message_id=None,**changes):
        envelope=example(name)
        return db.apply_event_changed(message_id or envelope['message_id'],envelope['payload']|changes)

    def event_row(self,event_id='event-demo-001'):
        rows=self.query('SELECT event_version,status,title,starts_at,timezone FROM events WHERE event_id=%s',(event_id,))
        return rows[0] if rows else None

    def seat_rows(self,event_id='event-demo-001'):
        return self.query('SELECT seat_id,section,label,status,booking_ref,customer_id,held_until FROM seats '
                          'WHERE event_id=%s ORDER BY seat_id',(event_id,))

    def processed(self):
        return {row[0] for row in self.query('SELECT message_id FROM processed_messages')}

    def client(self,sweeper=False,safety=False):
        env=patch('config.BOOKING_DEMO_SEED',False)
        env.start();self.addCleanup(env.stop)
        # Never consume the real queue from a test: no RabbitMQ consumer here.
        amqp=patch('config.AMQP_URL','')
        amqp.start();self.addCleanup(amqp.stop)
        # The expiry sweeper would release holds a test made expire on purpose, at a
        # random moment. Tests sweep with expiry.sweep_once(); sweeper=True runs the
        # real loop, every 0.05 s.
        loop=patch('expiry.SWEEP_SECONDS',0.05) if sweeper else patch('expiry.run',idle_sweeper)
        loop.start();self.addCleanup(loop.stop)
        # The same for the safety net (step 7b): tests drive SafetyNet.check_once()
        # directly; safety=True runs the real loop, every 0.05 s.
        net=patch('safety_net.CHECK_SECONDS',0.05) if safety else patch('safety_net.SafetyNet.run',idle_safety_net)
        net.start();self.addCleanup(net.stop)
        client=TestClient(app)
        client.__enter__();self.addCleanup(client.__exit__,None,None,None)
        return client

    def test_one_hold_wins_concurrently_and_retry_reuses_reference(self):
        self.seed_seat()
        with ThreadPoolExecutor(max_workers=20) as executor:
            results=list(executor.map(lambda customer:db.hold_seat('race-event','A1',str(customer)),range(20)))
        self.assertEqual(sum(outcome=='held' for outcome,ref,_ in results),1)
        self.assertEqual(sum(outcome=='taken' for outcome,ref,_ in results),19)
        winner=next(index for index,(outcome,ref,_) in enumerate(results) if outcome=='held')
        self.assertEqual(db.hold_seat('race-event','A1',str(winner)),('already_yours',results[winner][1],'held'))

    def test_http_hold_repeat_and_conflict(self):
        self.seed_seat();client=self.client()
        body={'event_id':'race-event','seat_id':'A1','customer_id':'buyer-1'}
        first=client.post('/holds',json=body)
        self.assertEqual(first.status_code,201)
        self.assertEqual(client.post('/holds',json=body).json()['booking_ref'],first.json()['booking_ref'])
        other=client.post('/holds',json=body|{'customer_id':'buyer-2'})
        self.assertEqual(other.status_code,409)
        self.assertEqual(other.json()['error'],'seat_taken')

    def test_missing_seat_and_validation(self):
        client=self.client()
        self.assertEqual(client.post('/holds',json={'event_id':'missing','seat_id':'A1','customer_id':'buyer'}).status_code,404)
        self.assertEqual(client.post('/holds',json={'event_id':' ','seat_id':'A1','customer_id':'buyer'}).status_code,422)

    def test_seed_rerun_preserves_existing_hold(self):
        demo_seed.seed()
        first=db.hold_seat('event-demo-001','A1','buyer')
        demo_seed.seed()
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer'),('already_yours',first[1],'held'))
        self.assertEqual(db.hold_seat('event-demo-001','A1','other'),('taken',None,None))

    def test_database_failure_preserves_health_and_returns_safe_503(self):
        client=self.client()
        with patch('db.connect_once',side_effect=psycopg2.OperationalError('private connection details')):
            self.assertEqual(client.get('/health').status_code,200)
            result=client.get('/ready')
            self.assertEqual(result.status_code,503)
            self.assertNotIn('private',result.text)
        with patch('db.hold_seat',side_effect=psycopg2.OperationalError('private details')):
            result=client.post('/holds',json={'event_id':'e','seat_id':'A1','customer_id':'buyer'})
            self.assertEqual(result.status_code,503)
            self.assertNotIn('private',result.text)

    def test_readiness_reports_holds_and_checkout_without_calling_payment(self):               # C13
        client=self.checkout_client()
        result=client.get('/ready')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['capabilities'],['holds','checkout'])
        self.assertEqual(self.payment_calls,[])
        self.assertEqual(self.client().post('/checkout',json={}).status_code,404)

    # --- event.changed (contract sections 3 and 7) ---

    def test_created_creates_event_and_labelled_seats(self):
        self.assertEqual(self.apply('created'),'applied')
        self.assertEqual(self.event_row(),(1,'scheduled','Evening Performance','2026-11-27T19:30:00+00:00','Europe/Dublin'))
        self.assertEqual(self.seat_rows(),[('A1','A','A1','free',None,None,None),('A2','A','A2','free',None,None,None)])
        self.assertEqual(self.processed(),{example('created')['message_id']})
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer')[0],'held')

    def test_same_message_id_twice_changes_nothing(self):
        first=example('created')['message_id']
        self.assertEqual(self.apply('created'),'applied')
        # Same message_id, even with a newer payload: the second delivery is a no-op.
        self.assertEqual(self.apply('updated',message_id=first),'duplicate')
        self.assertEqual(self.event_row()[:3],(1,'scheduled','Evening Performance'))
        self.assertEqual(self.processed(),{first})

    def test_same_version_other_message_id_is_recorded_only(self):
        self.apply('created')
        other=str(uuid.uuid4())
        self.assertEqual(self.apply('created',message_id=other,title='Should not apply'),'stale')
        self.assertEqual(self.event_row()[:3],(1,'scheduled','Evening Performance'))
        self.assertEqual(self.processed(),{example('created')['message_id'],other})

    def test_older_version_after_newer_is_ignored(self):
        self.assertEqual(self.apply('updated'),'applied')
        self.assertEqual(self.apply('created'),'stale')
        self.assertEqual(self.event_row()[:3],(2,'scheduled','Evening Performance - revised'))
        self.assertEqual(len(self.processed()),2)

    def test_cancelled_before_created_stays_cancelled_without_seats(self):
        self.assertEqual(self.apply('cancelled'),'applied')
        self.assertEqual(self.apply('created'),'stale')
        self.assertEqual(self.event_row()[:2],(3,'cancelled'))
        self.assertEqual(self.seat_rows(),[])

    def test_snapshot_on_held_seat_keeps_hold_and_fills_label(self):
        # Seats as the demo seed leaves them: no label, no event record.
        self.query("INSERT INTO seats (event_id,seat_id,section) VALUES ('event-demo-001','A1','A')")
        self.query("INSERT INTO seats (event_id,seat_id,section,label) VALUES ('event-demo-001','A2','A','Kept label')")
        outcome,ref,_=db.hold_seat('event-demo-001','A1','buyer-1')
        self.assertEqual(outcome,'held')
        before=self.seat_rows()[0]
        self.assertEqual(self.apply('created'),'applied')
        a1,a2=self.seat_rows()
        self.assertEqual(a1,('A1','A','A1','held',ref,'buyer-1',before[6]))
        self.assertEqual(a2[2],'Kept label')
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-1'),('already_yours',ref,'held'))

    def test_http_hold_on_cancelled_event_and_on_seat_without_event(self):
        self.apply('created');self.apply('cancelled')
        client=self.client()
        result=client.post('/holds',json={'event_id':'event-demo-001','seat_id':'A1','customer_id':'buyer'})
        self.assertEqual(result.status_code,409)
        self.assertEqual(result.json(),{'error':'event_cancelled','message':'This event has been cancelled.'})
        self.assertEqual(self.seat_rows()[0][3],'free')
        self.seed_seat()  # race-event has no event record: today's behaviour
        self.assertEqual(client.post('/holds',json={'event_id':'race-event','seat_id':'A1','customer_id':'buyer'}).status_code,201)

    def test_database_failure_records_nothing_and_raises(self):
        # A real database error after processed_messages and events were written.
        self.query("""CREATE FUNCTION fail_seat_insert() RETURNS trigger LANGUAGE plpgsql AS
                      $$ BEGIN RAISE EXCEPTION 'simulated database failure'; END $$""")
        self.query('CREATE TRIGGER fail_seats BEFORE INSERT ON seats FOR EACH ROW EXECUTE FUNCTION fail_seat_insert()')
        envelope=events_envelope('created')
        with self.assertRaises(psycopg2.Error):
            asyncio.run(events.handle(envelope))
        self.assertEqual(self.processed(),set())
        self.assertIsNone(self.event_row())
        # Once the cause is gone, the retried delivery applies normally.
        self.query('DROP TRIGGER fail_seats ON seats')
        asyncio.run(events.handle(envelope))
        self.assertEqual(self.processed(),{envelope.message_id})
        self.assertEqual(len(self.seat_rows()),2)

    def test_listener_dispatches_by_type_and_counts_outcomes(self):
        from messaging import Outcome
        asyncio.run(events.handle(events_envelope('created')))
        self.assertEqual(self.event_row()[0],1)
        other=dataclasses.replace(events_envelope('created'),type='payment.result',message_id=str(uuid.uuid4()))
        asyncio.run(events.handle(other))  # logged and acknowledged: no effect
        self.assertEqual(len(self.processed()),1)
        labels={'type':'event.changed','outcome':'processed'}
        before=REGISTRY.get_sample_value('booking_messages_total',labels) or 0
        events.track_outcome(Outcome.PROCESSED,'event.changed',None)
        self.assertEqual(REGISTRY.get_sample_value('booking_messages_total',labels),before+1)

    def test_schema_upgrade_keeps_held_seat(self):
        self.query('DROP TABLE seats, events, processed_messages')
        self.query(OLD_SEATS_TABLE)
        self.query("INSERT INTO seats (event_id,seat_id,section,status,booking_ref,customer_id) "
                   "VALUES ('event-demo-001','A1','A','held','ref-1','buyer-1')")
        db.init_schema();db.init_schema()  # safe to run on every start
        columns=[row[0] for row in self.query("SELECT column_name FROM information_schema.columns "
                                             "WHERE table_schema=%s AND table_name='seats'",(self.schema,))]
        self.assertIn('label',columns)
        self.assertEqual(self.seat_rows(),[('A1','A',None,'held','ref-1','buyer-1',None)])
        self.assertEqual(self.query('SELECT count(*) FROM events')[0][0],0)
        self.assertEqual(self.query('SELECT count(*) FROM processed_messages')[0][0],0)

    # --- Outbox and seat.availability-changed (contract sections 3, 4a and 11) ---

    def snapshots(self,event_id='event-demo-001'):
        """Availability envelopes in the outbox for one event, oldest sequence first."""
        rows=self.query("SELECT envelope FROM outbox WHERE type='seat.availability-changed'")
        found=[json.loads(text) for (text,) in rows]
        return sorted((e for e in found if e['payload']['event_id']==event_id),key=lambda e:e['payload']['sequence'])

    def outbox_count(self):
        return self.query('SELECT count(*) FROM outbox')[0][0]

    def sequence(self,event_id='event-demo-001'):
        return self.query('SELECT sequence FROM events WHERE event_id=%s',(event_id,))[0][0]

    def free_seats(self,event_id):
        return sorted(r[0] for r in self.query("SELECT seat_id FROM seats WHERE event_id=%s AND status='free'",(event_id,)))

    def big_event(self,event_id='event-test-many',seats=25,version=1,extra=()):
        """A Catalogue-style created payload with many seats (test IDs only)."""
        payload=example('created')['payload']|{'event_id':event_id,'event_version':version}
        payload['seats']=[{'seat_id':f'T{n:02d}','section':'T','label':f'T{n:02d}'} for n in range(1,seats+1)]
        payload['seats']+=[{'seat_id':seat,'section':'T','label':seat} for seat in extra]
        return payload

    def assert_valid_envelope(self,envelope):
        """What Catalogue's AvailabilityMessage accepts (catalogue/app/schemas.py)."""
        self.assertEqual(set(envelope),{'message_id','type','occurred_at','version','producer','payload'})
        uuid.UUID(envelope['message_id'])
        self.assertEqual(envelope['type'],'seat.availability-changed')
        self.assertEqual(envelope['version'],1)
        self.assertEqual(envelope['producer'],'booking')
        self.assertEqual(datetime.fromisoformat(envelope['occurred_at']).utcoffset(),timedelta(0))
        payload=envelope['payload']
        self.assertEqual(set(payload),{'event_id','sequence','seats_remaining','available_seat_ids'})
        self.assertIsInstance(payload['sequence'],int);self.assertGreater(payload['sequence'],0)
        ids=payload['available_seat_ids']
        self.assertEqual(len(set(ids)),len(ids))
        self.assertEqual(payload['seats_remaining'],len(ids))
        self.assertEqual(ids,sorted(ids))

    def test_held_hold_writes_one_complete_snapshot(self):
        self.apply('created')
        before=self.outbox_count()
        result=self.client().post('/holds',json={'event_id':'event-demo-001','seat_id':'A1','customer_id':'buyer-test'})
        self.assertEqual(result.status_code,201)
        self.assertEqual(self.outbox_count(),before+1)
        latest=self.snapshots()[-1]
        self.assert_valid_envelope(latest)
        self.assertEqual(latest['payload'],{'event_id':'event-demo-001','sequence':2,'seats_remaining':1,'available_seat_ids':['A2']})
        self.assertEqual(self.sequence(),2)
        self.assertEqual(self.query('SELECT published_at FROM outbox WHERE message_id=%s',(latest['message_id'],)),[(None,)])

    def test_other_hold_outcomes_write_nothing(self):
        self.apply('created')
        client=self.client()
        body={'event_id':'event-demo-001','seat_id':'A1','customer_id':'buyer-test-1'}
        self.assertEqual(client.post('/holds',json=body).status_code,201)
        count=self.outbox_count();seq=self.sequence()
        self.assertEqual(client.post('/holds',json=body).status_code,200)                                # retry
        self.assertEqual(client.post('/holds',json=body|{'customer_id':'buyer-test-2'}).status_code,409) # taken
        self.assertEqual(client.post('/holds',json=body|{'seat_id':'Z9'}).status_code,404)               # not found
        self.seed_seat()  # race-event has no event record
        self.assertEqual(client.post('/holds',json={'event_id':'race-event','seat_id':'A1','customer_id':'buyer-test'}).status_code,201)
        self.assertEqual((self.outbox_count(),self.sequence()),(count,seq))
        # Row 13: the cancellation releases buyer-test-1's hold on A1: one snapshot, one booking.cancelled.
        self.apply('cancelled')
        self.assertEqual((self.outbox_count(),self.sequence()),(count+2,seq+1))
        self.assertEqual(client.post('/holds',json=body|{'seat_id':'A2'}).status_code,409)               # cancelled
        self.assertEqual((self.outbox_count(),self.sequence()),(count+2,seq+1))

    def test_failed_outbox_insert_rolls_back_the_hold(self):
        self.apply('created')
        self.query("""CREATE FUNCTION fail_outbox_insert() RETURNS trigger LANGUAGE plpgsql AS
                      $$ BEGIN RAISE EXCEPTION 'simulated outbox failure'; END $$""")
        self.query('CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION fail_outbox_insert()')
        with self.assertRaises(psycopg2.Error):
            db.hold_seat('event-demo-001','A1','buyer-test')
        result=self.client().post('/holds',json={'event_id':'event-demo-001','seat_id':'A1','customer_id':'buyer-test'})
        self.assertEqual(result.status_code,503)
        self.assertEqual(self.seat_rows()[0][3:6],('free',None,None))
        self.assertEqual(self.sequence(),1)
        self.assertEqual(self.outbox_count(),1)  # only the creation snapshot
        self.query('DROP TRIGGER fail_outbox ON outbox')
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test')[0],'held')
        self.assertEqual(self.sequence(),2)

    def test_concurrent_holds_get_gapless_sequences(self):
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        k=len(self.snapshots('event-test-many'))
        self.assertEqual(k,1)
        with ThreadPoolExecutor(max_workers=20) as executor:
            results=list(executor.map(lambda n:db.hold_seat('event-test-many',f'T{n:02d}',f'buyer-test-{n}'),range(1,21)))
        self.assertEqual([outcome for outcome,ref,_ in results],['held']*20)
        found=self.snapshots('event-test-many')
        self.assertEqual([e['payload']['sequence'] for e in found],list(range(1,21+k)))
        self.assertEqual(self.sequence('event-test-many'),20+k)
        self.assertEqual(found[-1]['payload']['available_seat_ids'],self.free_seats('event-test-many'))
        self.assertEqual(found[-1]['payload']['seats_remaining'],5)
        for envelope in found:
            self.assert_valid_envelope(envelope)

    def test_concurrent_holds_and_event_changed_do_not_deadlock(self):
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        holds=[lambda n=n:db.hold_seat('event-test-many',f'T{n:02d}',f'buyer-test-{n}') for n in range(1,21)]
        # Newer versions that each add a seat, so they write snapshots too.
        changes=[lambda v=v:db.apply_event_changed(str(uuid.uuid4()),self.big_event(version=v,extra=[f'X{v}'])) for v in range(2,6)]
        jobs=holds[:10]+changes+holds[10:]
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures=[executor.submit(job) for job in jobs]
            results=[future.result(timeout=30) for future in futures]  # a deadlock error would raise here
        self.assertEqual([r[0] for r in results[:10]+results[14:]],['held']*20)
        found=self.snapshots('event-test-many')
        sequences=[e['payload']['sequence'] for e in found]
        self.assertEqual(sequences,list(range(1,len(found)+1)))
        self.assertEqual(found[-1]['payload']['available_seat_ids'],self.free_seats('event-test-many'))

    def test_first_event_changed_writes_one_snapshot(self):
        self.assertEqual(self.apply('created'),'applied')
        found=self.snapshots()
        self.assertEqual(len(found),1)
        self.assert_valid_envelope(found[0])
        self.assertEqual(found[0]['payload'],{'event_id':'event-demo-001','sequence':1,'seats_remaining':2,'available_seat_ids':['A1','A2']})
        self.assertEqual(self.apply('created'),'duplicate')                       # same message again
        self.assertEqual(self.apply('created',message_id=str(uuid.uuid4())),'stale')
        self.assertEqual(self.apply('updated'),'applied')                         # newer, no new seats
        self.assertEqual(len(self.snapshots()),1)

    def test_cancelled_first_event_writes_snapshot_with_no_seats(self):
        self.assertEqual(self.apply('cancelled'),'applied')
        found=self.snapshots()
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['payload'],{'event_id':'event-demo-001','sequence':1,'seats_remaining':0,'available_seat_ids':[]})
        self.assertEqual(self.apply('created'),'stale')
        self.assertEqual(len(self.snapshots()),1)

    def test_newer_version_with_new_seats_writes_snapshot(self):
        db.apply_event_changed(str(uuid.uuid4()),self.big_event(seats=2))
        db.apply_event_changed(str(uuid.uuid4()),self.big_event(seats=2,version=2,extra=['X1']))
        found=self.snapshots('event-test-many')
        self.assertEqual([e['payload']['sequence'] for e in found],[1,2])
        self.assertEqual(found[-1]['payload']['available_seat_ids'],['T01','T02','X1'])

    def test_backfill_gives_old_events_one_snapshot(self):
        # An event stored before this step (no sequence column yet) with one held seat.
        self.query('DROP TABLE events')
        self.query("""CREATE TABLE events (event_id TEXT PRIMARY KEY, event_version BIGINT NOT NULL,
                      status TEXT NOT NULL, title TEXT NOT NULL, starts_at TEXT NOT NULL, timezone TEXT NOT NULL)""")
        self.query("INSERT INTO events VALUES ('event-test-old',1,'scheduled','Old','2026-11-27T19:30:00+00:00','Europe/Dublin')")
        self.query("INSERT INTO seats (event_id,seat_id,section,label) VALUES ('event-test-old','A1','A','A1'),('event-test-old','A2','A','A2')")
        self.query("UPDATE seats SET status='held',booking_ref='ref-test',customer_id='buyer-test' WHERE seat_id='A1'")
        db.init_schema()
        # Give the held seat its booking, so the startup upgrade rule (4b) keeps it.
        self.query("INSERT INTO bookings (booking_id,event_id,seat_id,customer_id,customer_email,customer_name,"
                   "price_minor,currency,status,held_until) VALUES ('ref-test','event-test-old','A1','buyer-test',"
                   "'buyer-test@example.test','buyer-test',4500,'EUR','held',now()+interval '10 minutes')")
        self.assertEqual(self.sequence('event-test-old'),0)
        self.assertEqual(db.backfill_availability(),1)
        self.assertEqual(db.backfill_availability(),0)
        found=self.snapshots('event-test-old')
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['payload'],{'event_id':'event-test-old','sequence':1,'seats_remaining':1,'available_seat_ids':['A2']})
        self.client()  # startup runs the backfill too: still exactly one
        self.assertEqual(len(self.snapshots('event-test-old')),1)

    def test_startup_runs_backfill(self):
        self.query("INSERT INTO events (event_id,event_version,status,title,starts_at,timezone) "
                   "VALUES ('event-test-start',1,'scheduled','Start','2026-11-27T19:30:00+00:00','Europe/Dublin')")
        self.client()
        self.assertEqual([e['payload']['sequence'] for e in self.snapshots('event-test-start')],[1])

    def test_relay_marks_published_only_after_publish_succeeds(self):
        self.apply('created')
        stored=self.query('SELECT message_id,envelope FROM outbox')[0]

        async def fail(text):
            raise ConnectionError('broker down')
        with self.assertRaises(outbox.PublishFailed):
            asyncio.run(outbox.dispatch_once(fail))
        self.assertEqual(self.query('SELECT published_at FROM outbox'),[(None,)])
        asyncio.run(outbox.refresh_pending())
        self.assertEqual(REGISTRY.get_sample_value('booking_outbox_pending'),1)

        sent=[]
        async def succeed(text):
            sent.append(text)
        labels={'type':'seat.availability-changed'}
        before=REGISTRY.get_sample_value('booking_outbox_published_total',labels) or 0
        self.assertEqual(asyncio.run(outbox.dispatch_once(succeed)),1)
        self.assertEqual(sent,[stored[1]])  # the stored body, unchanged
        self.assertIsNotNone(self.query('SELECT published_at FROM outbox')[0][0])
        self.assertEqual(REGISTRY.get_sample_value('booking_outbox_published_total',labels),before+1)
        self.assertEqual(asyncio.run(outbox.dispatch_once(succeed)),0)  # nothing is sent twice
        asyncio.run(outbox.refresh_pending())
        self.assertEqual(REGISTRY.get_sample_value('booking_outbox_pending'),0)

    def test_race_on_event_with_record_has_one_winner_and_one_snapshot(self):
        self.apply('created')
        with ThreadPoolExecutor(max_workers=20) as executor:
            results=list(executor.map(lambda n:db.hold_seat('event-demo-001','A1',f'buyer-test-{n}'),range(20)))
        self.assertEqual(sum(outcome=='held' for outcome,ref,_ in results),1)
        self.assertEqual(sum(outcome=='taken' for outcome,ref,_ in results),19)
        self.assertEqual([e['payload']['sequence'] for e in self.snapshots()],[1,2])

    # --- Bookings: full hold, lookup, limits, upgrade rule (contract 1, 4, 4a, 4b, 5, 6, 8, 14) ---

    def bookings(self):
        return self.query('SELECT booking_id,event_id,seat_id,customer_id,customer_email,customer_name,'
                          'price_minor,currency,status,checkout_started_at,payment_id FROM bookings ORDER BY created_at')

    def booking_count(self):
        return self.query('SELECT count(*) FROM bookings')[0][0]

    def test_hold_creates_one_booking_with_fixed_price_and_database_clock(self):
        self.apply('created')
        body={'event_id':'event-demo-001','seat_id':'A1','customer_id':'buyer-test',
              'customer_email':' buyer-test@example.org ','customer_name':'Test Buyer'}
        result=self.client().post('/holds',json=body)
        self.assertEqual(result.status_code,201)
        booking_id=result.json()['booking_id']
        self.assertEqual(self.bookings(),[(booking_id,'event-demo-001','A1','buyer-test','buyer-test@example.org',
                                           'Test Buyer',4500,'EUR','held',None,None)])
        # held_until = the database's now() + 10 min, and the seat carries the same value.
        exact,ahead,seat_until,booking_until=self.query(
            "SELECT b.held_until-b.created_at, b.held_until-now(), s.held_until, b.held_until FROM bookings b "
            "JOIN seats s ON s.booking_ref=b.booking_id")[0]
        self.assertEqual(exact,timedelta(minutes=10))
        self.assertTrue(timedelta(minutes=9)<ahead<=timedelta(minutes=10))
        self.assertEqual(seat_until,booking_until)
        self.assertEqual(self.seat_rows()[0][4:6],(booking_id,'buyer-test'))

    def test_hold_without_email_and_name_stores_fallback_and_seat_without_record_gets_booking(self):
        self.seed_seat()  # race-event: no event record, so no snapshot, but a booking
        result=self.client().post('/holds',json={'event_id':'race-event','seat_id':'A1','customer_id':'buyer-test'})
        self.assertEqual(result.status_code,201)
        self.assertEqual(self.bookings()[0][3:9],('buyer-test','buyer-test@example.test','buyer-test',4500,'EUR','held'))
        self.assertEqual(self.outbox_count(),0)
        # An explicit null counts as absent.
        self.query("INSERT INTO seats (event_id,seat_id,section) VALUES ('race-event','A2','A')")
        result=self.client().post('/holds',json={'event_id':'race-event','seat_id':'A2','customer_id':'buyer-test-2',
                                                 'customer_email':None,'customer_name':None})
        self.assertEqual(result.status_code,201)
        self.assertEqual(self.bookings()[1][4:6],('buyer-test-2@example.test','buyer-test-2'))

    def test_price_and_hold_length_come_from_config(self):
        self.seed_seat()
        with patch('config.BOOKING_PRICE_MINOR',1234),patch('config.BOOKING_CURRENCY','GBP'),patch('config.HOLD_MINUTES',3):
            booking_id=db.hold_seat('race-event','A1','buyer-test')[1]
        self.assertEqual(self.bookings()[0][6:8],(1234,'GBP'))
        self.assertEqual(self.query('SELECT held_until-created_at FROM bookings WHERE booking_id=%s',(booking_id,))[0][0],
                         timedelta(minutes=3))

    def test_hold_response_keeps_old_fields_and_adds_booking_fields(self):
        self.apply('created');client=self.client()
        body={'event_id':'event-demo-001','seat_id':'A1','customer_id':'buyer-test'}
        first=client.post('/holds',json=body)
        self.assertEqual(first.status_code,201)
        ref=first.json()['booking_ref']
        self.assertEqual(first.json(),{'booking_ref':ref,'event_id':'event-demo-001','seat_id':'A1','status':'held',
                                       'booking_id':ref,'booking_status':'held'})
        retry=client.post('/holds',json=body)
        self.assertEqual(retry.status_code,200)
        self.assertEqual(retry.json(),first.json())
        # Error replies are unchanged (no booking fields).
        other=client.post('/holds',json=body|{'customer_id':'buyer-test-2'})
        self.assertEqual((other.status_code,set(other.json())),(409,{'error','message'}))

    def test_retry_returns_existing_booking_and_losers_create_none(self):
        self.apply('created')
        outcome,booking_id,status=db.hold_seat('event-demo-001','A1','buyer-test')
        self.assertEqual((outcome,status),('held','held'))
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test',customer_email='new@example.test'),
                         ('already_yours',booking_id,'held'))
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test-2'),('taken',None,None))
        self.assertEqual(db.hold_seat('event-demo-001','Z9','buyer-test'),('not_found',None,None))
        self.assertEqual(self.booking_count(),1)
        self.assertEqual(self.bookings()[0][4],'buyer-test@example.test')  # a retry changes nothing
        # The retry reports the booking's real state (here forced, as checkout will later do).
        self.query("UPDATE bookings SET status='payment_pending' WHERE booking_id=%s",(booking_id,))
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test'),('already_yours',booking_id,'payment_pending'))
        self.apply('cancelled')
        self.assertEqual(db.hold_seat('event-demo-001','A2','buyer-test'),('event_cancelled',None,None))
        self.assertEqual(self.booking_count(),1)

    def test_failed_booking_insert_rolls_back_the_whole_hold(self):
        self.apply('created');self.seed_seat()
        self.query("""CREATE FUNCTION fail_booking_insert() RETURNS trigger LANGUAGE plpgsql AS
                      $$ BEGIN RAISE EXCEPTION 'simulated booking failure'; END $$""")
        self.query('CREATE TRIGGER fail_bookings BEFORE INSERT ON bookings FOR EACH ROW EXECUTE FUNCTION fail_booking_insert()')
        for event_id in ('event-demo-001','race-event'):   # with and without an event record
            with self.assertRaises(psycopg2.Error):
                db.hold_seat(event_id,'A1','buyer-test')
            self.assertEqual(self.seat_rows(event_id)[0][3:7],('free',None,None,None))
        result=self.client().post('/holds',json={'event_id':'event-demo-001','seat_id':'A1','customer_id':'buyer-test'})
        self.assertEqual(result.status_code,503)
        self.assertEqual((self.sequence(),self.outbox_count(),self.booking_count()),(1,1,0))
        self.query('DROP TRIGGER fail_bookings ON bookings')
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test')[0],'held')
        self.assertEqual((self.sequence(),self.booking_count()),(2,1))

    def test_twenty_buyers_one_seat_one_booking(self):
        self.apply('created')
        with ThreadPoolExecutor(max_workers=20) as executor:
            results=list(executor.map(lambda n:db.hold_seat('event-demo-001','A1',f'buyer-test-{n}'),range(20)))
        winners=[r for r in results if r[0]=='held']
        self.assertEqual(len(winners),1)
        rows=self.bookings()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][0],winners[0][1])
        self.assertEqual(self.seat_rows()[0][4],winners[0][1])

    def test_lookup_owner_only_with_identical_404s(self):
        self.apply('created');client=self.client()
        booking_id=client.post('/holds',json={'event_id':'event-demo-001','seat_id':'A1','customer_id':'buyer-test'}).json()['booking_id']
        mine=client.get(f'/bookings/{booking_id}',params={'customer_id':'buyer-test'})
        self.assertEqual(mine.status_code,200)
        body=mine.json()
        stored=self.query('SELECT held_until FROM bookings')[0][0]
        self.assertEqual(body,{'booking_id':booking_id,'event_id':'event-demo-001','seat_id':'A1',
                               'booking_status':'held','held_until':body['held_until'],'payment_id':None})
        self.assertIsNotNone(datetime.fromisoformat(body['held_until']).utcoffset())
        self.assertEqual(datetime.fromisoformat(body['held_until']),stored)
        wrong=client.get(f'/bookings/{booking_id}',params={'customer_id':'buyer-test-2'})
        unknown=client.get(f'/bookings/{uuid.uuid4()}',params={'customer_id':'buyer-test'})
        for reply in (wrong,unknown):
            self.assertEqual(reply.status_code,404)
            self.assertEqual(reply.json(),{'error':'booking_not_found','message':'No such booking.'})
        self.assertEqual(wrong.content,unknown.content)
        self.assertEqual(client.get(f'/bookings/{booking_id}').status_code,422)                            # no customer_id
        self.assertEqual(client.get(f'/bookings/{booking_id}',params={'customer_id':'  '}).status_code,422)
        self.assertEqual(client.get(f'/bookings/{booking_id}',params={'customer_id':'c'*65}).status_code,422)
        self.assertEqual(client.get('/bookings/'+'b'*65,params={'customer_id':'buyer-test'}).status_code,422)
        self.assertEqual(client.get('/bookings/'+'b'*64,params={'customer_id':'buyer-test'}).status_code,404)
        self.assertEqual(client.get('/ready').json()['capabilities'],['holds','checkout'])

    def test_lookup_database_failure_is_safe_503(self):
        client=self.client()
        with patch('db.connect_once',side_effect=psycopg2.OperationalError('private details')):
            result=client.get('/bookings/some-test-id',params={'customer_id':'buyer-test'})
        self.assertEqual(result.status_code,503)
        self.assertNotIn('private',result.text)

    def test_input_limits(self):
        client=self.client()
        ok={'event_id':'e'*64,'seat_id':'s'*64,'customer_id':'c'*64,'customer_email':'m'*255,'customer_name':'n'*255}
        self.query('INSERT INTO seats (event_id,seat_id,section) VALUES (%s,%s,%s)',('e'*64,'s'*64,'A'))
        for field,too_long in (('event_id',65),('seat_id',65),('customer_id',65),('customer_email',256),('customer_name',256)):
            result=client.post('/holds',json=ok|{field:'x'*too_long})
            self.assertEqual(result.status_code,422,field)
            self.assertIn('detail',result.json())
        for field in ('customer_email','customer_name'):
            self.assertEqual(client.post('/holds',json=ok|{field:'   '}).status_code,422,field)
        self.assertEqual(self.booking_count(),0)
        result=client.post('/holds',json=ok|{'customer_id':'  '+'c'*64+'  '})   # trimmed first
        self.assertEqual(result.status_code,201)
        self.assertEqual(self.bookings()[0][1:6],('e'*64,'s'*64,'c'*64,'m'*255,'n'*255))

    def mark_held_without_booking(self,event_id,seat_id):
        """A hold as the old hold-only code left it: seat held, no booking row."""
        self.query("UPDATE seats SET status='held',booking_ref=%s,customer_id='buyer-test-old' WHERE event_id=%s AND seat_id=%s",
                   (f'ref-test-old-{event_id}-{seat_id}',event_id,seat_id))

    def test_upgrade_frees_held_seats_without_booking(self):
        self.apply('created')                                                      # event-demo-001: A1, A2; sequence 1
        db.apply_event_changed(str(uuid.uuid4()),self.big_event(seats=3))          # event-test-many: T01..T03; sequence 1
        kept=db.hold_seat('event-test-many','T01','buyer-test')[1]                 # a real booking; sequence 2
        self.mark_held_without_booking('event-demo-001','A1')
        self.mark_held_without_booking('event-demo-001','A2')
        self.mark_held_without_booking('event-test-many','T02')
        self.seed_seat();self.mark_held_without_booking('race-event','A1')         # no event record
        outbox_before=self.outbox_count()
        self.assertEqual(db.free_holds_without_booking(),(4,2))
        self.assertEqual([r[3:6] for r in self.seat_rows()],[('free',None,None)]*2)
        self.assertEqual([r[3:7] for r in self.seat_rows('race-event')],[('free',None,None,None)])
        self.assertEqual([r[0] for r in self.seat_rows('event-test-many') if r[3]=='held'],['T01'])
        self.assertEqual(self.seat_rows('event-test-many')[0][4],kept)
        # One snapshot per affected event with a record; none for race-event.
        self.assertEqual(self.outbox_count(),outbox_before+2)
        self.assertEqual(self.snapshots()[-1]['payload'],
                         {'event_id':'event-demo-001','sequence':2,'seats_remaining':2,'available_seat_ids':['A1','A2']})
        self.assertEqual(self.snapshots('event-test-many')[-1]['payload'],
                         {'event_id':'event-test-many','sequence':3,'seats_remaining':2,'available_seat_ids':['T02','T03']})
        self.assertEqual(self.snapshots('race-event'),[])
        for envelope in self.snapshots()+self.snapshots('event-test-many'):
            self.assert_valid_envelope(envelope)
        # A second run (and a restart) changes nothing.
        self.assertEqual(db.free_holds_without_booking(),(0,0))
        self.client()
        self.assertEqual(self.outbox_count(),outbox_before+2)
        self.assertEqual(self.booking_count(),1)

    def test_startup_upgrade_runs_before_backfill(self):
        # An old event (sequence 0, never snapshotted) with a hold-only held seat.
        self.query("INSERT INTO events (event_id,event_version,status,title,starts_at,timezone) "
                   "VALUES ('event-test-start',1,'scheduled','Start','2026-11-27T19:30:00+00:00','Europe/Dublin')")
        self.query("INSERT INTO seats (event_id,seat_id,section,label) VALUES ('event-test-start','A1','A','A1'),('event-test-start','A2','A','A2')")
        self.mark_held_without_booking('event-test-start','A1')
        self.client()
        found=self.snapshots('event-test-start')
        # Exactly one snapshot (from the upgrade; the backfill then skips it), showing A1 free again.
        self.assertEqual([e['payload'] for e in found],
                         [{'event_id':'event-test-start','sequence':1,'seats_remaining':2,'available_seat_ids':['A1','A2']}])

    # --- Step 6b-1: relay batch, temporary vs permanent failures, write_outbox, relay connection ---

    def add_outbox_rows(self,count,message_type='booking.test-relay'):
        """count outbox rows, created_at one second apart, INSERTED NEWEST FIRST
        (so only created_at can give the right order). Returns message_ids, oldest first."""
        from messaging import Envelope
        envelopes=[Envelope.new(message_type,{'n':n},producer='booking') for n in range(count)]
        connection=self.connect()
        try:
            with connection,connection.cursor() as cursor:
                for n in reversed(range(count)):
                    cursor.execute("INSERT INTO outbox (message_id,type,envelope,created_at) VALUES (%s,%s,%s,"
                                   "timestamptz '2026-01-01 00:00:00+00' + make_interval(secs => %s))",
                                   (envelopes[n].message_id,message_type,envelopes[n].to_json().decode(),n))
        finally:
            connection.close()
        return [e.message_id for e in envelopes]

    def recorder(self,fail=None):
        """A fake publish. fail(message_id) may return an exception to raise instead."""
        sent=[]
        async def publish(text):
            message_id=json.loads(text)['message_id']
            error=fail(message_id) if fail else None
            if error is not None:
                raise error
            sent.append(message_id)
        return publish,sent

    def outbox_state(self,message_id):
        """(published?, set aside?, error) for one outbox row."""
        published,failed,error=self.query('SELECT published_at,failed_at,error FROM outbox WHERE message_id=%s',(message_id,))[0]
        return (published is not None,failed is not None,error)

    def gauges(self):
        """booking_outbox_pending and booking_outbox_failed, after a refresh from the database."""
        asyncio.run(outbox.refresh_pending())
        return (REGISTRY.get_sample_value('booking_outbox_pending'),REGISTRY.get_sample_value('booking_outbox_failed'))

    def test_relay_publishes_oldest_fifty_then_the_rest(self):                                   # T1
        ids=self.add_outbox_rows(60)
        publish,sent=self.recorder()
        self.assertEqual(asyncio.run(outbox.dispatch_once(publish)),50)
        self.assertEqual(sent,ids[:50])
        self.assertEqual(asyncio.run(outbox.dispatch_once(publish)),10)
        self.assertEqual(sent,ids)
        self.assertEqual(self.gauges(),(0,0))

    def test_temporary_failure_keeps_the_row_and_later_rows_pending(self):                       # T2
        ids=self.add_outbox_rows(3)
        # A confirm timeout: aiormq's publish raises TimeoutError (asyncio.timeout).
        publish,sent=self.recorder(lambda m:TimeoutError('no confirm within 5 s') if m==ids[1] else None)
        with self.assertRaises(outbox.PublishFailed) as caught:
            asyncio.run(outbox.dispatch_once(publish))
        self.assertIsInstance(caught.exception.__cause__,TimeoutError)
        self.assertEqual(sent,[ids[0]])
        self.assertEqual([self.outbox_state(m) for m in ids],[(True,False,None),(False,False,None),(False,False,None)])
        self.assertEqual(self.gauges(),(2,0))
        # A lost connection is temporary too; once the broker is back the rows go out in order.
        publish,sent=self.recorder(lambda m:ConnectionError('connection lost') if m==ids[1] else None)
        with self.assertRaises(outbox.PublishFailed):
            asyncio.run(outbox.dispatch_once(publish))
        publish,sent=self.recorder()
        self.assertEqual(asyncio.run(outbox.dispatch_once(publish)),2)
        self.assertEqual(sent,ids[1:])
        self.assertEqual(self.gauges(),(0,0))

    def test_permanent_failure_sets_the_row_aside_and_the_relay_moves_on(self):                 # T3
        ids=self.add_outbox_rows(3)
        tried=[]
        def fail(message_id):
            tried.append(message_id)
            return unroutable_error() if message_id==ids[1] else None
        publish,sent=self.recorder(fail)
        with self.assertLogs('booking.outbox','WARNING') as logs:
            self.assertEqual(asyncio.run(outbox.dispatch_once(publish)),2)
        self.assertEqual(sent,[ids[0],ids[2]])                       # row 3 went out in the same run
        published,set_aside,error=self.outbox_state(ids[1])
        self.assertEqual((published,set_aside),(False,True))
        for part in ('PublishError','312','NO_ROUTE'):
            self.assertIn(part,error)
        self.assertTrue(any(ids[1] in line and 'booking.test-relay' in line for line in logs.output))
        self.assertEqual((db.count_pending_outbox(),db.count_failed_outbox()),(0,1))
        self.assertEqual(self.gauges(),(0,1))
        tried.clear()
        self.assertEqual(asyncio.run(outbox.dispatch_once(publish)),0)
        self.assertEqual(tried,[])                                    # never retried
        # Only an unpublished, not-yet-set-aside row can be set aside; the error is cut to 500.
        self.assertFalse(db.set_aside_outbox(ids[1],'again'))
        self.assertEqual(self.outbox_state(ids[1])[2],error)
        self.assertFalse(db.set_aside_outbox(ids[0],'published already'))
        (extra,)=self.add_outbox_rows(1)
        self.assertTrue(db.set_aside_outbox(extra,'x'*600))
        self.assertEqual(len(self.outbox_state(extra)[2]),500)

    def test_broker_nack_is_temporary(self):                                                    # T4
        ids=self.add_outbox_rows(3)
        publish,sent=self.recorder(lambda m:nack_error() if m==ids[1] else None)
        with self.assertRaises(outbox.PublishFailed) as caught:
            asyncio.run(outbox.dispatch_once(publish))
        from aio_pika.exceptions import DeliveryError,PublishError
        self.assertIsInstance(caught.exception.__cause__,DeliveryError)
        self.assertNotIsInstance(caught.exception.__cause__,PublishError)
        self.assertEqual(sent,[ids[0]])
        self.assertEqual(self.outbox_state(ids[1]),(False,False,None))
        self.assertEqual(self.gauges(),(2,0))
        self.assertFalse(outbox.is_permanent(nack_error()))
        self.assertTrue(outbox.is_permanent(unroutable_error()))

    def test_write_outbox_inside_the_callers_transaction(self):                                 # T5
        from messaging import Envelope
        payload={'booking_id':'booking-test-1','count':1,'items':['a','b']}
        connection=self.connect()
        try:
            with connection,connection.cursor() as cursor:
                message_id=db.write_outbox(cursor,'booking.test-type',payload)
                self.assertEqual(self.outbox_count(),0)   # not committed: another connection cannot see it
        finally:
            connection.close()
        row_type,text,published_at,failed_at=self.query(
            'SELECT type,envelope,published_at,failed_at FROM outbox WHERE message_id=%s',(message_id,))[0]
        envelope=Envelope.from_json(text.encode())
        self.assertEqual((row_type,envelope.type),('booking.test-type','booking.test-type'))
        self.assertEqual(envelope.message_id,message_id)
        self.assertEqual(str(uuid.UUID(message_id)),message_id)
        self.assertEqual((envelope.producer,envelope.version,envelope.payload),('booking',1,payload))
        self.assertEqual((published_at,failed_at),(None,None))
        # A rolled-back transaction leaves no row.
        connection=self.connect()
        try:
            cursor=connection.cursor()
            other=db.write_outbox(cursor,'booking.test-type',payload)
            connection.rollback()
        finally:
            connection.close()
        self.assertEqual(self.query('SELECT count(*) FROM outbox WHERE message_id=%s',(other,))[0][0],0)
        self.assertEqual(self.outbox_count(),1)

    def test_snapshot_envelope_is_unchanged(self):                                              # T6
        self.apply('created')
        connection=self.connect()
        try:
            with connection,connection.cursor() as cursor:
                db._lock_event_row(cursor,'event-demo-001')
                self.assertEqual(db.write_availability_snapshot(cursor,'event-demo-001'),2)  # returns the sequence
        finally:
            connection.close()
        found=self.snapshots()
        self.assertEqual(len(found),2)
        for envelope in found:
            self.assert_valid_envelope(envelope)   # exactly event_id, sequence, seats_remaining, available_seat_ids
        self.assertEqual(found[-1]['payload'],{'event_id':'event-demo-001','sequence':2,'seats_remaining':2,
                                               'available_seat_ids':['A1','A2']})
        self.assertEqual(self.query('SELECT type FROM outbox WHERE message_id=%s',(found[-1]['message_id'],)),
                         [('seat.availability-changed',)])

    def test_relay_reuses_one_connection_and_reconnects_after_an_error(self):                   # T7
        ids=self.add_outbox_rows(2)
        opened=[]
        def counting_connect():
            connection=self.connect();opened.append(connection);return connection
        def backend_pid():
            def work(cursor):
                cursor.execute('SELECT pg_backend_pid()');return cursor.fetchone()[0]
            return db._relay_query(work)
        with patch('db.connect_once',counting_connect):
            db.close_relay_connection()
            first=backend_pid()
            db.pending_outbox();db.mark_published(ids[0]);db.set_aside_outbox(ids[1],'test error')
            db.count_pending_outbox();db.count_failed_outbox()
            asyncio.run(outbox.refresh_pending())
            asyncio.run(outbox.dispatch_once(self.recorder()[0]))
            with ThreadPoolExecutor(max_workers=10) as executor:       # the lock: never two at once
                self.assertEqual(list(executor.map(lambda _:db.count_failed_outbox(),range(20))),[1]*20)
            self.assertEqual(backend_pid(),first)
            self.assertEqual(len(opened),1)
            # Each call committed its own work: another connection already sees it.
            self.assertEqual(self.outbox_state(ids[0])[:2],(True,False))
            self.assertEqual(self.outbox_state(ids[1]),(False,True,'test error'))
            # A forced connection error: the server ends the relay's session.
            self.query('SELECT pg_terminate_backend(%s)',(first,))
            for _ in range(50):
                if not self.query('SELECT 1 FROM pg_stat_activity WHERE pid=%s',(first,)):
                    break
                asyncio.run(asyncio.sleep(0.1))
            with self.assertRaises(psycopg2.Error):
                db.count_pending_outbox()
            self.assertEqual(db.count_pending_outbox(),0)              # the next call reconnects and works
            self.assertEqual(len(opened),2)
            second=backend_pid()
            self.assertNotEqual(second,first)
            # Any other database error also closes it; the next call opens a new one.
            with self.assertRaises(psycopg2.Error):
                db._relay_query(lambda cursor:cursor.execute('SELECT 1/0'))
            self.assertEqual(db.count_failed_outbox(),1)
            self.assertEqual(len(opened),3)
            self.assertNotEqual(backend_pid(),second)

    def test_bad_event_changed_raises_and_records_nothing(self):                                # T8
        base=events_envelope('created')
        without=lambda field:{k:v for k,v in base.payload.items() if k!=field}
        bad={'unknown status':base.payload|{'status':'postponed'},
             'event_version 0':base.payload|{'event_version':0},
             'event_version text':base.payload|{'event_version':'1'},
             'event_version bool':base.payload|{'event_version':True},
             'missing event_id':without('event_id'),
             'missing status':without('status'),
             'missing title':without('title'),
             'missing timezone':without('timezone')}
        for name,payload in bad.items():
            with self.subTest(name):
                envelope=dataclasses.replace(base,message_id=str(uuid.uuid4()),payload=payload)
                with self.assertRaises((ValueError,KeyError)):
                    asyncio.run(events.handle(envelope))
                self.assertEqual(self.processed(),set())
                self.assertIsNone(self.event_row())
                self.assertEqual(self.seat_rows(),[])
                self.assertEqual(self.outbox_count(),0)

    def test_http_metrics_use_the_route_template(self):                                         # T9
        client=self.client()
        raw='booking-test-metrics-'+uuid.uuid4().hex[:12]
        self.assertEqual(client.get(f'/bookings/{raw}',params={'customer_id':'test-buyer-metrics'}).status_code,404)
        text=client.get('/metrics').text
        self.assertIn('path="/bookings/{booking_id}"',text)
        self.assertNotIn(raw,text)
        self.assertNotIn('test-buyer-metrics',text)

    def test_cancelled_event_beats_seat_taken_and_seat_not_found(self):                         # T10
        self.apply('created')
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test-1')[0],'held')
        self.apply('cancelled')
        client=self.client()
        for seat in ('A1','Z9'):        # A1: held by someone else; Z9: no such seat
            with self.subTest(seat):
                result=client.post('/holds',json={'event_id':'event-demo-001','seat_id':seat,'customer_id':'buyer-test-2'})
                self.assertEqual(result.status_code,409)
                self.assertEqual(result.json()['error'],'event_cancelled')
                self.assertEqual(db.hold_seat('event-demo-001',seat,'buyer-test-2'),('event_cancelled',None,None))

    def test_newer_event_changed_fills_only_an_empty_section(self):                             # T11
        self.apply('created')
        self.query("UPDATE seats SET section='' WHERE event_id='event-demo-001' AND seat_id='A1'")
        self.query("UPDATE seats SET section='Kept section' WHERE event_id='event-demo-001' AND seat_id='A2'")
        self.assertEqual(self.apply('updated'),'applied')     # version 2, scheduled, seats A1 and A2 in section A
        a1,a2=self.seat_rows()
        self.assertEqual(a1[:4],('A1','A','A1','free'))
        self.assertEqual(a2[:4],('A2','Kept section','A2','free'))

    def test_demo_seed_off_by_default_and_runs_only_when_config_is_true(self):                 # T12
        import subprocess,sys
        def config_value(env):
            return subprocess.run([sys.executable,'-c','import config; print(config.BOOKING_DEMO_SEED)'],
                                  env=env,cwd=Path(db.__file__).parent,capture_output=True,text=True,check=True).stdout.strip()
        unset={k:v for k,v in os.environ.items() if k!='BOOKING_DEMO_SEED'}
        self.assertEqual(config_value(unset),'False')
        self.assertEqual(config_value(unset|{'BOOKING_DEMO_SEED':'true'}),'True')
        self.client()                                          # config value False
        self.assertEqual(self.query('SELECT count(*) FROM seats')[0][0],0)
        with patch('config.BOOKING_DEMO_SEED',True):
            with TestClient(app):
                pass
        self.assertGreater(self.query('SELECT count(*) FROM seats')[0][0],0)

    # --- Checkout and Payment's HTTP answer (step 6b-2; contract sections 4, 6, 9, 12) ---

    def checkout_client(self,reply=None,gate=None,hang=False,safety=False):
        """A TestClient whose Payment is a fake (httpx.MockTransport), with no real pauses.

        reply(body) -> httpx.Response (default: paid). If gate (a threading.Event) is
        given, the fake waits for it before answering, without blocking the event
        loop. hang=True: the fake never answers (cancelled at shutdown).
        self.payment_calls lists (request body, booking status in the database at
        the moment Payment was called).
        """
        self.payment_calls=[]
        self.fake_now=0.0
        reply=reply or (lambda body:payment_reply('paid',body))
        if gate is not None:
            self.addCleanup(gate.set)       # never leave a waiting fake behind

        async def handler(request):
            body=json.loads(request.content)
            rows=await asyncio.to_thread(self.query,'SELECT status FROM bookings WHERE booking_id=%s',(body['booking_id'],))
            self.payment_calls.append((body,rows[0][0]))
            if hang:
                await asyncio.sleep(60)
            if gate is not None:
                await asyncio.to_thread(gate.wait,10)
            return reply(body)

        async def no_pause(seconds):
            pass

        def create_runner(url):
            self.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
            breaker=payments.CircuitBreaker(clock=lambda:self.fake_now)
            return payments.CheckoutRunner(payments.PaymentClient(self.http,url,breaker,sleep=no_pause))
        factory=patch('payments.create_runner',create_runner)
        factory.start();self.addCleanup(factory.stop)
        return self.client(safety=safety)

    def hold(self,client,seat='A1',customer='buyer-test',event_id='event-demo-001'):
        result=client.post('/holds',json={'event_id':event_id,'seat_id':seat,'customer_id':customer})
        self.assertEqual(result.status_code,201)
        return result.json()['booking_id']

    def checkout(self,client,booking_id,customer='buyer-test'):
        return client.post(f'/bookings/{booking_id}/checkout',json={'customer_id':customer})

    def wait_until(self,condition,timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail('condition not reached in time')

    def booking_status(self,booking_id):
        return self.query('SELECT status FROM bookings WHERE booking_id=%s',(booking_id,))[0][0]

    def wait_for_status(self,booking_id,wanted):
        self.wait_until(lambda:self.booking_status(booking_id)==wanted)

    def background_idle(self):
        return not app.state.checkout._tasks

    def messages(self,message_type):
        """Outbox envelopes of one type, oldest first."""
        rows=self.query('SELECT envelope FROM outbox WHERE type=%s ORDER BY created_at',(message_type,))
        return [json.loads(text) for (text,) in rows]

    def assert_booking_envelope(self,envelope,message_type):
        """Parses as the shared consumer does, and its type is bound to a queue (never set aside)."""
        from messaging import QUEUES, Envelope
        parsed=Envelope.from_json(json.dumps(envelope).encode())
        uuid.UUID(parsed.message_id)
        self.assertEqual((parsed.type,parsed.version,parsed.producer),(message_type,1,'booking'))
        self.assertTrue(any(message_type in queue.bindings for queue in QUEUES))

    def counter(self,name,**labels):
        return REGISTRY.get_sample_value(name,labels) or 0

    def test_checkout_commits_then_replies_202_before_payment_answers(self):                   # C1, C2, C16
        self.apply('created')
        gate=threading.Event()
        client=self.checkout_client(gate=gate)
        booking_id=self.hold(client)
        started=self.counter('booking_checkouts_total',outcome='started')
        confirmed=self.counter('booking_payment_answers_total',result='confirmed')
        result=self.checkout(client,booking_id)
        # The 202 arrived while the fake Payment was still waiting for the gate.
        self.assertEqual(result.status_code,202)
        self.assertEqual(result.json(),{'booking_id':booking_id,'booking_status':'payment_pending'})
        self.assertEqual(self.query('SELECT status,checkout_started_at IS NOT NULL FROM bookings')[0],('payment_pending',True))
        self.wait_until(lambda:len(self.payment_calls)==1)
        body,status_when_called=self.payment_calls[0]
        self.assertEqual(status_when_called,'payment_pending')    # committed BEFORE Payment was called
        self.assertEqual(body,{'booking_id':booking_id,'amount':4500,'currency':'EUR',
                               'customer_email':'buyer-test@example.test'})
        # A repeat while the payment is in progress: 202 again, no second call.
        repeat=self.checkout(client,booking_id)
        self.assertEqual((repeat.status_code,repeat.json()['booking_status']),(202,'payment_pending'))
        gate.set()
        self.wait_for_status(booking_id,'confirmed')
        self.wait_until(self.background_idle)
        self.assertEqual(len(self.payment_calls),1)
        self.assertEqual(self.counter('booking_checkouts_total',outcome='started'),started+1)
        self.assertEqual(self.counter('booking_payment_answers_total',result='confirmed'),confirmed+1)
        # Confirmed is final: a later checkout is refused.
        late=self.checkout(client,booking_id)
        self.assertEqual((late.status_code,late.json()['error']),(409,'not_checkoutable'))

    def test_checkout_unknown_booking_wrong_customer_and_bad_input(self):                       # C3
        self.apply('created')
        client=self.checkout_client()
        booking_id=self.hold(client)
        unknown=self.checkout(client,'booking-test-unknown')
        wrong=self.checkout(client,booking_id,customer='someone-else-test')
        self.assertEqual((unknown.status_code,wrong.status_code),(404,404))
        self.assertEqual(unknown.json(),wrong.json())
        self.assertEqual(unknown.json()['error'],'booking_not_found')
        self.assertEqual(client.post(f'/bookings/{booking_id}/checkout',json={'customer_id':' '}).status_code,422)
        self.assertEqual(client.post(f'/bookings/{booking_id}/checkout',json={}).status_code,422)
        self.assertEqual(self.checkout(client,'b'*65).status_code,422)
        self.assertEqual(self.booking_status(booking_id),'held')
        self.assertEqual(self.payment_calls,[])

    def test_checkout_refusals_cancelled_expired_and_no_event_record(self):                    # C4
        self.apply('created')
        client=self.checkout_client()
        expired=self.hold(client,seat='A2',customer='buyer-test-2')
        self.query("UPDATE bookings SET held_until=now()-interval '1 second' WHERE booking_id=%s",(expired,))
        result=self.checkout(client,expired,customer='buyer-test-2')
        self.assertEqual((result.status_code,result.json()['error']),(409,'not_checkoutable'))
        cancelled=self.hold(client)
        self.apply('cancelled')
        result=self.checkout(client,cancelled)
        self.assertEqual((result.status_code,result.json()['error']),(409,'event_cancelled'))
        self.seed_seat()     # race-event has no event record
        no_record=self.hold(client,seat='A1',customer='buyer-test-3',event_id='race-event')
        result=self.checkout(client,no_record,customer='buyer-test-3')
        self.assertEqual((result.status_code,result.json()['error']),(409,'not_checkoutable'))
        # Row 13 released both held bookings of the cancelled event (the expired one too);
        # the booking without an event record is still held (the sweeper is idle here).
        self.assertEqual([self.booking_status(b) for b in (expired,cancelled,no_record)],['released','released','held'])
        self.assertEqual(self.payment_calls,[])

    def test_breaker_open_or_trial_running_refuses_checkout_and_keeps_the_hold(self):          # C5
        self.apply('created')
        client=self.checkout_client()
        booking_id=self.hold(client)
        breaker=app.state.checkout.breaker
        for _ in range(5):
            breaker.record_failure()
        outbox_rows=self.outbox_count()
        result=self.checkout(client,booking_id)
        self.assertEqual((result.status_code,result.json()['error']),(503,'payment_service_unavailable'))
        self.assertEqual(self.query('SELECT status,checkout_started_at FROM bookings')[0],('held',None))
        self.assertEqual(self.outbox_count(),outbox_rows)
        self.assertEqual(client.get('/metrics').text.count('booking_payment_breaker_state 1.0'),1)
        # 30 s later, but the one trial is already running: still 503.
        self.fake_now+=30
        self.assertTrue(breaker.try_acquire())
        self.assertEqual(self.checkout(client,booking_id).status_code,503)
        # The trial ends without an answer either way (409/422): a checkout may become the next trial.
        breaker.record_neutral()
        self.assertEqual(self.checkout(client,booking_id).status_code,202)
        self.wait_for_status(booking_id,'confirmed')
        self.assertEqual(breaker.state,payments.CLOSED)
        self.assertEqual(len(self.payment_calls),1)

    def test_cancelled_event_beats_the_open_breaker(self):                                      # C5 (order)
        self.apply('created')
        client=self.checkout_client()
        booking_id=self.hold(client)
        self.apply('cancelled')
        for _ in range(5):
            app.state.checkout.breaker.record_failure()
        result=self.checkout(client,booking_id)
        self.assertEqual((result.status_code,result.json()['error']),(409,'event_cancelled'))

    def test_paid_confirms_books_the_seat_and_writes_booking_confirmed(self):                 # C6, C15 (row 5)
        long_title='T'*300
        self.apply('created',title=long_title,seats=[{'seat_id':'A1','section':'A','label':'L'*100},
                                                      {'seat_id':'A2','section':'A'}])   # A2: no label
        client=self.checkout_client()
        first=self.hold(client)
        second=self.hold(client,seat='A2',customer='buyer-test-2')
        snapshots=len(self.snapshots())
        self.assertEqual(self.checkout(client,first).status_code,202)
        self.assertEqual(self.checkout(client,second,customer='buyer-test-2').status_code,202)
        self.wait_for_status(first,'confirmed');self.wait_for_status(second,'confirmed')
        self.assertEqual(self.query('SELECT payment_id FROM bookings WHERE booking_id=%s',(first,))[0][0],'pay-test-'+first[:8])
        self.assertEqual([row[3] for row in self.seat_rows()],['booked','booked'])
        self.assertEqual(len(self.snapshots()),snapshots)        # row 5 changes no free seats
        found={e['payload']['booking_id']:e for e in self.messages('booking.confirmed')}
        self.assertEqual(set(found),{first,second})
        for envelope in found.values():
            self.assert_booking_envelope(envelope,'booking.confirmed')
        self.assertEqual(found[first]['payload'],{
            'booking_id':first,'customer_email':'buyer-test@example.test','customer_name':'buyer-test',
            'event_id':'event-demo-001','event_title':'T'*200,'event_starts_at':'2026-11-27T19:30:00+00:00',
            'seat_label':'L'*64})
        self.assertEqual(found[second]['payload']['seat_label'],'A2')   # falls back to the seat ID

    def test_paid_after_the_event_was_cancelled_requests_a_refund(self):                      # C7 (row 6)
        self.apply('created')
        gate=threading.Event()
        client=self.checkout_client(gate=gate)
        booking_id=self.hold(client)
        self.assertEqual(self.checkout(client,booking_id).status_code,202)
        self.wait_until(lambda:len(self.payment_calls)==1)
        # The event is cancelled while the payment is in progress.
        self.assertEqual(self.apply('cancelled'),'applied')
        sequence=self.sequence()
        gate.set()
        self.wait_for_status(booking_id,'refund_required')
        payment_id='pay-test-'+booking_id[:8]
        self.assertEqual(self.query('SELECT payment_id FROM bookings')[0][0],payment_id)
        self.assertEqual(self.seat_rows()[0][3:7],('free',None,None,None))
        latest=self.snapshots()[-1]
        self.assertEqual(latest['payload']['sequence'],sequence+1)
        self.assertIn('A1',latest['payload']['available_seat_ids'])
        [cancelled]=self.messages('booking.cancelled')
        [refund]=self.messages('refund.requested')
        self.assert_booking_envelope(cancelled,'booking.cancelled')
        self.assert_booking_envelope(refund,'refund.requested')
        self.assertEqual(cancelled['payload'],{'booking_id':booking_id,'reason':'event_cancelled',
                                               'customer_email':'buyer-test@example.test','customer_name':'buyer-test',
                                               'event_title':'Evening Performance - revised'})
        self.assertEqual(refund['payload'],{'booking_id':booking_id,'payment_id':payment_id,
                                            'customer_email':'buyer-test@example.test'})
        self.assertEqual(self.messages('booking.confirmed'),[])

    def test_failed_or_refunded_releases_the_seat(self):                                        # C8 (row 7)
        self.apply('created')
        statuses={}
        client=self.checkout_client(reply=lambda body:payment_reply(statuses[body['booking_id']],body))
        failed=self.hold(client)
        refunded=self.hold(client,seat='A2',customer='buyer-test-2')
        statuses.update({failed:'failed',refunded:'refunded'})
        sequence=self.sequence()
        self.assertEqual(self.checkout(client,failed).status_code,202)
        self.assertEqual(self.checkout(client,refunded,customer='buyer-test-2').status_code,202)
        self.wait_for_status(failed,'released');self.wait_for_status(refunded,'released')
        self.assertEqual(self.query('SELECT count(*) FROM bookings WHERE payment_id IS NOT NULL')[0][0],2)
        self.assertEqual(self.free_seats('event-demo-001'),['A1','A2'])
        self.assertEqual(self.sequence(),sequence+2)
        self.assertEqual(self.snapshots()[-1]['payload']['available_seat_ids'],['A1','A2'])
        cancelled=self.messages('booking.cancelled')
        self.assertEqual(sorted(e['payload']['booking_id'] for e in cancelled),sorted([failed,refunded]))
        self.assertEqual({e['payload']['reason'] for e in cancelled},{'payment_failed'})
        self.assertEqual(self.messages('refund.requested'),[])

    def test_answer_for_a_booking_not_payment_pending_changes_nothing(self):                    # C9
        self.apply('created')
        booking_id=db.hold_seat('event-demo-001','A1','buyer-test')[1]
        rows=self.outbox_count()
        self.assertEqual(db.apply_payment_answer(booking_id,'paid','pay-test-1'),'noop')      # still held
        self.assertEqual(db.start_checkout(booking_id,'buyer-test',True),'started')
        self.assertEqual(db.apply_payment_answer(booking_id,'paid','pay-test-1'),'confirmed')
        self.assertEqual(db.apply_payment_answer(booking_id,'failed','pay-test-1'),'noop')    # second answer
        self.assertEqual(self.booking_status(booking_id),'confirmed')
        self.assertEqual(self.seat_rows()[0][3],'booked')
        self.assertEqual(self.outbox_count(),rows+1)                                          # one booking.confirmed

    def test_no_answer_leaves_the_booking_payment_pending(self):                                # C10
        self.apply('created')
        client=self.checkout_client(reply=lambda body:httpx.Response(503,json={'detail':'down'}))
        booking_id=self.hold(client)
        rows=self.outbox_count()
        no_answer=self.counter('booking_payment_answers_total',result='no_answer')
        self.assertEqual(self.checkout(client,booking_id).status_code,202)
        self.wait_until(lambda:len(self.payment_calls)==3)
        self.wait_until(self.background_idle)
        self.assertEqual(self.booking_status(booking_id),'payment_pending')
        self.assertEqual(self.seat_rows()[0][3],'held')
        self.assertEqual(self.outbox_count(),rows)
        self.assertEqual(self.counter('booking_payment_answers_total',result='no_answer'),no_answer+1)

    def test_payment_answers_holds_and_event_changed_do_not_deadlock(self):                     # C11
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        pending=[]
        for n in range(1,6):
            booking_id=db.hold_seat('event-test-many',f'T{n:02d}',f'buyer-test-{n}')[1]
            self.assertEqual(db.start_checkout(booking_id,f'buyer-test-{n}',True),'started')
            pending.append(booking_id)
        answers=[lambda b=b,s=s:db.apply_payment_answer(b,s,'pay-test-'+b[:8])
                 for b,s in zip(pending,['paid','failed','paid','refunded','paid'])]
        holds=[lambda n=n:db.hold_seat('event-test-many',f'T{n:02d}',f'buyer-test-{n}') for n in range(6,16)]
        changes=[lambda v=v:db.apply_event_changed(str(uuid.uuid4()),self.big_event(version=v,extra=[f'X{v}'])) for v in range(2,5)]
        jobs=holds[:5]+answers[:2]+changes+answers[2:]+holds[5:]
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures=[executor.submit(job) for job in jobs]
            for future in futures:
                future.result(timeout=30)       # a deadlock error would raise here
        statuses=dict(self.query('SELECT booking_id,status FROM bookings WHERE booking_id = ANY(%s)',(pending,)))
        self.assertEqual([statuses[b] for b in pending],['confirmed','released','confirmed','released','confirmed'])
        found=self.snapshots('event-test-many')
        self.assertEqual([e['payload']['sequence'] for e in found],list(range(1,len(found)+1)))
        self.assertEqual(found[-1]['payload']['available_seat_ids'],self.free_seats('event-test-many'))

    def test_failed_outbox_insert_rolls_back_the_whole_answer(self):                            # C12
        self.apply('created')
        booking_id=db.hold_seat('event-demo-001','A1','buyer-test')[1]
        self.assertEqual(db.start_checkout(booking_id,'buyer-test',True),'started')
        self.query("""CREATE FUNCTION fail_outbox_insert() RETURNS trigger LANGUAGE plpgsql AS
                      $$ BEGIN RAISE EXCEPTION 'simulated outbox failure'; END $$""")
        self.query('CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION fail_outbox_insert()')
        for status in ('paid','failed'):
            with self.subTest(status),self.assertRaises(psycopg2.Error):
                db.apply_payment_answer(booking_id,status,'pay-test-1')
        self.assertEqual(self.query('SELECT status,payment_id FROM bookings')[0],('payment_pending',None))
        self.assertEqual(self.seat_rows()[0][3:5],('held',booking_id))
        self.assertEqual(self.sequence(),2)      # creation snapshot + the hold's snapshot only

    def test_shutdown_cancels_a_call_in_flight(self):                                          # C14
        self.apply('created')
        client=self.checkout_client(hang=True)
        booking_id=self.hold(client)
        self.assertEqual(self.checkout(client,booking_id).status_code,202)
        self.wait_until(lambda:len(self.payment_calls)==1)
        client.__exit__(None,None,None)          # the app shuts down while Payment has not answered
        self.assertEqual(self.booking_status(booking_id),'payment_pending')
        self.assertTrue(self.http.is_closed)

    def test_startup_refuses_a_bad_currency(self):                                              # P11 (lifespan)
        with patch('config.BOOKING_CURRENCY','eur'):
            with self.assertRaises(ValueError):
                with TestClient(app):
                    pass

    # --- booking.payment-updates (step 6b-3; contract sections 3, 4 rows 5-7, 10, 11, and 9) ---

    def pending_booking(self,seat='T01',customer='buyer-test'):
        """A payment_pending booking on event-test-many (created on first use)."""
        if not self.query("SELECT 1 FROM events WHERE event_id='event-test-many'"):
            db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        booking_id=db.hold_seat('event-test-many',seat,customer)[1]
        self.assertEqual(db.start_checkout(booking_id,customer,True),'started')
        return booking_id

    def refund_required_booking(self,seat='T01',customer='buyer-test'):
        """A booking in refund_required: paid after its event was cancelled (row 6)."""
        booking_id=self.pending_booking(seat,customer)
        db.apply_event_changed(str(uuid.uuid4()),self.big_event(version=2)|{'status':'cancelled'})
        self.assertEqual(db.apply_payment_answer(booking_id,'paid','pay-test-'+booking_id[:8]),'refund_required')
        return booking_id

    def consume(self,envelope):
        """What run_consumer does with a parsed message: await the handler."""
        return asyncio.run(payment_updates.handle(envelope))

    def results(self,message_type,result):
        return self.counter('booking_payment_messages_total',type=message_type,result=result)

    def test_payment_result_paid_confirms_and_records_the_message(self):                       # M1 (row 5)
        booking_id=self.pending_booking()
        envelope=payment_envelope('payment.result',booking_id,'paid')
        before=self.results('payment.result','confirmed')
        self.consume(envelope)
        self.assertEqual(self.booking_status(booking_id),'confirmed')
        self.assertEqual(self.query('SELECT payment_id FROM bookings WHERE booking_id=%s',(booking_id,))[0][0],
                         'pay-test-'+booking_id[:8])
        self.assertEqual(self.query("SELECT status FROM seats WHERE seat_id='T01'")[0][0],'booked')
        self.assertIn(envelope.message_id,self.processed())
        [confirmed]=self.messages('booking.confirmed')
        self.assertEqual(confirmed['payload']['booking_id'],booking_id)
        self.assertEqual(self.results('payment.result','confirmed'),before+1)

    def test_payment_result_after_cancellation_requests_a_refund(self):                        # M2 (row 6)
        booking_id=self.pending_booking()
        db.apply_event_changed(str(uuid.uuid4()),self.big_event(version=2)|{'status':'cancelled'})
        sequence=self.sequence('event-test-many')
        self.consume(payment_envelope('payment.result',booking_id,'paid'))
        self.assertEqual(self.booking_status(booking_id),'refund_required')
        self.assertIn('T01',self.free_seats('event-test-many'))
        self.assertEqual(self.sequence('event-test-many'),sequence+1)
        [cancelled]=self.messages('booking.cancelled')
        [refund]=self.messages('refund.requested')
        self.assertEqual(cancelled['payload']['reason'],'event_cancelled')
        self.assertEqual(refund['payload'],{'booking_id':booking_id,'payment_id':'pay-test-'+booking_id[:8],
                                            'customer_email':'buyer-test@example.test'})

    def test_payment_result_failed_or_refunded_releases(self):                                 # M3 (row 7)
        for n,status in enumerate(('failed','refunded'),start=1):
            with self.subTest(status):
                booking_id=self.pending_booking(seat=f'T0{n}',customer=f'buyer-test-{n}')
                self.consume(payment_envelope('payment.result',booking_id,status))
                self.assertEqual(self.booking_status(booking_id),'released')
                self.assertIn(f'T0{n}',self.free_seats('event-test-many'))
        self.assertEqual({e['payload']['reason'] for e in self.messages('booking.cancelled')},{'payment_failed'})
        self.assertEqual(self.messages('refund.requested'),[])

    def test_repeated_payment_message_id_does_nothing(self):                                   # M4 (D2)
        booking_id=self.pending_booking()
        envelope=payment_envelope('payment.result',booking_id,'paid')
        self.consume(envelope)
        rows,processed=self.outbox_count(),self.processed()
        before=self.results('payment.result','duplicate')
        self.consume(envelope)                                       # redelivered: same message_id
        self.assertEqual(db.apply_payment_answer(booking_id,'failed','pay-test-x',envelope.message_id),'duplicate')
        self.assertEqual(self.booking_status(booking_id),'confirmed')
        self.assertEqual((self.outbox_count(),self.processed()),(rows,processed))
        self.assertEqual(self.results('payment.result','duplicate'),before+1)

    def test_message_after_the_http_answer_is_a_recorded_noop(self):                           # M5 (second arrival)
        booking_id=self.pending_booking()
        self.assertEqual(db.apply_payment_answer(booking_id,'paid','pay-test-1'),'confirmed')   # HTTP first
        rows=self.outbox_count()
        envelope=payment_envelope('payment.result',booking_id,'paid',payment_id='pay-test-1')
        before=self.results('payment.result','noop')
        self.consume(envelope)
        self.assertEqual(self.booking_status(booking_id),'confirmed')
        self.assertEqual(self.outbox_count(),rows)
        self.assertIn(envelope.message_id,self.processed())          # recorded although nothing changed
        self.assertEqual(self.results('payment.result','noop'),before+1)
        # And the other way round: message first, then the HTTP reply does nothing.
        other=self.pending_booking(seat='T02',customer='buyer-test-2')
        self.consume(payment_envelope('payment.result',other,'failed'))
        self.assertEqual(db.apply_payment_answer(other,'failed','pay-test-'+other[:8]),'noop')
        self.assertEqual(self.booking_status(other),'released')

    def waiting_on_event_lock(self):
        """Sessions blocked on 'SELECT 1 FROM events ... FOR UPDATE' (the event row lock)."""
        return self.query("""SELECT count(*) FROM pg_stat_activity
                             WHERE wait_event_type = 'Lock'
                               AND strpos(query, 'SELECT 1 FROM events WHERE event_id') = 1
                               AND strpos(query, 'FOR UPDATE') > 0""")[0][0]

    def test_http_reply_and_message_race_has_one_winner(self):                                 # M6 (race)
        for n,first in enumerate(('http','message'),start=1):
            with self.subTest(first=first):
                booking_id=self.pending_booking(seat=f'T0{n}',customer=f'buyer-test-{n}')
                payment_id='pay-test-'+booking_id[:8]
                message_id=str(uuid.uuid4())
                callers={'http':lambda:db.apply_payment_answer(booking_id,'paid',payment_id),
                         'message':lambda:db.apply_payment_answer(booking_id,'paid',payment_id,message_id)}
                order=[first]+[name for name in callers if name!=first]
                confirmed=len(self.messages('booking.confirmed'))
                # Hold the event row lock, so both callers queue behind it at the same point.
                holder=self.connect()
                try:
                    with holder.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM events WHERE event_id='event-test-many' FOR UPDATE")
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures={}
                        for waiting,name in enumerate(order,start=1):
                            futures[name]=executor.submit(callers[name])
                            self.wait_until(lambda w=waiting:self.waiting_on_event_lock()==w)
                        holder.commit()                      # release: both run, one after the other
                        results={name:future.result(timeout=10) for name,future in futures.items()}
                finally:
                    holder.close()
                self.assertEqual(sorted(results.values()),['confirmed','noop'])
                self.assertEqual(self.booking_status(booking_id),'confirmed')
                self.assertEqual(len(self.messages('booking.confirmed')),confirmed+1)    # exactly one
                self.assertIn(message_id,self.processed())   # recorded whether it won or not

    def test_unknown_booking_is_recorded_counted_and_acknowledged(self):                      # M7 (D4)
        for message_type in ('payment.result','refund.completed','refund.failed'):
            with self.subTest(message_type):
                envelope=payment_envelope(message_type,'booking-test-unknown','paid')
                rows=self.outbox_count()
                before=self.results(message_type,'unknown_booking')
                self.consume(envelope)                       # returns: run_consumer acks it
                self.assertIn(envelope.message_id,self.processed())
                self.assertEqual(self.outbox_count(),rows)
                self.assertEqual(self.results(message_type,'unknown_booking'),before+1)
                self.consume(envelope)                       # a redelivery is a duplicate, not counted again
                self.assertEqual(self.results(message_type,'unknown_booking'),before+1)

    def test_malformed_payment_messages_raise_and_record_nothing(self):                       # M8 (D4)
        from messaging import Envelope
        booking_id=self.pending_booking()
        good=payment_envelope('payment.result',booking_id,'paid').payload
        bad=[('payment.result',{k:v for k,v in good.items() if k!='booking_id'}),
             ('payment.result',good|{'booking_id':''}),
             ('payment.result',good|{'booking_id':'b'*65}),
             ('payment.result',good|{'status':'pending'}),
             ('payment.result',{k:v for k,v in good.items() if k!='status'}),
             ('payment.result',good|{'payment_id':' '}),
             ('payment.result',good|{'payment_id':7}),
             ('payment.result',['not','an','object']),
             ('refund.completed',{k:v for k,v in good.items() if k!='booking_id'}),
             ('refund.failed',good|{'booking_id':None})]
        processed=self.processed()
        for message_type,payload in bad:
            with self.subTest(message_type=message_type,payload=payload):
                envelope=Envelope.new(message_type,payload,producer='payment')
                with self.assertRaises(ValueError):          # run_consumer retries, then dead-letters
                    self.consume(envelope)
        self.assertEqual(self.processed(),processed)
        self.assertEqual(self.booking_status(booking_id),'payment_pending')

    def test_refund_completed_from_refund_required_or_refund_pending(self):                   # M9 (row 10)
        pending=self.pending_booking(seat='T02',customer='buyer-test-2')    # before the event is cancelled
        required=self.refund_required_booking()
        self.assertEqual(db.apply_payment_answer(pending,'paid','pay-test-'+pending[:8]),'refund_required')
        self.consume(payment_envelope('refund.failed',pending,'paid'))
        self.assertEqual(self.booking_status(pending),'refund_pending')
        rows,seats=self.outbox_count(),self.seat_rows('event-test-many')
        for booking_id in (required,pending):
            envelope=payment_envelope('refund.completed',booking_id,'refunded')
            self.consume(envelope)
            self.assertEqual(self.booking_status(booking_id),'refunded')
            self.assertIn(envelope.message_id,self.processed())
        self.assertEqual(self.outbox_count(),rows)          # row 10 publishes nothing
        self.assertEqual(self.seat_rows('event-test-many'),seats)   # and changes no seats

    def test_refund_failed_only_from_refund_required_then_completed(self):                    # M10 (row 11)
        booking_id=self.refund_required_booking()
        before=self.results('refund.failed','refund_pending')
        self.consume(payment_envelope('refund.failed',booking_id,'paid'))
        self.assertEqual(self.booking_status(booking_id),'refund_pending')
        self.assertEqual(self.results('refund.failed','refund_pending'),before+1)
        second=payment_envelope('refund.failed',booking_id,'paid')    # a new message_id
        self.consume(second)
        self.assertEqual(self.booking_status(booking_id),'refund_pending')   # noop
        self.assertIn(second.message_id,self.processed())
        self.consume(payment_envelope('refund.completed',booking_id,'refunded'))
        self.assertEqual(self.booking_status(booking_id),'refunded')
        self.consume(payment_envelope('refund.failed',booking_id,'paid'))    # never moves back
        self.assertEqual(self.booking_status(booking_id),'refunded')

    def test_refund_messages_on_other_states_are_recorded_noops(self):                        # M11
        confirmed=self.pending_booking()
        db.apply_payment_answer(confirmed,'paid','pay-test-1')
        released=self.pending_booking(seat='T02',customer='buyer-test-2')
        db.apply_payment_answer(released,'failed','pay-test-2')
        pending=self.pending_booking(seat='T03',customer='buyer-test-3')
        held=db.hold_seat('event-test-many','T04','buyer-test-4')[1]
        expected={confirmed:'confirmed',released:'released',pending:'payment_pending',held:'held'}
        for message_type in ('refund.completed','refund.failed'):
            for booking_id,status in expected.items():
                with self.subTest(message_type=message_type,status=status):
                    envelope=payment_envelope(message_type,booking_id,'refunded')
                    self.assertEqual(db.apply_refund_message(envelope.message_id,booking_id,message_type),'noop')
                    self.assertEqual(self.booking_status(booking_id),status)
                    self.assertIn(envelope.message_id,self.processed())

    def test_dispatch_by_type_not_status(self):                                                # M12 (section 3)
        booking_id=self.refund_required_booking()
        # refund.failed carries status "paid": it must NOT be read as a payment result.
        self.consume(payment_envelope('refund.failed',booking_id,'paid'))
        self.assertEqual(self.booking_status(booking_id),'refund_pending')
        processed=self.processed()
        other=payment_envelope('booking.confirmed',booking_id,'paid')      # not bound to this queue
        self.consume(other)                                                # logged and acknowledged
        self.assertEqual(self.processed(),processed)
        self.assertEqual(self.booking_status(booking_id),'refund_pending')

    def test_database_failure_rolls_back_the_message_id_too(self):                            # M13
        booking_id=self.pending_booking()
        self.query("""CREATE FUNCTION fail_outbox_insert() RETURNS trigger LANGUAGE plpgsql AS
                      $$ BEGIN RAISE EXCEPTION 'simulated outbox failure'; END $$""")
        self.query('CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION fail_outbox_insert()')
        envelope=payment_envelope('payment.result',booking_id,'paid')
        processed=self.processed()
        with self.assertRaises(psycopg2.Error):                 # run_consumer retries it
            self.consume(envelope)
        self.assertEqual(self.processed(),processed)
        self.assertEqual(self.booking_status(booking_id),'payment_pending')
        self.query('DROP TRIGGER fail_outbox ON outbox')
        self.consume(envelope)                                  # the retry then applies cleanly
        self.assertEqual(self.booking_status(booking_id),'confirmed')
        self.assertIn(envelope.message_id,self.processed())

    def test_http_path_records_no_message_id_and_still_raises_on_unknown_booking(self):       # M14
        booking_id=self.pending_booking()
        processed=self.processed()
        self.assertEqual(db.apply_payment_answer(booking_id,'paid','pay-test-1'),'confirmed')
        self.assertEqual(db.apply_payment_answer(booking_id,'paid','pay-test-1'),'noop')
        self.assertEqual(self.processed(),processed)
        with self.assertRaises(LookupError):
            db.apply_payment_answer('booking-test-unknown','paid','pay-test-1')
        self.assertEqual(self.processed(),processed)

    def test_listener_consumes_both_queues_on_one_connection(self):                           # M15 (D1)
        calls,opened=[],[]

        class FakeConnection:
            async def __aenter__(self):
                return self
            async def __aexit__(self,*exc):
                return False
            async def channel(self,**kwargs):
                opened.append(object())
                return opened[-1]

        async def fake_connect(url):
            return FakeConnection()

        async def fake_run_consumer(channel,queue,handler,on_outcome=None):
            calls.append((channel,queue,handler,on_outcome))

        async def scenario():
            task=asyncio.create_task(events.run('amqp://test'))
            while len(calls)<2:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch('events._connect',fake_connect),patch('messaging.run_consumer',fake_run_consumer):
            asyncio.run(asyncio.wait_for(scenario(),5))
        self.assertEqual([(queue,handler) for _,queue,handler,_ in calls],
                         [('booking.event-changes',events.handle),('booking.payment-updates',payment_updates.handle)])
        self.assertEqual(len({id(channel) for channel,*_ in calls}),2)        # one channel per queue
        self.assertTrue(all(hook is events.track_outcome for *_,hook in calls))
        from messaging import QUEUES
        bound={q.name:q.bindings for q in QUEUES}['booking.payment-updates']
        self.assertEqual(set(bound),{'payment.result','refund.completed','refund.failed'})

    # --- Hold expiry (row 4) and event cancelled (row 13) (step 7a; contract sections 4, 4a, 11, 12) ---

    def expire(self,*booking_ids):
        """Make holds expire now (database clock), as if HOLD_MINUTES had passed."""
        self.query("UPDATE bookings SET held_until=now()-interval '1 second' WHERE booking_id = ANY(%s)",(list(booking_ids),))

    def sweep(self):
        return asyncio.run(expiry.sweep_once())

    def released_count(self,reason):
        return self.counter('booking_holds_released_total',reason=reason)

    def seat(self,event_id,seat_id):
        """(status, booking_ref, customer_id, held_until) of one seat."""
        return self.query('SELECT status,booking_ref,customer_id,held_until FROM seats WHERE event_id=%s AND seat_id=%s',
                          (event_id,seat_id))[0]

    def cancel_big_event(self,version=2):
        return db.apply_event_changed(str(uuid.uuid4()),self.big_event(version=version)|{'status':'cancelled'})

    def cancelled_payloads(self):
        return sorted((e['payload'] for e in self.messages('booking.cancelled')),key=lambda p:p['booking_id'])

    def expected_cancelled(self,reason,bookings,title=None):
        """booking.cancelled payloads for [(booking_id, customer_id)] with the fallback email and name."""
        found=[]
        for booking_id,customer in bookings:
            payload={'booking_id':booking_id,'reason':reason,'customer_email':f'{customer}@example.test',
                     'customer_name':customer}
            if title is not None:
                payload['event_title']=title
            found.append(payload)
        return sorted(found,key=lambda p:p['booking_id'])

    def test_sweep_releases_expired_holds_with_one_snapshot_per_event(self):                   # E1, E6 (row 4)
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        first=db.hold_seat('event-test-many','T01','buyer-test-1')[1]
        second=db.hold_seat('event-test-many','T02','buyer-test-2')[1]
        kept=db.hold_seat('event-test-many','T03','buyer-test-3')[1]
        self.expire(first,second)
        sequence=self.sequence('event-test-many');before=self.released_count('hold_expired')
        self.assertEqual(self.sweep(),(2,1,0))                       # 2 released, ONE snapshot, 0 failed
        self.assertEqual([self.booking_status(b) for b in (first,second,kept)],['released','released','held'])
        for seat in ('T01','T02'):
            self.assertEqual(self.seat('event-test-many',seat),('free',None,None,None))
        self.assertEqual(self.seat('event-test-many','T03')[:2],('held',kept))
        self.assertEqual(self.sequence('event-test-many'),sequence+1)
        latest=self.snapshots('event-test-many')[-1]
        self.assertEqual(latest['payload']['available_seat_ids'],self.free_seats('event-test-many'))
        self.assertIn('T01',latest['payload']['available_seat_ids'])
        self.assertNotIn('T03',latest['payload']['available_seat_ids'])
        self.assertEqual(self.cancelled_payloads(),self.expected_cancelled(
            'hold_expired',[(first,'buyer-test-1'),(second,'buyer-test-2')],self.big_event()['title']))
        for envelope in self.messages('booking.cancelled'):
            self.assert_booking_envelope(envelope,'booking.cancelled')
        self.assertEqual(self.released_count('hold_expired'),before+2)
        # A second sweep finds nothing and writes nothing.
        count=self.outbox_count()
        self.assertEqual(self.sweep(),(0,0,0))
        self.assertEqual(self.outbox_count(),count)

    def test_sweep_ignores_unexpired_payment_pending_and_confirmed(self):                       # E2
        pending=self.pending_booking('T01','buyer-test-1')
        confirmed=self.pending_booking('T02','buyer-test-2')
        self.assertEqual(db.apply_payment_answer(confirmed,'paid','pay-test-2'),'confirmed')
        fresh=db.hold_seat('event-test-many','T03','buyer-test-3')[1]
        self.expire(pending,confirmed)          # past held_until, but no longer 'held'
        count=self.outbox_count()
        self.assertEqual(self.sweep(),(0,0,0))
        self.assertEqual([self.booking_status(b) for b in (pending,confirmed,fresh)],['payment_pending','confirmed','held'])
        self.assertEqual([self.seat('event-test-many',s)[0] for s in ('T01','T02','T03')],['held','booked','held'])
        self.assertEqual(self.outbox_count(),count)

    def test_sweep_on_event_without_record_sends_no_snapshot_and_no_title(self):                # E3 (Q2)
        self.seed_seat()        # race-event has no event record
        booking_id=db.hold_seat('race-event','A1','buyer-test')[1]
        self.expire(booking_id)
        self.assertEqual(self.sweep(),(1,0,0))
        self.assertEqual(self.booking_status(booking_id),'released')
        self.assertEqual(self.seat('race-event','A1'),('free',None,None,None))
        self.assertEqual(self.cancelled_payloads(),self.expected_cancelled('hold_expired',[(booking_id,'buyer-test')]))
        self.assertEqual(self.messages('seat.availability-changed'),[])
        self.assertEqual(self.query("SELECT count(*) FROM events WHERE event_id='race-event'")[0][0],0)

    def test_sweep_cuts_the_event_title_to_200(self):                                          # E4 (section 1)
        db.apply_event_changed(str(uuid.uuid4()),self.big_event()|{'title':'T'*250})
        booking_id=db.hold_seat('event-test-many','T01','buyer-test')[1]
        self.expire(booking_id)
        self.sweep()
        [payload]=self.cancelled_payloads()
        self.assertEqual(payload['event_title'],'T'*200)

    def test_one_events_failure_does_not_stop_the_others(self):                                # E5 (D3)
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        db.apply_event_changed(str(uuid.uuid4()),self.big_event(event_id='event-test-other'))
        bad=db.hold_seat('event-test-many','T01','buyer-test-1')[1]
        good=db.hold_seat('event-test-other','T01','buyer-test-2')[1]
        self.expire(bad,good)
        # Swept in event_id order, so the failing event comes first.
        self.query("""CREATE FUNCTION fail_release() RETURNS trigger LANGUAGE plpgsql AS
                      $$ BEGIN IF NEW.event_id = 'event-test-many' THEN RAISE EXCEPTION 'simulated failure'; END IF;
                         RETURN NEW; END $$""")
        self.query('CREATE TRIGGER fail_release BEFORE UPDATE ON bookings FOR EACH ROW EXECUTE FUNCTION fail_release()')
        errors=self.counter('booking_expiry_sweep_errors_total');count=self.outbox_count()
        with self.assertLogs('booking.expiry','ERROR'):
            self.assertEqual(self.sweep(),(1,1,1))
        self.assertEqual([self.booking_status(b) for b in (bad,good)],['held','released'])
        self.assertEqual(self.seat('event-test-many','T01')[:2],('held',bad))      # rolled back completely
        self.assertEqual(self.outbox_count(),count+2)                              # good: snapshot + booking.cancelled
        self.assertEqual([p['booking_id'] for p in self.cancelled_payloads()],[good])
        self.assertEqual(self.counter('booking_expiry_sweep_errors_total'),errors+1)

    def test_sweep_waits_for_a_checkout_in_flight_and_leaves_it_payment_pending(self):         # E7 (Q3)
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        booking_id=db.hold_seat('event-test-many','T01','buyer-test')[1]
        self.query("UPDATE bookings SET held_until=now()+interval '0.3 second' WHERE booking_id=%s",(booking_id,))
        # A checkout transaction, statement by statement as db.start_checkout runs them:
        # it locks the booking row while the hold is still valid ...
        checkout=self.connect()
        self.addCleanup(checkout.close)
        cur=checkout.cursor()
        cur.execute("SELECT b.status, b.held_until > now() FROM bookings b WHERE b.booking_id=%s FOR UPDATE OF b",(booking_id,))
        self.assertEqual(cur.fetchone(),('held',True))
        time.sleep(0.4)                         # ... and the hold expires before it commits.
        with ThreadPoolExecutor(max_workers=1) as executor:
            sweep=executor.submit(self.sweep)
            # The sweeper has the event lock and now waits for the booking row.
            self.wait_until(lambda:self.query('SELECT count(*) FROM pg_locks WHERE NOT granted')[0][0]>0)
            cur.execute("""UPDATE bookings SET status='payment_pending', checkout_started_at=now(), updated_at=now()
                           WHERE booking_id=%s AND status='held' AND held_until > now()""",(booking_id,))
            self.assertEqual(cur.rowcount,1)    # now() is the checkout's start time: still valid
            checkout.commit()
            self.assertEqual(sweep.result(timeout=10),(0,0,0))      # its conditional UPDATE re-checked 'held'
        self.assertEqual(self.booking_status(booking_id),'payment_pending')
        self.assertEqual(self.seat('event-test-many','T01')[:2],('held',booking_id))
        self.assertEqual(self.messages('booking.cancelled'),[])
        # The other order: once swept, a checkout is refused.
        other=db.hold_seat('event-test-many','T02','buyer-test-2')[1]
        self.expire(other)
        self.sweep()
        self.assertEqual(db.start_checkout(other,'buyer-test-2',True),'not_checkoutable')
        self.assertEqual(self.booking_status(other),'released')

    def test_checkouts_and_sweeps_racing_never_both_win(self):                                # E7 (stress)
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        bookings=[db.hold_seat('event-test-many',f'T{n:02d}',f'buyer-test-{n}')[1] for n in range(1,11)]
        self.query("UPDATE bookings SET held_until=now()+interval '0.15 second'")
        checkouts=[lambda n=n,b=b:db.start_checkout(b,f'buyer-test-{n}',True) for n,b in zip(range(1,11),bookings)]
        sweeps=[lambda d=d:(time.sleep(d),self.sweep())[1] for d in (0.1,0.15,0.2,0.25)]
        with ThreadPoolExecutor(max_workers=14) as executor:
            futures=[executor.submit(lambda j=j,d=d:(time.sleep(d),j())[1]) for j,d in
                     zip(checkouts,[0.02*n for n in range(10)])]
            for job in sweeps:
                futures.append(executor.submit(job))
            results=[future.result(timeout=30) for future in futures]
        self.sweep()                            # anything still held has expired by now
        cancelled={p['booking_id'] for p in self.cancelled_payloads()}
        for n,(booking_id,result) in enumerate(zip(bookings,results[:10]),start=1):
            status=self.booking_status(booking_id)
            seat=self.seat('event-test-many',f'T{n:02d}')
            if result=='started':
                self.assertEqual((status,seat[:2]),('payment_pending',('held',booking_id)))
                self.assertNotIn(booking_id,cancelled)
            else:
                self.assertEqual(result,'not_checkoutable')
                self.assertEqual((status,seat[0]),('released','free'))
                self.assertIn(booking_id,cancelled)
        self.assertEqual(len(self.messages('booking.cancelled')),len(cancelled))   # one per released booking

    def test_sweep_and_hold_retries_and_new_holds(self):                                      # E8 (Q3)
        self.apply('created')
        mine=db.hold_seat('event-demo-001','A1','buyer-test')[1]
        self.expire(mine)
        # Expired but not swept yet: a retry reports the booking as it is (section 5).
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test'),('already_yours',mine,'held'))
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test-2'),('taken',None,None))
        self.sweep()
        outcome,theirs,_=db.hold_seat('event-demo-001','A1','buyer-test-2')
        self.assertEqual(outcome,'held')
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer-test'),('taken',None,None))
        # Racing: one sweep against ten new buyers on an expired seat.
        self.expire(theirs)
        jobs=[self.sweep]+[lambda n=n:db.hold_seat('event-demo-001','A1',f'buyer-test-r{n}') for n in range(10)]
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            results=[f.result(timeout=30) for f in [executor.submit(job) for job in jobs]]
        winners=[r[1] for r in results[1:] if r[0]=='held']
        self.assertLessEqual(len(winners),1)    # holds before the sweep got 'taken'; one after it may win
        self.assertEqual(self.booking_status(theirs),'released')
        held=self.query("SELECT booking_id FROM bookings WHERE seat_id='A1' AND event_id='event-demo-001' AND status='held'")
        self.assertEqual([b for (b,) in held],winners)
        status,ref,_,_=self.seat('event-demo-001','A1')
        self.assertEqual((status,ref),('held',winners[0]) if winners else ('free',None))
        found=self.snapshots()
        self.assertEqual([e['payload']['sequence'] for e in found],list(range(1,len(found)+1)))
        self.assertEqual(found[-1]['payload']['available_seat_ids'],self.free_seats('event-demo-001'))

    def test_sweeps_holds_answers_and_event_changed_do_not_deadlock(self):                    # E9 (Q3)
        pending=[self.pending_booking(f'T{n:02d}',f'buyer-test-{n}') for n in range(1,6)]
        expired=[db.hold_seat('event-test-many',f'T{n:02d}',f'buyer-test-{n}')[1] for n in range(6,11)]
        self.expire(*expired)
        answers=[lambda b=b,s=s:db.apply_payment_answer(b,s,'pay-test-'+b[:8])
                 for b,s in zip(pending,['paid','failed','paid','refunded','paid'])]
        holds=[lambda n=n:db.hold_seat('event-test-many',f'T{n:02d}',f'buyer-test-{n}') for n in range(11,21)]
        changes=[lambda v=v:db.apply_event_changed(str(uuid.uuid4()),self.big_event(version=v,extra=[f'X{v}'])) for v in range(2,5)]
        sweeps=[self.sweep]*3
        jobs=holds[:5]+sweeps[:1]+answers[:2]+changes+sweeps[1:]+answers[2:]+holds[5:]
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            for future in [executor.submit(job) for job in jobs]:
                future.result(timeout=30)       # a deadlock error would raise here
        self.assertEqual({self.booking_status(b) for b in expired},{'released'})
        self.assertEqual([self.booking_status(b) for b in pending],['confirmed','released','confirmed','released','confirmed'])
        found=self.snapshots('event-test-many')
        self.assertEqual([e['payload']['sequence'] for e in found],list(range(1,len(found)+1)))
        self.assertEqual(found[-1]['payload']['available_seat_ids'],self.free_seats('event-test-many'))

    def test_lifespan_runs_the_sweeper_survives_an_error_and_stops(self):                     # E10 (D1)
        self.apply('created')
        client=self.client(sweeper=True)
        first=self.hold(client)
        self.expire(first)
        self.wait_for_status(first,'released')
        # One failing sweep is logged and counted; the loop keeps going.
        errors=self.counter('booking_expiry_sweep_errors_total')
        real=db.expired_hold_events
        calls=[]
        def fail_once():
            calls.append(1)
            if len(calls)==1:
                raise psycopg2.OperationalError('simulated database outage')
            return real()
        with patch('db.expired_hold_events',fail_once),self.assertLogs('booking.expiry','ERROR'):
            self.wait_until(lambda:len(calls)>=2)
        self.assertEqual(self.counter('booking_expiry_sweep_errors_total'),errors+1)
        second=self.hold(client,seat='A2')
        self.expire(second)
        self.wait_for_status(second,'released')
        # After shutdown the sweeper has stopped.
        client.__exit__(None,None,None)
        third=db.hold_seat('event-demo-001','A1','buyer-test-3')[1]
        self.expire(third)
        time.sleep(0.3)
        self.assertEqual(self.booking_status(third),'held')

    def test_cancel_releases_only_held_bookings_with_one_snapshot(self):                      # R1 (row 13)
        pending=self.pending_booking('T01','buyer-test-1')
        confirmed=self.pending_booking('T02','buyer-test-2')
        self.assertEqual(db.apply_payment_answer(confirmed,'paid','pay-test-2'),'confirmed')
        first=db.hold_seat('event-test-many','T03','buyer-test-3')[1]
        second=db.hold_seat('event-test-many','T04','buyer-test-4')[1]
        self.expire(second)                     # expired but not swept: released by row 13 too
        sequence=self.sequence('event-test-many');before=self.released_count('event_cancelled')
        message_id=str(uuid.uuid4())
        self.assertEqual(db.apply_event_changed(message_id,self.big_event(version=2)|{'status':'cancelled'}),'applied')
        self.assertEqual([self.booking_status(b) for b in (pending,confirmed,first,second)],
                         ['payment_pending','confirmed','released','released'])
        self.assertEqual([self.seat('event-test-many',s)[0] for s in ('T01','T02','T03','T04')],
                         ['held','booked','free','free'])
        self.assertEqual(self.sequence('event-test-many'),sequence+1)             # ONE snapshot
        latest=self.snapshots('event-test-many')[-1]
        self.assertEqual(latest['payload']['available_seat_ids'],self.free_seats('event-test-many'))
        self.assertIn('T03',latest['payload']['available_seat_ids'])
        self.assertEqual(self.cancelled_payloads(),self.expected_cancelled(
            'event_cancelled',[(first,'buyer-test-3'),(second,'buyer-test-4')],self.big_event()['title']))
        self.assertIn(message_id,self.processed())
        self.assertEqual(self.released_count('event_cancelled'),before+2)
        # The payment in progress is then refunded as before (row 6).
        self.assertEqual(db.apply_payment_answer(pending,'paid','pay-test-1'),'refund_required')

    def test_cancelled_first_event_releases_existing_holds_in_its_first_snapshot(self):       # R2 (Q1b)
        self.seed_seat()        # race-event: seat A1, no event record
        booking_id=db.hold_seat('race-event','A1','buyer-test')[1]
        payload=self.big_event(event_id='race-event')|{'status':'cancelled','seats':[]}
        self.assertEqual(db.apply_event_changed(str(uuid.uuid4()),payload),'applied')
        self.assertEqual(self.booking_status(booking_id),'released')
        self.assertEqual(self.seat('race-event','A1'),('free',None,None,None))
        [snapshot]=self.snapshots('race-event')                                  # one, not two
        self.assertEqual((snapshot['payload']['sequence'],snapshot['payload']['available_seat_ids']),(1,['A1']))
        self.assertEqual(self.cancelled_payloads(),self.expected_cancelled(
            'event_cancelled',[(booking_id,'buyer-test')],payload['title']))

    def test_cancel_with_no_holds_and_a_stale_cancel_change_nothing(self):                    # R3
        self.apply('created')
        count=self.outbox_count()
        self.assertEqual(self.apply('cancelled'),'applied')                     # nothing held: no snapshot
        self.assertEqual(self.outbox_count(),count)
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        booking_id=db.hold_seat('event-test-many','T01','buyer-test')[1]
        count=self.outbox_count()
        self.assertEqual(self.cancel_big_event(version=1),'stale')              # not newer: nothing happens
        self.assertEqual(self.booking_status(booking_id),'held')
        self.assertEqual(self.outbox_count(),count)

    def test_failed_outbox_insert_rolls_back_the_cancellation(self):                         # R4
        self.apply('created')
        booking_id=db.hold_seat('event-demo-001','A1','buyer-test')[1]
        self.query("""CREATE FUNCTION fail_outbox_insert() RETURNS trigger LANGUAGE plpgsql AS
                      $$ BEGIN RAISE EXCEPTION 'simulated outbox failure'; END $$""")
        self.query('CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION fail_outbox_insert()')
        before=self.released_count('event_cancelled')
        with self.assertRaises(psycopg2.Error):
            self.apply('cancelled',message_id='msg-test-cancel')
        self.assertEqual(self.event_row()[:2],(1,'scheduled'))
        self.assertEqual(self.booking_status(booking_id),'held')
        self.assertEqual(self.seat_rows()[0][3:5],('held',booking_id))
        self.assertNotIn('msg-test-cancel',self.processed())
        self.assertEqual(self.released_count('event_cancelled'),before)

    def test_cancel_racing_checkouts_never_both_win(self):                                   # R5 (Q3)
        db.apply_event_changed(str(uuid.uuid4()),self.big_event())
        bookings=[db.hold_seat('event-test-many',f'T{n:02d}',f'buyer-test-{n}')[1] for n in range(1,11)]
        checkouts=[lambda n=n,b=b:db.start_checkout(b,f'buyer-test-{n}',True) for n,b in zip(range(1,11),bookings)]
        jobs=checkouts[:5]+[self.cancel_big_event]+checkouts[5:]
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            results=[f.result(timeout=30) for f in [executor.submit(job) for job in jobs]]
        results=results[:5]+results[6:]
        cancelled={p['booking_id'] for p in self.cancelled_payloads()}
        for n,(booking_id,result) in enumerate(zip(bookings,results),start=1):
            status=self.booking_status(booking_id)
            seat=self.seat('event-test-many',f'T{n:02d}')
            if result=='started':
                self.assertEqual((status,seat[:2]),('payment_pending',('held',booking_id)))
                self.assertNotIn(booking_id,cancelled)
                # Payment's answer after the cancellation: refunded (row 6), seat freed.
                self.assertEqual(db.apply_payment_answer(booking_id,'paid','pay-test-'+booking_id[:8]),'refund_required')
            else:
                self.assertEqual(result,'event_cancelled')
                self.assertEqual((status,seat[0]),('released','free'))
                self.assertIn(booking_id,cancelled)
        found=self.snapshots('event-test-many')
        self.assertEqual([e['payload']['sequence'] for e in found],list(range(1,len(found)+1)))
        self.assertEqual(found[-1]['payload']['available_seat_ids'],self.free_seats('event-test-many'))

    # --- Safety net (row 8) and late success (row 9) (step 7b; contract sections 4, 4a, 9, 12) ---

    def make_due(self,*booking_ids):
        """As if S (60 s) had passed since checkout (database clock)."""
        self.query("UPDATE bookings SET checkout_started_at=now()-interval '61 seconds' WHERE booking_id = ANY(%s)",
                   (list(booking_ids),))

    def net_outcome(self,outcome):
        return self.counter('booking_safety_net_calls_total',outcome=outcome)

    def late_count(self):
        return self.counter('booking_late_successes_total')

    def payment_id(self,booking_id):
        return self.query('SELECT payment_id FROM bookings WHERE booking_id=%s',(booking_id,))[0][0]

    def event_title(self,event_id='event-test-many'):
        return self.event_row(event_id)[2]

    def safety_check(self,reply=None,breaker=None,checks=1,clock=None,between=None):
        """Run SafetyNet.check_once(wait=True) `checks` times (S = 60) against a fake Payment.

        reply(body) -> httpx.Response (default: paid); it may raise (see down()).
        between(i) runs after check i. Returns the booking_ids each check started.
        self.net_calls lists the request bodies Payment received.
        """
        self.net_calls=[]
        reply=reply or (lambda body:payment_reply('paid',body))

        async def handler(request):
            body=json.loads(request.content)
            self.net_calls.append(body)
            return reply(body)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=payments.PaymentClient(http,'http://payment-test',
                                              breaker or payments.CircuitBreaker(clock=lambda:0.0))
                net=safety_net.SafetyNet(client,60,**({'clock':clock} if clock else {}))
                started=[]
                for i in range(checks):
                    started.append(await net.check_once(wait=True))
                    if between:
                        between(i)
                return started
        return asyncio.run(go())

    def released_by_safety_net(self,seat='T01',customer='buyer-test'):
        """A booking released by row 8 (Payment gave the safety net no answer)."""
        booking_id=self.pending_booking(seat,customer)
        self.make_due(booking_id)
        self.assertEqual(self.safety_check(reply=down),[[booking_id]])
        self.assertEqual(self.booking_status(booking_id),'released')
        return booking_id

    def test_safety_net_ignores_bookings_not_yet_due(self):                                       # N1 (D1)
        booking_id=self.pending_booking()
        held=self.pending_booking('T02','buyer-test-2')
        self.query("UPDATE bookings SET status='held', checkout_started_at=now()-interval '1 hour' WHERE booking_id=%s",
                   (held,))                                                       # not payment_pending: never due
        self.assertEqual(self.safety_check(),[[]])
        self.query("UPDATE bookings SET checkout_started_at=now()-interval '59 seconds' WHERE booking_id=%s",(booking_id,))
        self.assertEqual(self.safety_check(),[[]])
        self.assertEqual(self.net_calls,[])
        self.assertEqual(self.booking_status(booking_id),'payment_pending')
        self.make_due(booking_id)
        self.assertEqual(self.safety_check(),[[booking_id]])

    def test_safety_net_answer_paid_confirms_with_one_call(self):                                # N2 (row 8 -> 5)
        booking_id=self.pending_booking()
        self.make_due(booking_id)
        before=self.net_outcome('confirmed')
        self.assertEqual(self.safety_check(),[[booking_id]])
        self.assertEqual(self.net_calls,[{'booking_id':booking_id,'amount':4500,'currency':'EUR',
                                          'customer_email':'buyer-test@example.test'}])
        self.assertEqual(self.booking_status(booking_id),'confirmed')
        self.assertEqual(self.seat('event-test-many','T01')[:2],('booked',booking_id))
        self.assertEqual(len(self.messages('booking.confirmed')),1)
        self.assertEqual(self.net_outcome('confirmed'),before+1)
        self.assertEqual(self.safety_check(),[[]])                    # no longer due: no second call
        self.assertEqual(self.net_calls,[])

    def test_safety_net_answer_failed_or_refunded_releases_as_row_7(self):                       # N3 (row 8 -> 7)
        failed=self.pending_booking('T01','buyer-test-1')
        refunded=self.pending_booking('T02','buyer-test-2')
        self.make_due(failed,refunded)
        statuses={failed:'failed',refunded:'refunded'}
        before=self.net_outcome('released')
        self.safety_check(reply=lambda body:payment_reply(statuses[body['booking_id']],body))
        self.assertEqual([self.booking_status(b) for b in (failed,refunded)],['released','released'])
        self.assertEqual(self.cancelled_payloads(),self.expected_cancelled(
            'payment_failed',[(failed,'buyer-test-1'),(refunded,'buyer-test-2')],title=self.event_title()))
        self.assertEqual(self.messages('refund.requested'),[])
        self.assertEqual(self.net_outcome('released'),before+2)

    def test_safety_net_no_answer_releases_row_8(self):                                          # N4 (row 8)
        title='Test event with a very long title '+'x'*300
        db.apply_event_changed(str(uuid.uuid4()),self.big_event()|{'title':title})
        booking_id=self.pending_booking()
        self.make_due(booking_id)
        sequence=self.sequence('event-test-many')
        before=(self.net_outcome('payments_unavailable'),self.counter('booking_payment_attempts_total',outcome='error'))
        self.assertEqual(self.safety_check(reply=down),[[booking_id]])
        self.assertEqual(len(self.net_calls),1)                       # ONE call, no retries
        self.assertEqual(self.booking_status(booking_id),'released')
        self.assertIsNone(self.payment_id(booking_id))
        self.assertEqual(self.seat('event-test-many','T01'),('free',None,None,None))
        self.assertEqual(self.sequence('event-test-many'),sequence+1)                 # ONE snapshot
        snapshot=self.snapshots('event-test-many')[-1]['payload']
        self.assertEqual((snapshot['sequence'],snapshot['available_seat_ids']),(sequence+1,self.free_seats('event-test-many')))
        self.assertIn('T01',snapshot['available_seat_ids'])
        self.assertEqual(self.cancelled_payloads(),self.expected_cancelled(
            'payments_unavailable',[(booking_id,'buyer-test')],title=title[:200]))
        self.assert_booking_envelope(self.messages('booking.cancelled')[0],'booking.cancelled')
        self.assertEqual((self.net_outcome('payments_unavailable'),
                          self.counter('booking_payment_attempts_total',outcome='error')),(before[0]+1,before[1]+1))
        self.assertEqual(self.safety_check(reply=down),[[]])          # released: never called again

    def test_safety_net_breaker_open_releases_without_a_call(self):                              # N5 (D2)
        breaker=payments.CircuitBreaker(clock=lambda:0.0)
        for _ in range(payments.FAILURE_THRESHOLD):
            breaker.try_acquire();breaker.record_failure()
        self.assertEqual(breaker.state,payments.OPEN)
        booking_id=self.pending_booking()
        self.make_due(booking_id)
        short=self.counter('booking_payment_attempts_total',outcome='short_circuited')
        self.safety_check(breaker=breaker)
        self.assertEqual(self.net_calls,[])
        self.assertEqual(self.booking_status(booking_id),'released')
        self.assertEqual(self.cancelled_payloads()[0]['reason'],'payments_unavailable')
        self.assertEqual(self.counter('booking_payment_attempts_total',outcome='short_circuited'),short+1)

    def test_safety_net_409_is_no_answer_and_releases(self):                                     # N6 (I3)
        booking_id=self.pending_booking()
        self.make_due(booking_id)
        breaker=payments.CircuitBreaker(clock=lambda:0.0)
        self.safety_check(reply=lambda body:httpx.Response(409,json={'detail':'x'}),breaker=breaker)
        self.assertEqual(self.booking_status(booking_id),'released')
        self.assertEqual(self.cancelled_payloads()[0]['reason'],'payments_unavailable')
        self.assertEqual((breaker.state,breaker.failures),(payments.CLOSED,0))     # counted neither way

    def test_safety_net_skips_a_booking_whose_call_is_in_flight(self):                           # N7 (D2)
        booking_id=self.pending_booking()
        self.make_due(booking_id)
        skipped=self.counter('booking_safety_net_skipped_total')
        calls=[]

        async def go():
            gate=asyncio.Event()

            async def handler(request):
                body=json.loads(request.content)
                calls.append(body)
                await gate.wait()
                return payment_reply('paid',body)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                net=safety_net.SafetyNet(payments.PaymentClient(http,'http://payment-test',
                                                                payments.CircuitBreaker(clock=lambda:0.0)),60)
                first=await net.check_once()
                while not calls:
                    await asyncio.sleep(0.01)
                second=await net.check_once()
                third=await net.check_once()
                during=set(net.in_flight)
                gate.set()
                await asyncio.gather(*list(net._tasks))
                return first,second,third,during,set(net.in_flight)
        first,second,third,during,after=asyncio.run(go())
        self.assertEqual((first,second,third),([booking_id],[],[]))
        self.assertEqual(len(calls),1)                                # never a second call
        self.assertEqual((during,after),({booking_id},set()))
        self.assertEqual(self.counter('booking_safety_net_skipped_total'),skipped+2)
        self.assertEqual(self.booking_status(booking_id),'confirmed')

    def test_payment_result_during_the_safety_net_call_wins_and_the_call_does_nothing(self):     # N8 (Q3)
        no_answer=self.pending_booking('T01','buyer-test-1')
        answered=self.pending_booking('T02','buyer-test-2')
        self.make_due(no_answer,answered)
        sequence=self.sequence('event-test-many')
        noop=self.net_outcome('noop')

        def message_wins(body):
            # payment.result is applied while the safety-net call is still waiting.
            self.assertEqual(db.apply_payment_answer(body['booking_id'],'paid','pay-test-'+body['booking_id'][:8],
                                                     str(uuid.uuid4())),'confirmed')
            if body['booking_id']==no_answer:
                raise httpx.ConnectError('Payment is down')         # then: no answer -> row 8 finds nothing
            return payment_reply('paid',body)                       # then: the answer -> rows 5-7 find nothing
        self.safety_check(reply=message_wins)
        self.assertEqual([self.booking_status(b) for b in (no_answer,answered)],['confirmed','confirmed'])
        self.assertEqual(self.seat('event-test-many','T01')[:2],('booked',no_answer))
        self.assertEqual(self.messages('booking.cancelled'),[])
        self.assertEqual(len(self.messages('booking.confirmed')),2)
        self.assertEqual(self.sequence('event-test-many'),sequence)    # no snapshot
        self.assertEqual(self.net_outcome('noop'),noop+2)

    def test_late_paid_message_after_row_8_requests_a_refund(self):                              # N9 (row 9)
        booking_id=self.released_by_safety_net()
        rows,sequence=self.outbox_count(),self.sequence('event-test-many')
        seat=self.seat('event-test-many','T01')
        late,result=self.late_count(),self.results('payment.result','late_success')
        envelope=payment_envelope('payment.result',booking_id,'paid',payment_id='pay-test-late')
        with self.assertLogs('booking.payment_updates','WARNING'):
            self.consume(envelope)
        self.assertEqual((self.booking_status(booking_id),self.payment_id(booking_id)),('refund_required','pay-test-late'))
        [refund]=self.messages('refund.requested')
        self.assertEqual(refund['payload'],{'booking_id':booking_id,'payment_id':'pay-test-late',
                                            'customer_email':'buyer-test@example.test'})
        self.assert_booking_envelope(refund,'refund.requested')
        self.assertEqual(self.outbox_count(),rows+1)                  # only refund.requested
        self.assertEqual(self.sequence('event-test-many'),sequence)   # seat untouched: no snapshot
        self.assertEqual(self.seat('event-test-many','T01'),seat)
        self.assertEqual(len(self.messages('booking.cancelled')),1)   # no second cancellation
        self.assertIn(envelope.message_id,self.processed())
        self.assertEqual((self.late_count(),self.results('payment.result','late_success')),(late+1,result+1))

    def test_duplicate_late_paid_http_and_message_gives_one_refund(self):                       # N10 (Q4)
        first=self.released_by_safety_net('T01','buyer-test-a')
        second=self.released_by_safety_net('T02','buyer-test-b')
        late=self.late_count()
        # HTTP reply first, then the message, then a redelivery of that message.
        self.assertEqual(db.apply_payment_answer(first,'paid','pay-test-a'),'late_success')
        envelope=payment_envelope('payment.result',first,'paid',payment_id='pay-test-a')
        noop,duplicate=self.results('payment.result','noop'),self.results('payment.result','duplicate')
        self.consume(envelope);self.consume(envelope)
        self.assertEqual((self.results('payment.result','noop'),self.results('payment.result','duplicate')),
                         (noop+1,duplicate+1))
        # The message first, then the HTTP reply.
        self.consume(payment_envelope('payment.result',second,'paid',payment_id='pay-test-b'))
        self.assertEqual(db.apply_payment_answer(second,'paid','pay-test-b'),'noop')
        self.assertEqual(sorted(e['payload']['booking_id'] for e in self.messages('refund.requested')),
                         sorted([first,second]))                    # exactly one refund each
        self.assertEqual([self.booking_status(b) for b in (first,second)],['refund_required','refund_required'])
        self.assertEqual(self.late_count(),late+2)

    def test_late_failed_or_refunded_for_a_released_booking_is_a_noop(self):                     # N11 (D4)
        booking_id=self.released_by_safety_net()
        rows=self.outbox_count()
        for status in ('failed','refunded'):
            with self.subTest(status):
                self.assertEqual(db.apply_payment_answer(booking_id,status,'pay-test-1'),'noop')
                before=self.results('payment.result','noop')
                self.consume(payment_envelope('payment.result',booking_id,status))
                self.assertEqual(self.results('payment.result','noop'),before+1)
        self.assertEqual((self.booking_status(booking_id),self.payment_id(booking_id)),('released',None))
        self.assertEqual(self.outbox_count(),rows)

    def test_resold_seat_late_success_never_both_never_neither(self):                            # N12 (row 9)
        first=self.released_by_safety_net('T01','buyer-test-a')
        # Someone else buys the freed seat.
        outcome,second,_=db.hold_seat('event-test-many','T01','buyer-test-b')
        self.assertEqual(outcome,'held')
        self.assertEqual(db.start_checkout(second,'buyer-test-b',True),'started')
        self.assertEqual(db.apply_payment_answer(second,'paid','pay-test-b'),'confirmed')
        seat=self.seat('event-test-many','T01')
        self.assertEqual(seat[:3],('booked',second,'buyer-test-b'))
        sequence,snapshots=self.sequence('event-test-many'),len(self.snapshots('event-test-many'))
        confirmed=self.messages('booking.confirmed')
        # The first customer's charge turns up late.
        self.consume(payment_envelope('payment.result',first,'paid',payment_id='pay-test-a'))
        # Never both: the first customer does not get the seat; never neither: they are refunded.
        self.assertEqual((self.booking_status(first),self.payment_id(first)),('refund_required','pay-test-a'))
        self.assertEqual((self.booking_status(second),self.payment_id(second)),('confirmed','pay-test-b'))
        self.assertEqual(self.seat('event-test-many','T01'),seat)
        self.assertEqual((self.sequence('event-test-many'),len(self.snapshots('event-test-many'))),(sequence,snapshots))
        self.assertEqual(self.messages('booking.confirmed'),confirmed)
        self.assertEqual([e['payload'] for e in self.messages('refund.requested')],
                         [{'booking_id':first,'payment_id':'pay-test-a','customer_email':'buyer-test-a@example.test'}])

    def test_row_8_on_a_cancelled_event_says_payments_unavailable(self):                         # N13 (I2)
        booking_id=self.pending_booking()
        self.cancel_big_event()
        self.assertEqual(self.booking_status(booking_id),'payment_pending')     # row 13 leaves it alone
        self.make_due(booking_id)
        self.safety_check(reply=down)
        self.assertEqual(self.booking_status(booking_id),'released')
        self.assertEqual(self.cancelled_payloads(),self.expected_cancelled(
            'payments_unavailable',[(booking_id,'buyer-test')],title=self.event_title()))
        # A late charge is then refunded (row 9), the event being cancelled changes nothing.
        self.consume(payment_envelope('payment.result',booking_id,'paid'))
        self.assertEqual(self.booking_status(booking_id),'refund_required')

    def test_expiry_sweeper_never_touches_payment_pending(self):                                 # N14 (Q3)
        booking_id=self.pending_booking()
        self.expire(booking_id)
        self.assertEqual(self.sweep(),(0,0,0))
        self.assertEqual(self.booking_status(booking_id),'payment_pending')
        self.assertEqual(self.seat('event-test-many','T01')[:2],('held',booking_id))

    def test_shutdown_cancels_a_safety_net_call_and_the_booking_stays_pending(self):             # N15 (D5)
        booking_id=self.pending_booking()
        self.make_due(booking_id)
        rows=self.outbox_count()
        counts=[self.counter('booking_payment_attempts_total',outcome=o) for o in ('timeout','error')]
        client=self.checkout_client(hang=True,safety=True)
        self.wait_until(lambda:len(self.payment_calls)>=1)
        breaker=app.state.checkout.breaker
        client.__exit__(None,None,None)                               # shutdown while the call waits
        self.assertEqual(self.booking_status(booking_id),'payment_pending')
        self.assertEqual(self.outbox_count(),rows)
        self.assertEqual((breaker.state,breaker.failures),(payments.CLOSED,0))
        self.assertEqual([self.counter('booking_payment_attempts_total',outcome=o) for o in ('timeout','error')],counts)
        self.assertEqual(len(self.payment_calls),1)

    def test_lifespan_runs_the_safety_net_survives_an_error_and_stops(self):                     # N16 (D1)
        first=self.pending_booking('T01','buyer-test-1')
        self.make_due(first)
        client=self.checkout_client(safety=True)
        self.wait_for_status(first,'confirmed')
        real=db.due_safety_net
        calls=[]

        def fail_once(*args,**kwargs):
            calls.append(1)
            if len(calls)==1:
                raise psycopg2.OperationalError('simulated database outage')
            return real(*args,**kwargs)
        with patch('db.due_safety_net',fail_once),self.assertLogs('booking.safety_net','ERROR'):
            self.wait_until(lambda:len(calls)>=2)
        second=self.pending_booking('T02','buyer-test-2')
        self.make_due(second)
        self.wait_for_status(second,'confirmed')
        client.__exit__(None,None,None)
        third=self.pending_booking('T03','buyer-test-3')
        self.make_due(third)
        time.sleep(0.3)
        self.assertEqual(self.booking_status(third),'payment_pending')

    def test_safety_net_error_backs_off_before_trying_again(self):                               # N17
        booking_id=self.pending_booking()
        self.make_due(booking_id)
        now=[1000.0]
        errors=self.net_outcome('error')
        real=db.payment_request
        reads=[]

        def flaky(booking):
            reads.append(booking)
            if len(reads)==1:
                raise psycopg2.OperationalError('simulated outage')
            return real(booking)

        def between(i):
            now[0]+=safety_net.ERROR_BACKOFF_S-1 if i==0 else 2       # still backing off, then over
        with patch('db.payment_request',flaky),self.assertLogs('booking.safety_net','ERROR'):
            started=self.safety_check(checks=3,clock=lambda:now[0],between=between)
        self.assertEqual(started,[[booking_id],[],[booking_id]])
        self.assertEqual(len(self.net_calls),1)                       # no Payment call for the failed try
        self.assertEqual(self.booking_status(booking_id),'confirmed')
        self.assertEqual(self.net_outcome('error'),errors+1)


def down(body):
    """A fake Payment that gives no answer (connection refused)."""
    raise httpx.ConnectError('Payment is down')


def payment_reply(status,body):
    """Payment's 200 body (payment/app/store.py format_payment) for a fake Payment."""
    return httpx.Response(200,json={'payment_id':'pay-test-'+body['booking_id'][:8],'booking_id':body['booking_id'],
                                    'amount':body['amount'],'currency':body['currency'],'status':status,
                                    'refund_id':None,'created_at':'2026-11-18T12:00:00+00:00'})


def payment_envelope(message_type,booking_id,status,payment_id=None):
    """A Payment message shaped exactly like payment/app/store.py save_event, parsed as the consumer does."""
    from messaging import Envelope
    payload={'booking_id':booking_id,'payment_id':payment_id or 'pay-test-'+booking_id[:8],
             'amount':'45.00','currency':'EUR','status':status,'customer_email':'buyer-test@example.test'}
    if message_type=='refund.completed':
        payload['refund_id']='refund-test-1'
    if message_type=='refund.failed':
        payload['reason']='provider_rejected'
    body={'message_id':str(uuid.uuid4()),'type':message_type,'occurred_at':'2026-11-19T12:00:00+00:00',
          'version':1,'producer':'payment','payload':payload}
    return Envelope.from_json(json.dumps(body).encode())


def unroutable_error(routing_key='booking.test-unroutable'):
    """Exactly what aiormq 7.2.5 raises for a basic.return with on_return_raises=True
    (aiormq/channel.py, _on_return_frame: PublishError(message, frame))."""
    from aio_pika.exceptions import PublishError
    from aiormq.abc import DeliveredMessage
    from pamqp.commands import Basic
    from pamqp.header import ContentHeader
    frame=Basic.Return(reply_code=312,reply_text='NO_ROUTE',exchange='events',routing_key=routing_key)
    return PublishError(DeliveredMessage(delivery=frame,header=ContentHeader(),body=b'',channel=None),frame)


def nack_error():
    """What aiormq 7.2.5 raises for a broker nack (aiormq/channel.py, _confirm_delivery: DeliveryError(None, frame))."""
    from aio_pika.exceptions import DeliveryError
    from pamqp.commands import Basic
    return DeliveryError(None,Basic.Nack(delivery_tag=1))


def events_envelope(name):
    """Parse an example exactly as the shared consumer does."""
    from messaging import Envelope
    return Envelope.from_json((EXAMPLES/f'event-{name}.json').read_bytes())
