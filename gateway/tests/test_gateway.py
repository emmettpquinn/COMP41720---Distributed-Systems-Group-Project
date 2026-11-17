"""Exercise the gateway's public boundary without running peer services."""

import unittest

import httpx
from fastapi.testclient import TestClient

from app.main import Settings, create_app


class GatewayTests(unittest.TestCase):
    def client(self, handler, **settings):
        client = TestClient(create_app(Settings(**settings), httpx.MockTransport(handler)))
        self.addCleanup(client.__exit__, None, None, None)
        return client.__enter__()

    def test_browse_forwards_only_to_configured_origin_and_drops_credentials(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'event_id': 'demo'})
        client = self.client(handler)
        result = client.get('/api/catalogue/events/demo?view=full', headers={'Authorization': 'secret', 'Cookie': 'secret=1'})
        self.assertEqual(result.json(), {'event_id': 'demo'})
        self.assertEqual(str(seen[0].url), 'http://catalogue:8000/events/demo?view=full')
        self.assertNotIn('authorization', seen[0].headers)
        self.assertNotIn('cookie', seen[0].headers)
        self.assertEqual(result.headers['cache-control'], 'no-store')

    def test_hold_conflict_body_and_status_survive_without_retry(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(409, json={'error': 'seat_taken'})
        client = self.client(handler, holds_enabled=True)
        result = client.post('/api/booking/holds', json={'event_id':'e','seat_id':'A1','customer_id':'demo'})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json(), {'error': 'seat_taken'})
        self.assertEqual(len(seen), 1)
        self.assertIn(b'customer_id', seen[0].content)

    def test_timeout_is_unknown_and_never_retries_hold(self):
        seen = []
        def handler(request):
            seen.append(request)
            raise httpx.ReadTimeout('synthetic timeout', request=request)
        client = self.client(handler, holds_enabled=True)
        result = client.post('/api/booking/holds', json={})
        self.assertEqual(result.status_code, 504)
        self.assertIn('unknown', result.json()['detail'])
        self.assertEqual(len(seen), 1)

    def test_connection_failure_is_safe(self):
        def handler(request):
            raise httpx.ConnectError('credential-bearing internal address', request=request)
        result = self.client(handler).get('/api/catalogue/events')
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('credential', result.text)

    def test_disallowed_routes_and_methods_never_reach_upstream(self):
        def handler(request):
            self.fail('Disallowed request reached a peer')
        client = self.client(handler)
        for path in ['/api/payment/payments', '/api/catalogue/metrics', '/api/booking/checkout', '/api/unknown/events']:
            self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.post('/api/catalogue/events', json={}).status_code, 405)
        self.assertEqual(client.post('/api/booking/holds', json={}).status_code, 503)
        self.assertFalse(client.get('/api/capabilities').json()['checkout'])

    def test_request_limits_redirect_and_non_json_upstreams(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={'Location':'http://example.invalid'})
        client=self.client(handler, holds_enabled=True)
        self.assertEqual(client.post('/api/booking/holds', content='x'*65537, headers={'Content-Type':'application/json'}).status_code, 413)
        self.assertEqual(client.post('/api/booking/holds', content='x').status_code, 415)
        self.assertEqual(calls, [])
        self.assertEqual(client.get('/api/catalogue/events').status_code, 502)
        self.assertEqual(len(calls), 1)
        other=self.client(lambda request:httpx.Response(200,text='not JSON'))
        self.assertEqual(other.get('/api/catalogue/events').status_code, 502)

    def test_static_preview_and_invalid_configuration(self):
        client=self.client(lambda request:httpx.Response(200,json=[]))
        self.assertIn('StageDoor', client.get('/').text)
        self.assertEqual(client.get('/assets/app.js').status_code, 200)
        self.assertIn("frame-ancestors 'none'",client.get('/').headers['content-security-policy'])
        for value in [0,float('nan'),float('inf'),31]:
            with self.assertRaises(ValueError): Settings(timeout_seconds=value)
        for value in ['file:///tmp', 'http://user:secret@host','http://host/private']:
            with self.assertRaises(ValueError): Settings(catalogue_url=value)


class DiscoveryGatewayTests(unittest.TestCase):
    client = GatewayTests.client

    def test_search_query_and_fallback_metadata_survive_proxy(self):
        seen = []
        body = {'mode':'keyword_fallback', 'label':'Showing keyword results',
                'fallback_reason':'circuit_open', 'embedding_provider':'simulated_http', 'results':[]}
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json=body)
        client = self.client(handler, search_enabled=True)
        response = client.get('/api/discovery/search',params={'q':'music Friday after 7pm','limit':5})
        self.assertEqual(response.json(),body)
        self.assertEqual(seen[0].url.host,'discovery')
        self.assertEqual(seen[0].url.params['q'],'music Friday after 7pm')
        self.assertTrue(client.get('/api/capabilities').json()['search'])

    def test_similar_route_and_disabled_search(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json={'results':[]})
        client=self.client(handler,search_enabled=True)
        self.assertEqual(client.get('/api/discovery/events/event-demo-001/similar').status_code,200)
        self.assertEqual(seen[0].url.path,'/events/event-demo-001/similar')
        self.assertEqual(client.get('/api/discovery/admin/indexes').status_code,404)
        blocked=self.client(handler)
        self.assertEqual(blocked.get('/api/discovery/search?q=music').status_code,503)
        self.assertEqual(len(seen),1)


    def test_read_only_search_timeout_has_no_reservation_outcome(self):
        def handler(request):
            raise httpx.ReadTimeout('synthetic timeout',request=request)
        response=self.client(handler,search_enabled=True).get('/api/discovery/search?q=music')
        self.assertEqual(response.status_code,504)
        self.assertNotIn('reservation',response.json()['detail'])
