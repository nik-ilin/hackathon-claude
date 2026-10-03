"""Offline checks for the dashboard's shared read budget and credentials."""
import io
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

import app


class ReaderTests(unittest.TestCase):
    def test_public_and_private_reads_share_pacing_and_only_private_has_key(self):
        reader = app.Reader('https://example.invalid', 'test-only')
        reader._opener = Mock()
        reader._opener.open.side_effect = [io.BytesIO(b'{}'), io.BytesIO(b'{}')]
        with patch.object(app.time, 'monotonic', side_effect=[0, 0, .1, .3]), \
             patch.object(app.time, 'sleep') as sleep:
            reader.get('/api/feed')
            reader.get('/api/me', private=True)
        self.assertAlmostEqual(sleep.call_args.args[0], .2)
        requests = [call.args[0] for call in reader._opener.open.call_args_list]
        self.assertIsNone(requests[0].get_header('X-team-key'))
        self.assertEqual(requests[1].get_header('X-team-key'), 'test-only')
        self.assertEqual([r.get_method() for r in requests], ['GET', 'GET'])

    def test_missing_key_never_sends_private_request(self):
        reader = app.Reader('https://example.invalid', None)
        with patch.object(reader._opener, 'open') as send:
            with self.assertRaises(ValueError):
                reader.get('/api/me', private=True)
        send.assert_not_called()

    def test_redirect_cannot_forward_private_header(self):
        handler = app.NoRedirect()
        handler.parent = Mock()
        request = urllib.request.Request('https://example.invalid/api/me',
                                         headers={'X-Team-Key': 'test-only'})
        self.assertIsNone(handler.http_error_302(
            request, io.BytesIO(b''), 302, 'Found',
            {'location': 'https://other.invalid/'}))
        handler.parent.open.assert_not_called()

    def test_public_builder_uses_same_reader(self):
        reader = app.Reader('https://example.invalid', None)
        with patch.object(app.public_dashboard, 'Builder') as builder:
            app.Model(reader)
        self.assertIs(builder.call_args.args[0], reader)
