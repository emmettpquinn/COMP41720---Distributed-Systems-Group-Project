"""Integration additions use unique schemas, never existing Booking inventory."""

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from unittest.mock import patch
import uuid

import psycopg2
from psycopg2 import sql
from fastapi.testclient import TestClient

import db
import demo_seed
from main import app


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

    def client(self):
        env=patch.dict(os.environ,{'BOOKING_DEMO_SEED':'false'})
        env.start();self.addCleanup(env.stop)
        client=TestClient(app)
        client.__enter__();self.addCleanup(client.__exit__,None,None,None)
        return client

    def test_one_hold_wins_concurrently_and_retry_reuses_reference(self):
        self.seed_seat()
        with ThreadPoolExecutor(max_workers=20) as executor:
            results=list(executor.map(lambda customer:db.hold_seat('race-event','A1',str(customer)),range(20)))
        self.assertEqual(sum(outcome=='held' for outcome,ref in results),1)
        self.assertEqual(sum(outcome=='taken' for outcome,ref in results),19)
        winner=next(index for index,(outcome,ref) in enumerate(results) if outcome=='held')
        self.assertEqual(db.hold_seat('race-event','A1',str(winner)),('already_yours',results[winner][1]))

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
        self.assertEqual(db.hold_seat('event-demo-001','A1','buyer'),('already_yours',first[1]))
        self.assertEqual(db.hold_seat('event-demo-001','A1','other'),('taken',None))

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

    def test_readiness_identifies_hold_only_capability(self):
        result=self.client().get('/ready')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['capabilities'],['holds'])
        self.assertEqual(self.client().post('/checkout',json={}).status_code,404)
