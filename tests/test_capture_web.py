"""Synthetic capture contracts, including SSRF and private-write boundaries."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('capture_web', SCRIPTS/'capture_web.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
PUBLIC = '93.184.216.34'


def resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC, 443))]


def transport(*args):
    return 200, {'content-type': 'text/html'}, b'<p>synthetic</p>'


class CaptureContracts(unittest.TestCase):
    def test_scope_rejects_protocol_credentials_ports_fragments_and_tokens(self):
        for url in ('http://example.com', 'file:///etc/passwd', 'https://u:p@example.com',
                    'https://example.com:444/', 'https://example.com/#x', 'https://other.com/',
                    'https://127.0.0.1/', 'https://example.com/?token=x',
                    'https://example.com/%0d%0aX:', ' https://example.com'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.validate_url(url, {'example.com'})
        self.assertEqual(m.validate_url('https://example.com/p?variant=1', {'example.com'}), ('example.com', '/p?variant=1'))

    def test_private_loopback_linklocal_multicast_and_transition_ips_rejected(self):
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '100.64.0.1', '224.0.0.1',
                        '::1', 'fc00::1', 'fe80::1', '::ffff:127.0.0.1', '2002:7f00:1::'):
            lookup = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
            with self.subTest(address=address), self.assertRaises(ValueError):
                m.public_addresses('example.com', lookup)

    def test_mixed_public_private_dns_is_rejected_before_request(self):
        lookup = lambda *a, **k: resolver(*a, **k) + [(2, 1, 6, '', ('10.0.0.1', 443))]
        sender = Mock()
        with self.assertRaises(ValueError):
            m.collect('https://example.com', ['example.com'], allow_network=True, resolver=lookup, transport=sender)
        sender.assert_not_called()

    def test_explicit_permission_and_allowlist_are_required(self):
        for kwargs in ({}, {'allow_network': False}, {'allow_network': 1}):
            with self.assertRaises(ValueError):
                m.collect('https://example.com', ['example.com'], resolver=resolver, transport=transport, **kwargs)
        with self.assertRaises(ValueError):
            m.collect('https://example.com', [], allow_network=True)

    def test_transport_receives_pinned_public_ip(self):
        sender = Mock(side_effect=transport)
        result, body = m.collect('https://example.com', ['example.com'], allow_network=True, resolver=resolver, transport=sender)
        self.assertEqual(sender.call_args.args[2], PUBLIC)
        self.assertEqual(result['redirect_chain'][0]['address'], PUBLIC)
        self.assertEqual(body, b'<p>synthetic</p>')

    def test_cross_host_redirect_is_checked_before_second_request(self):
        sender = Mock(return_value=(302, {'location': 'https://other.com'}, b''))
        with self.assertRaises(ValueError):
            m.collect('https://example.com', ['example.com'], allow_network=True, max_redirects=1, resolver=resolver, transport=sender)
        self.assertEqual(sender.call_count, 1)

    def test_same_host_redirect_records_chain_and_loop_is_rejected(self):
        sender = Mock(side_effect=[(302, {'location': '/next'}, b''), transport()])
        result, _ = m.collect('https://example.com', ['example.com'], allow_network=True, max_redirects=1, resolver=resolver, transport=sender)
        self.assertEqual(result['final_url'], 'https://example.com/next')
        self.assertEqual(len(result['redirect_chain']), 2)
        with self.assertRaises(ValueError):
            m.collect('https://example.com', ['example.com'], allow_network=True, max_redirects=1,
                      resolver=resolver, transport=Mock(return_value=(302, {'location': 'https://example.com'}, b'')))

    def test_default_does_not_follow_redirects(self):
        sender = Mock(return_value=(302, {'location': '/next'}, b''))
        with self.assertRaises(ValueError):
            m.collect('https://example.com', ['example.com'], allow_network=True, resolver=resolver, transport=sender)
        self.assertEqual(sender.call_count, 1)

    def test_403_is_only_an_observation(self):
        result, _ = m.collect('https://example.com', ['example.com'], allow_network=True, resolver=resolver,
                              transport=Mock(return_value=(403, {'content-type': 'text/plain'}, b'Forbidden')))
        self.assertEqual(result['http_status'], 403)
        self.assertNotIn('verified_effective', json.dumps(result))

    def test_capture_is_inert_hashed_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            first = m.capture('https://example.com', 't1', directory, ['example.com'], allow_network=True, resolver=resolver, transport=transport)
            second = m.capture('https://example.com', 't1', directory, ['example.com'], allow_network=True, resolver=resolver, transport=transport)
            path = Path(first['evidence']['original_path'])
            self.assertEqual(path.suffix, '.bin')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), first['evidence']['sha256'])
            self.assertNotEqual(first['evidence']['original_path'], second['evidence']['original_path'])
            self.assertFalse(first['authorizes_submission'])
            self.assertFalse(first['writes_case'])

    def test_failed_collection_creates_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                m.capture('https://example.com', 't', directory, ['example.com'], allow_network=True,
                          resolver=resolver, transport=Mock(side_effect=OSError('failure')))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_partial_write_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'write_private', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                m.capture('https://example.com', 't', directory, ['example.com'], allow_network=True,
                          resolver=resolver, transport=transport)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_output_inside_skill_is_rejected_before_network(self):
        sender = Mock()
        with self.assertRaises(ValueError):
            m.capture('https://example.com', 't', SCRIPTS, ['example.com'], allow_network=True, transport=sender)
        sender.assert_not_called()

    def test_time_and_redirect_bounds_are_strict(self):
        for kwargs in ({'timeout': True}, {'timeout': float('nan')}, {'timeout': 0}, {'max_redirects': 4}, {'max_redirects': True}):
            with self.assertRaises(ValueError):
                m.collect('https://example.com', ['example.com'], allow_network=True, resolver=resolver, transport=transport, **kwargs)

    def test_binary_oversized_and_compressed_responses_are_rejected(self):
        for headers in ({'content-type': 'application/octet-stream'}, {'content-type': 'text/html', 'content-encoding': 'gzip'},
                        {'content-type': 'text/html', 'content-length': str(m.LIMIT+1)}):
            response = Mock(status=200)
            response.getheader.side_effect = lambda name: headers.get(name)
            connection = Mock()
            connection.getresponse.return_value = response
            with patch.object(m, 'PinnedHTTPS', return_value=connection), self.assertRaises(ValueError):
                m.request_once('example.com', '/', PUBLIC, 10)
            connection.close.assert_called_once()

    def test_short_content_length_is_not_a_complete_capture(self):
        response = Mock(status=200)
        headers = {'content-type': 'text/html', 'content-length': '100'}
        response.getheader.side_effect = lambda name: headers.get(name)
        response.read1.side_effect = [b'short', b'']
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(m, 'PinnedHTTPS', return_value=connection), self.assertRaises(ValueError):
            m.request_once('example.com', '/', PUBLIC, 10)

    def test_default_tls_verifies_certificate_and_hostname(self):
        import ssl
        connection = m.PinnedHTTPS('example.com', PUBLIC, 10)
        self.assertTrue(connection.tls.check_hostname)
        self.assertEqual(connection.tls.verify_mode, ssl.CERT_REQUIRED)

    def test_pinned_tls_uses_original_hostname_without_second_dns_lookup(self):
        connection = m.PinnedHTTPS('example.com', PUBLIC, 10)
        connection.tls = Mock()
        raw = Mock()
        with patch.object(m.socket, 'create_connection', return_value=raw) as create:
            connection.connect()
        self.assertEqual(create.call_args.args, ((PUBLIC, 443), 10))
        connection.tls.wrap_socket.assert_called_once_with(raw, server_hostname='example.com')


if __name__ == '__main__':
    unittest.main()
