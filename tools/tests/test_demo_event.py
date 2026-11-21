"""Demo generation must not create duplicate seats or overwrite live inventory."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from create_demo_event import APIError, create_payload, publish_demo


class DemoEventTests(unittest.TestCase):
    def test_default_map_and_multi_digit_columns_preserve_unique_stable_ids(self):
        payload = create_payload()
        seats = payload['event']['seats']
        self.assertEqual(len(seats), 50)
        self.assertEqual(len({seat['seat_id'] for seat in seats}), 50)
        self.assertEqual((seats[0]['label'], seats[-1]['label']), ('A1', 'E10'))
        wider = create_payload(2, 12)['event']['seats']
        self.assertEqual([seat['label'] for seat in wider[:12]], [f'A{i}' for i in range(1, 13)])
        self.assertGreater(datetime.fromisoformat(payload['event']['starts_at']), datetime.now(timezone.utc))

    def test_rows_beyond_z_and_invalid_or_oversized_maps(self):
        self.assertEqual(create_payload(27, 1)['event']['seats'][-1]['label'], 'AA1')
        for dimensions in [(0, 10), (5, 0), (53, 1), (1, 41), (26, 20)]:
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                create_payload(*dimensions)
        for stamp in ['2020-01-01T19:30:00Z', '2099-01-01T19:30:00']:
            with self.assertRaises(ValueError): create_payload(starts_at=stamp)

    def test_existing_event_is_refused_before_any_venue_or_event_write(self):
        calls = []
        def existing(origin, path, body=None):
            calls.append((path, body))
            return {'event_id': 'existing'}
        with self.assertRaisesRegex(ValueError, 'already exists'):
            publish_demo('http://localhost:8001', create_payload(event_id='existing'), request=existing)
        self.assertEqual(calls, [('/events/existing', None)])

    def test_reusing_a_venue_reads_it_without_modifying_its_record(self):
        payload = create_payload(event_id='new-event', venue_id='existing-venue')
        calls = []
        def existing_venue(origin, path, body=None):
            calls.append((path, body))
            if path == '/events/new-event': raise APIError(404, 'missing')
            if path == '/venues': raise APIError(409, 'already exists')
            if path == '/venues/existing-venue': return {'venue_id':'existing-venue'}
            if path == '/events': return {'event_id':'new-event','title':payload['event']['title']}
            self.fail('Unexpected API request')
        result = publish_demo('http://localhost:8001', payload, request=existing_venue)
        self.assertEqual(result['seats'], 50)
        self.assertEqual([path for path, _ in calls], ['/events/new-event','/venues','/venues/existing-venue','/events'])
