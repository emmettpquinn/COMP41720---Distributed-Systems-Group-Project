"""Create/update/cancel a unique event through real Catalogue; verify Discovery.
Run against an agreed local integration stack. Leaves a cancelled audit event.
Embedding provider is reported, not assumed to be real Ollama.
"""
import argparse
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalogue', default='http://127.0.0.1:8001')
    parser.add_argument('--discovery', default='http://127.0.0.1:8003')
    parser.add_argument('--output', default='work/catalogue-check.json')
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(base, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=10) as response:
            return json.load(response)

    token = 'integration' + uuid4().hex
    event_id, venue_id = token, 'venue' + uuid4().hex
    evidence = {'started_utc': datetime.now(timezone.utc).isoformat(),
                'event_id': event_id, 'catalogue': args.catalogue,
                'discovery': args.discovery, 'checks': []}

    def observe(version, absent=False):
        started = time.monotonic()
        while True:
            result = request(args.discovery, '/search?q=' + token)
            rows = [row for row in result['results'] if row['event_id'] == event_id]
            matched = not rows if absent else bool(rows and rows[0]['event_version'] == version)
            if matched:
                evidence['checks'].append({'version': version, 'absent': absent,
                    'seconds_after_catalogue_response': round(time.monotonic() - started, 3),
                    'response': result})
                return
            if time.monotonic() - started > 30:
                raise AssertionError('Catalogue change did not appear in Discovery within 30 seconds')
            time.sleep(0.2)

    request(args.catalogue, '/venues', {'venue_id': venue_id, 'name': 'Integration test venue', 'city': 'Dublin'})
    request(args.catalogue, '/events', {
        'event_id': event_id, 'venue_id': venue_id, 'title': token + ' music',
        'description': 'Synthetic event created through the real Catalogue API.',
        'starts_at': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        'timezone': 'Europe/Dublin', 'tags': [token, 'music'],
        'seats': [{'seat_id': 'A1', 'section': 'A', 'label': 'A1'}]})
    observe(1)
    request(args.catalogue, '/events/' + event_id,
            {'expected_version': 1, 'title': token + ' revised music'}, 'PATCH')
    observe(2)
    request(args.catalogue, '/events/' + event_id,
            {'expected_version': 2, 'status': 'cancelled'}, 'PATCH')
    observe(3, absent=True)
    evidence['status'] = 'passed'
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'passed', 'output': str(path), 'event_id': event_id}))


if __name__ == '__main__':
    main()
