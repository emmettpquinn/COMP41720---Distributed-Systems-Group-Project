"""Run a real-HTTP failure/recovery/index-upgrade rehearsal without Docker.

Starts isolated local processes, creates a temporary SQLite database, writes
JSON evidence and cleans up only the processes it started. No real AI or broker.
Example: python -m discovery.scripts.rehearse --output work/rehearsal
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import httpx

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def exercise(api, stub):
    checks = []
    headers = {'X-Admin-Token': 'rehearsal-only'}
    async with httpx.AsyncClient(base_url=api, timeout=5) as client, httpx.AsyncClient(base_url=stub) as control:
        async def until(function, timeout=30):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    result = await function()
                    if result:
                        return result
                except (httpx.ConnectError, httpx.RemoteProtocolError):
                    pass
                await asyncio.sleep(0.1)
            raise AssertionError('Rehearsal condition was not reached within its deadline')

        async def healthy():
            response = await client.get('/health')
            return response.status_code == 200

        await until(healthy)
        async def search():
            response = await client.get('/search', params={'q': 'calm music after 7pm'})
            response.raise_for_status()
            return response.json()

        async def semantic():
            result = await search()
            return result if result['mode'] == 'semantic' else None

        normal = await until(semantic)
        assert normal['embedding_provider'] == 'simulated_http'
        ids = {r['event_id'] for r in normal['results']}
        assert ids == {'demo-jazz', 'demo-rock', 'demo-saturday'}, f'Unexpected seed/date relationship: {ids}'
        checks.append({'check': 'normal_http_search', 'passed': True, 'response': normal})

        response = await control.post('/test/faults', json={'delay_seconds': 2.0})
        response.raise_for_status()
        observations = []
        for _ in range(6):
            started = time.monotonic()
            result = await search()
            elapsed = time.monotonic() - started
            assert result['mode'] == 'keyword_fallback'
            assert {r['event_id'] for r in result['results']} == ids
            observations.append({'seconds': elapsed, 'reason': result['fallback_reason']})
        assert any(o['reason'] == 'circuit_open' for o in observations)
        checks.append({'check': 'slow_model_and_breaker', 'passed': True, 'requests': observations,
                       'configured_timeout_seconds': 1.5})

        response = await control.post('/test/faults', json={})
        response.raise_for_status()
        started = time.monotonic()
        recovered = await until(semantic, timeout=15)
        checks.append({'check': 'half_open_recovery', 'passed': True,
                       'seconds': time.monotonic() - started, 'mode': recovered['mode']})

        (await control.post('/test/faults', json={'delay_seconds': 0.5})).raise_for_status()
        concurrent = await asyncio.gather(*(search() for _ in range(5)))
        peak = (await control.get('/test/status')).json()['peak_calls']
        assert peak <= 4
        assert any(r['fallback_reason'] == 'bulkhead_full' for r in concurrent)
        assert any(r['mode'] == 'semantic' for r in concurrent)
        checks.append({'check': 'five_concurrent_http_searches', 'passed': True, 'model_peak_calls': peak,
                       'responses': [{'mode': r['mode'], 'reason': r['fallback_reason']} for r in concurrent]})
        (await control.post('/test/faults', json={})).raise_for_status()

        model_info = (await control.get('/api/tags')).json()['models'][1]
        response = await client.post('/admin/indexes', headers=headers, json={
            'version': 'v2', 'model': model_info['name'], 'model_version': model_info['digest'], 'dimensions': 64})
        assert response.status_code == 201, response.text

        async def complete():
            response = await client.get('/admin/indexes', headers=headers)
            response.raise_for_status()
            status = response.json()
            return status if any(i['index_version'] == 'v2' and i['pending'] == 0 for i in status) else None

        await until(complete)
        # Search remains on v1 while v2 builds.
        assert (await search())['index_version'] == 'v1'
        response = await client.post('/admin/indexes/v2/activate', headers=headers)
        response.raise_for_status()
        assert (await search())['index_version'] == 'v2'
        response = await client.post('/admin/indexes/v1/activate', headers=headers)
        response.raise_for_status()
        assert (await search())['index_version'] == 'v1'
        checks.append({'check': 'different_dimension_upgrade_and_rollback', 'passed': True, 'dimensions': [768, 64]})
        metric_text = (await client.get('/metrics')).text
        assert 'discovery_notional_cost_total' in metric_text
        checks.append({'check': 'metrics_export', 'passed': True})
        return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('work/rehearsal'))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    api_port, stub_port = free_port(), free_port()
    while stub_port == api_port:
        stub_port = free_port()
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(ROOT), str(ROOT/'shared')]),
           'AMQP_URL': '', 'SEED_DEMO': '1', 'EMBEDDING_PROVIDER': 'simulated_http',
           'EMBEDDING_MODEL': 'nomic-embed-text:latest', 'EMBEDDING_DIMENSIONS': '768',
           'MODEL_VERSION': 'unverified-local-tag', 'OLLAMA_URL': f'http://127.0.0.1:{stub_port}',
           'OLLAMA_COOLDOWN_SECONDS': '3', 'OLLAMA_FAILURE_THRESHOLD': '5', 'OLLAMA_TIMEOUT_SECONDS': '1.5',
           'DISCOVERY_ADMIN_TOKEN': 'rehearsal-only'}
    processes = []
    result = {'generated_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'infrastructure': {'database': 'SQLite', 'model': 'synthetic Ollama-compatible HTTP server',
                                 'broker': 'disabled', 'cooldown_seconds': 3}, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='rehearsal-', dir=output) as temporary, (output/'processes.log').open('w', encoding='utf-8') as log:
        env['DATABASE_URL'] = 'sqlite+aiosqlite:///' + str(Path(temporary)/'test.db').replace('\\', '/')
        try:
            for module, port in [('discovery.testing.ollama_stub:app', stub_port), ('discovery.app.main:app', api_port)]:
                processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', module, '--host', '127.0.0.1', '--port', str(port)],
                    cwd=ROOT, env=env, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
            result['checks'] = asyncio.run(exercise(f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{stub_port}'))
            result['passed'] = True
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            (output/'rehearsal.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f"Passed {len(result['checks'])} real-HTTP rehearsal checks. Evidence: {output/'rehearsal.json'}")


if __name__ == '__main__':
    main()
