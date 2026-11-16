from fastapi.testclient import TestClient
from discovery.app.main import create_app
from discovery.app.store import Store
from discovery.app.simulator import SimulatedOllama


def test_api_search_validation_admin_and_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv('DISCOVERY_ADMIN_TOKEN', 'test-token')
    app = create_app(Store(f'sqlite+aiosqlite:///{tmp_path}/api.db'), SimulatedOllama(), broker='', seed=True)
    with TestClient(app) as client:
        assert client.get('/health').json()['embedding_provider'] == 'simulated'
        response = client.get('/search', params={'q': 'music Friday night'})
        assert response.status_code == 200
        assert response.json()['embedding_provider'] == 'simulated'
        assert client.get('/search', params={'q': 'next Friday'}).status_code == 422
        assert client.get('/search', params={'q': 'music', 'limit': 1000}).status_code == 422
        assert client.get('/events/no-such-event/similar').status_code == 404
        assert client.get('/admin/indexes').status_code == 403
        assert client.get('/admin/indexes', headers={'X-Admin-Token': 'test-token'}).status_code == 200
        assert client.post('/demo/outage/true', headers={'X-Admin-Token': 'test-token'}).status_code == 200
        assert client.get('/search', params={'q': 'music Friday night'}).json()['mode'] == 'keyword_fallback'
        assert 'discovery_searches_total' in client.get('/metrics').text
