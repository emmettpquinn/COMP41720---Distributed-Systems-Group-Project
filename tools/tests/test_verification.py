"""Regression checks for actual integration-harness response formats."""

import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _verification import http


class Reply(io.BytesIO):
    def __init__(self, status, body, content_type):
        super().__init__(body.encode())
        self.code = status
        self.headers = {'Content-Type': content_type}


class VerificationResponseTests(unittest.TestCase):
    def test_mailhog_text_json_is_parsed(self):
        with patch('urllib.request.urlopen', return_value=Reply(200, '{"items": [], "total": 0}', 'text/json; charset=utf-8')):
            self.assertEqual(http('http://example.test', '/api/v2/messages'), {'items': [], 'total': 0})

    def test_absent_or_void_ticket_allowed_but_unavailable_service_is_not(self):
        for status, body in [(404, '{"detail":"No ticket for this booking"}'),
                             (200, '{"status":"void","valid":false}')]:
            with patch('urllib.request.urlopen', return_value=Reply(status, body, 'application/json')):
                self.assertIsInstance(http('http://example.test', '/tickets/test', expected=(200, 404)), dict)
        with patch('urllib.request.urlopen', return_value=Reply(503, '{}', 'application/json')):
            with self.assertRaises(AssertionError):
                http('http://example.test', '/tickets/test', expected=(200, 404))

    def test_metrics_remain_plain_text(self):
        with patch('urllib.request.urlopen', return_value=Reply(200, 'metric_name 1.0\n', 'text/plain')):
            self.assertEqual(http('http://example.test', '/metrics'), 'metric_name 1.0\n')
