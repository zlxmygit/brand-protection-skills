"""Real HTTP parsing over local socket pairs; no external network targets."""
import importlib.util
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('v184_capture', SCRIPTS/'capture_web.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CaptureDeadlines(unittest.TestCase):
    def request(self, prefix, slow=b'', suffix=b'', timeout=.10):
        client, peer = socket.socketpair()
        stop = threading.Event()
        def feed():
            try:
                peer.settimeout(2)
                request = b''
                while b'\r\n\r\n' not in request:
                    chunk = peer.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                peer.sendall(prefix)
                for byte in slow:
                    if stop.wait(.012):
                        return
                    peer.sendall(bytes([byte]))
                peer.sendall(suffix)
            except OSError:
                pass
            finally:
                peer.close()
        worker = threading.Thread(target=feed, daemon=True)
        tls = Mock()
        tls.wrap_socket.side_effect = lambda raw, **kwargs: raw
        worker.start()
        try:
            with patch.object(m.ssl, 'create_default_context', return_value=tls), \
                 patch.object(m.socket, 'create_connection', return_value=client):
                return m.request_once('example.com', '/', '93.184.216.34', timeout)
        finally:
            stop.set()
            client.close()
            worker.join(timeout=1)

    def assert_bounded(self, prefix, slow, suffix):
        started = time.monotonic()
        with self.assertRaises((TimeoutError, socket.timeout)):
            self.request(prefix, slow, suffix)
        self.assertLess(time.monotonic() - started, .35)

    def test_slow_response_header_exceeds_shared_budget(self):
        self.assert_bounded(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nX-Slow: ',
                            b'x'*32, b'\r\nContent-Length: 2\r\n\r\nOK')

    def test_slow_redirect_header_exceeds_shared_budget(self):
        self.assert_bounded(b'HTTP/1.1 302 Found\r\nLocation: /next\r\nX-Slow: ',
                            b'x'*32, b'\r\n\r\n')

    def test_slow_status_line_exceeds_shared_budget(self):
        self.assert_bounded(b'HTTP/1.1 200 ', b'x'*32,
                            b'\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK')

    def test_slow_chunked_trailer_exceeds_shared_budget(self):
        self.assert_bounded(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nOK\r\n0\r\nX-Trailer: ',
                            b'x'*32, b'\r\n\r\n')

    def test_slow_chunk_header_exceeds_shared_budget(self):
        self.assert_bounded(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n2;',
                            b'x'*32, b'\r\nOK\r\n0\r\n\r\n')

    def test_informational_headers_share_budget(self):
        self.assert_bounded(b'HTTP/1.1 100 Continue\r\nX-Slow: ', b'x'*32,
                            b'\r\n\r\nHTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK')

    def test_normal_length_chunked_and_connection_close_bodies_remain_valid(self):
        for response in (
            b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK',
            b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nOK\r\n0\r\nX-Trailer: yes\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nOK'):
            with self.subTest(response=response):
                self.assertEqual(self.request(response, timeout=1)[2], b'OK')

    def test_worker_timeout_terminates_collection_before_any_evidence_write(self):
        original_run = subprocess.run
        def slow_worker(command, **kwargs):
            return original_run([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(5)'], **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch.object(m.subprocess, 'run', side_effect=slow_worker):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                m.capture('https://example.com', 't1', directory, ['example.com'],
                          allow_network=True, total_timeout=.15)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_private_root_prevents_worker_start(self):
        with patch.object(m.subprocess, 'run') as launch, self.assertRaises(ValueError):
            m.capture('https://example.com', 't1', SCRIPTS, ['example.com'],
                      allow_network=True, total_timeout=1)
        launch.assert_not_called()

    def test_bounded_worker_success_preserves_exact_inert_body(self):
        body = b'<p>synthetic response</p>'
        metadata = {'observed_at':'2026-01-01T00:00:00+00:00', 'method':'public_https_get',
                    'limitations':[], 'http_status':200}
        payload = json.dumps({'observation':metadata, 'body_base64':base64.b64encode(body).decode('ascii')}).encode('ascii')
        result = subprocess.CompletedProcess([], 0, payload, b'')
        with tempfile.TemporaryDirectory() as directory, patch.object(m.subprocess, 'run', return_value=result) as run:
            record = m.capture('https://example.com', 't1', directory, ['example.com'],
                               allow_network=True, total_timeout=2)
            self.assertEqual(Path(record['evidence']['original_path']).read_bytes(), body)
            self.assertFalse(record['authorizes_submission'])
            self.assertEqual(run.call_args.kwargs['timeout'], 2)
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_worker_entry_can_reject_invalid_scope_without_network(self):
        with self.assertRaises(ValueError):
            m.bounded_collect('http://example.com', ['example.com'], allow_network=True, total_timeout=5)

    def test_total_budget_and_network_permission_validated_before_start(self):
        for value in (True, 0, float('nan'), 121):
            with patch.object(m.subprocess, 'run') as launch, self.assertRaises(ValueError):
                m.bounded_collect('https://example.com', ['example.com'], allow_network=True, total_timeout=value)
            launch.assert_not_called()
        with patch.object(m.subprocess, 'run') as launch, self.assertRaises(ValueError):
            m.bounded_collect('https://example.com', ['example.com'])
        launch.assert_not_called()

    def test_installed_copy_rejects_separate_source_repository_and_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base/'source'
            (source/'skills/brand-protection').mkdir(parents=True)
            (source/'release').mkdir()
            (source/'README.md').write_text('Synthetic source')
            (source/'skills/brand-protection/SKILL.md').write_text('Synthetic entry')
            (source/'release/skill-files.json').write_text('[]')
            (source/'output').mkdir()
            installed = base/'installed/brand-protection/scripts/capture_web.py'
            with patch.object(m, '__file__', str(installed)):
                for target in (source, source/'output'):
                    with self.subTest(target=target), self.assertRaises(ValueError):
                        m.private_root(target)

    def test_installed_copy_rejects_other_known_scan_roots_but_allows_private_git(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            collection = base/'.agents/skills'
            (collection/'other-skill').mkdir(parents=True)
            private = base/'private'
            (private/'.git').mkdir(parents=True)
            (private/'README.md').write_text('Private project, not the Skill source')
            installed = base/'installed/brand-protection/scripts/capture_web.py'
            with patch.object(m, '__file__', str(installed)):
                for target in (collection, collection/'other-skill'):
                    with self.subTest(target=target), self.assertRaises(ValueError):
                        m.private_root(target)
                self.assertEqual(m.private_root(private), private)


if __name__ == '__main__':
    unittest.main()
