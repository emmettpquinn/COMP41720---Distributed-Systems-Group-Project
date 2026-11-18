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
import outbox
import payments
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

    def client(self):
        env=patch('config.BOOKING_DEMO_SEED',False)
        env.start();self.addCleanup(env.stop)
        # Never consume the real queue from a test: no RabbitMQ consumer here.
        amqp=patch('config.AMQP_URL','')
        amqp.start();self.addCleanup(amqp.stop)
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
        self.apply('cancelled')  # newer version of an existing event: no new free seats, no snapshot
        self.assertEqual(self.outbox_count(),count)
        self.assertEqual(client.post('/holds',json=body|{'seat_id':'A2'}).status_code,409)               # cancelled
        self.assertEqual((self.outbox_count(),self.sequence()),(count,seq))

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

    def checkout_client(self,reply=None,gate=None,hang=False):
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
        return self.client()

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
        self.assertEqual({status for (status,) in self.query('SELECT status FROM bookings')},{'held'})
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


def payment_reply(status,body):
    """Payment's 200 body (payment/app/store.py format_payment) for a fake Payment."""
    return httpx.Response(200,json={'payment_id':'pay-test-'+body['booking_id'][:8],'booking_id':body['booking_id'],
                                    'amount':body['amount'],'currency':body['currency'],'status':status,
                                    'refund_id':None,'created_at':'2026-11-18T12:00:00+00:00'})


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
