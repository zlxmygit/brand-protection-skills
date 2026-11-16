#!/usr/bin/env python3
"""Capture one explicitly scoped public HTTPS response as inert private evidence."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import io
import json
import math
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import domain_ascii, text
from filesystem_paths import checked_existing_path

LIMIT = 5 * 1024 * 1024
HEADERS = ('content-type', 'content-length', 'content-encoding', 'location', 'date', 'etag', 'last-modified')
REDIRECTS = {301, 302, 303, 307, 308}
SECRET_KEYS = {'token', 'access_token', 'authorization', 'password', 'api_key', 'signature'}


def remaining(deadline):
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError('HTTPS request deadline exceeded')
    return budget


class DeadlineReader(io.RawIOBase):
    """One unbuffered socket operation at a time under an absolute deadline."""
    def __init__(self, source, connection, deadline):
        super().__init__()
        self.source, self.connection, self.deadline = source, connection, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self.connection.settimeout(remaining(self.deadline))
        count = self.source.readinto(buffer)
        remaining(self.deadline)
        return count

    def close(self):
        if not self.closed:
            try:
                self.source.close()
            finally:
                super().close()


class DeadlineSocket:
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def makefile(self, mode, buffering=None):
        if mode != 'rb':
            raise ValueError('Only binary response reads are supported')
        source = self.connection.makefile('rb', buffering=0)
        return io.BufferedReader(DeadlineReader(source, self.connection, self.deadline))

    def sendall(self, data):
        view = memoryview(data)
        while view:
            self.connection.settimeout(remaining(self.deadline))
            sent = self.connection.send(view)
            if not sent:
                raise OSError('HTTPS connection closed during request')
            view = view[sent:]
        remaining(self.deadline)


def validate_url(url, allowed_hosts):
    text(url, 'url', 8192)
    if url != url.strip() or not url.isascii() or '\\' in url:
        raise ValueError('Use an ASCII HTTPS URL without whitespace or backslashes')
    if any(ord(c) < 32 or ord(c) == 127 for c in unquote(url)):
        raise ValueError('Encoded control characters are not allowed')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError('Only credential-free HTTPS port 443 URLs without fragments are supported')
    host = domain_ascii(parsed.hostname)
    if host not in allowed_hosts:
        raise ValueError('Host is outside the explicitly allowed scope')
    if any(key.casefold() in SECRET_KEYS for key, _ in parse_qsl(parsed.query, max_num_fields=100)):
        raise ValueError('Credential-shaped query fields are not supported')
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    return host, path


def public_addresses(host, resolver=socket.getaddrinfo):
    rows = resolver(host, 443, type=socket.SOCK_STREAM)
    addresses = set()
    for row in rows:
        value = row[4][0]
        if '%' in value:
            raise ValueError('Scoped addresses are not supported')
        address = ipaddress.ip_address(value)
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError('DNS returned a non-public address')
        if isinstance(address, ipaddress.IPv6Address) and (address.ipv4_mapped or address.sixtofour or address.teredo):
            raise ValueError('IPv6 transition addresses are not supported')
        addresses.add(str(address))
    if not addresses:
        raise ValueError('DNS returned no usable address')
    return sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        self.address = address
        self.deadline = time.monotonic() + timeout
        self.tls = ssl.create_default_context()
        self.tls.set_alpn_protocols(['http/1.1'])
        super().__init__(host, port=443, timeout=timeout, context=self.tls)

    def connect(self):
        raw = socket.create_connection((self.address, 443), remaining(self.deadline))
        try:
            raw.settimeout(remaining(self.deadline))
            secured = self.tls.wrap_socket(raw, server_hostname=self.host)
            self.sock = DeadlineSocket(secured, self.deadline)
            remaining(self.deadline)
        except BaseException:
            raw.close()
            raise


def request_once(host, path, address, timeout):
    connection = PinnedHTTPS(host, address, timeout)
    response = None
    try:
        connection.request('GET', path, headers={'Accept': 'text/html,text/plain',
                           'Accept-Encoding': 'identity', 'User-Agent': 'brand-protection-skill/1.3'})
        response = connection.getresponse()
        headers = {}
        for name in HEADERS:
            value = response.getheader(name)
            if value is not None:
                text(value, 'response header', 4096)
                headers[name] = value
        if response.status in REDIRECTS:
            remaining(connection.deadline)
            return response.status, headers, b''
        if headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('Compressed responses are not supported')
        media_type = headers.get('content-type', '').split(';', 1)[0].strip().lower()
        if media_type not in {'text/html', 'text/plain', 'application/xhtml+xml'}:
            raise ValueError('Only HTML and plain-text responses are supported')
        if 'content-length' in headers and (not headers['content-length'].isdigit() or int(headers['content-length']) > LIMIT):
            raise ValueError('Invalid or oversized content length')
        chunks, size = [], 0
        started = time.monotonic()
        while True:
            if time.monotonic() - started > timeout:
                raise TimeoutError('Body read time exceeded')
            chunk = response.read1(min(65536, LIMIT + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > LIMIT:
                raise ValueError('Response exceeds 5 MiB')
            chunks.append(chunk)
        body = b''.join(chunks)
        if 'content-length' in headers and len(body) != int(headers['content-length']):
            raise ValueError('Truncated response body')
        remaining(connection.deadline)
        return response.status, headers, body
    finally:
        if response is not None:
            response.close()
        connection.close()


def collect(url, allowed_hosts, *, allow_network=False, timeout=10, max_redirects=0,
            resolver=socket.getaddrinfo, transport=request_once):
    if allow_network is not True:
        raise ValueError('Explicit network permission is required')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError('timeout must be positive and at most 30 seconds')
    if type(max_redirects) is not int or not 0 <= max_redirects <= 3:
        raise ValueError('max_redirects must be between 0 and 3')
    if not isinstance(allowed_hosts, (list, tuple, set)) or not allowed_hosts:
        raise ValueError('An explicit host allowlist is required')
    allowed = {domain_ascii(host) for host in allowed_hosts}
    chain, visited = [], set()
    current = url
    for hop in range(max_redirects + 1):
        host, path = validate_url(current, allowed)
        if current in visited:
            raise ValueError('Redirect loop')
        visited.add(current)
        addresses = public_addresses(host, resolver)
        selected = addresses[0]
        status, headers, body = transport(host, path, selected, timeout)
        if type(status) is not int or not 100 <= status <= 599 or not isinstance(headers, dict) or not isinstance(body, bytes) or len(body) > LIMIT:
            raise ValueError('Invalid response from capture transport')
        chain.append({'url': current, 'address': selected, 'resolved_addresses': addresses,
                      'status': status, 'headers': headers})
        if status not in REDIRECTS:
            return {'supplied_url': url, 'final_url': current, 'redirect_chain': chain,
                    'http_status': status, 'method': 'public_https_get',
                    'observed_at': datetime.now(timezone.utc).isoformat(),
                    'limitations': ['HTTP response only; not a screenshot or rendered browser',
                                    'No inference of infringement or successful removal',
                                    'Selected response headers only; no cookies retained']}, body
        if hop == max_redirects or not isinstance(headers.get('location'), str):
            raise ValueError('Redirect requires explicit scope and a higher permitted hop limit')
        current = urljoin(current, headers['location'])
    raise ValueError('Redirect limit exceeded')


def private_root(directory):
    root = checked_existing_path(directory)
    if not root.is_dir():
        raise ValueError('Private evidence directory must already exist')
    skill = Path(__file__).resolve().parents[1]
    protected = [skill] + [p for p in skill.parents if (p/'.git').exists()]
    # Source archives have no .git. Recognize this project's exact layout and
    # release marker, not an arbitrary ancestor containing a README.
    if skill.parent.name == 'skills':
        candidate = skill.parent.parent
        if ((candidate/'skills/brand-protection').resolve() == skill
                and (candidate/'release/skill-files.json').is_file()
                and (candidate/'README.md').is_file()):
            protected.append(candidate)
        # Never write private results to a host's scanned Skill collection.
        if skill.parent.parent.name in {'.agents', '.codex', '.claude'}:
            protected.append(skill.parent)
    # An installed copy can be invoked from a separate source checkout. Inspect
    # the supplied destination too, using this exact repository layout rather
    # than treating every README or private Git repository as public source.
    for candidate in (root, *root.parents):
        if ((candidate/'skills/brand-protection/SKILL.md').is_file()
                and (candidate/'release/skill-files.json').is_file()
                and (candidate/'README.md').is_file()):
            protected.append(candidate)
        if candidate.name == 'skills' and candidate.parent.name in {'.agents', '.codex', '.claude'}:
            protected.append(candidate)
    if any(root == p or p in root.parents for p in protected):
        raise ValueError('Evidence must be stored outside the Skill and repository')
    return root


def write_private(path, data):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def bounded_collect(url, allowed_hosts, *, total_timeout=30, **options):
    """Bound worker startup, DNS, all hops and collection; the worker never writes."""
    if (isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float))
            or not math.isfinite(total_timeout) or not 0 < total_timeout <= 120):
        raise ValueError('total_timeout must be positive and at most 120 seconds')
    if set(options) - {'allow_network', 'timeout', 'max_redirects'}:
        raise ValueError('Bounded collection does not accept injected transports')
    if options.get('allow_network') is not True:
        raise ValueError('Explicit network permission is required')
    request = json.dumps({'url':url, 'allowed_hosts':list(allowed_hosts), **options},
                         ensure_ascii=True, allow_nan=False).encode('ascii')
    if len(request) > 65536:
        raise ValueError('Capture request exceeds worker input limit')
    try:
        completed = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                                    '--collect-worker'], input=request, capture_output=True,
                                   timeout=total_timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError('Capture collection deadline exceeded') from exc
    if completed.returncode or len(completed.stdout) > 8 * 1024 * 1024:
        raise ValueError('Capture worker failed')
    payload = json.loads(completed.stdout)
    body = base64.b64decode(payload['body_base64'], validate=True)
    metadata = payload['observation']
    if not isinstance(metadata, dict) or len(body) > LIMIT:
        raise ValueError('Invalid capture worker result')
    metadata.setdefault('limitations', []).append(
        'Collection deadline covers worker startup, DNS, all HTTP hops and return; not final filesystem persistence')
    return metadata, body


def collect_worker():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('Capture worker input too large')
        request = json.loads(raw)
        metadata, body = collect(request['url'], request['allowed_hosts'],
                                 allow_network=request.get('allow_network'),
                                 timeout=request.get('timeout', 10),
                                 max_redirects=request.get('max_redirects', 0))
        print(json.dumps({'observation':metadata, 'body_base64':base64.b64encode(body).decode('ascii')},
                         ensure_ascii=True, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        print(json.dumps({'errors':['capture_failed_or_out_of_scope']}))
        return 1


def capture(url, target_id, directory, allowed_hosts, *, total_timeout=None, **options):
    text(target_id, 'target_id', 200)
    root = private_root(directory)
    if total_timeout is None:
        metadata, body = collect(url, allowed_hosts, **options)
    else:
        metadata, body = bounded_collect(url, allowed_hosts, total_timeout=total_timeout, **options)
    destination = Path(tempfile.mkdtemp(prefix='capture-', dir=root))
    try:
        body_path = destination/'body.bin'
        write_private(body_path, body)
        evidence = {'id': 'capture-' + uuid.uuid4().hex, 'target_id': target_id,
                    'source_url_or_document': url, 'acquired_at': metadata['observed_at'],
                    'method': metadata['method'], 'original_path': str(body_path),
                    'sha256': hashlib.sha256(body).hexdigest(), 'disclosure_class': 'restricted',
                    'limitations': metadata['limitations']}
        manifest = {'version': '1.0', 'evidence': evidence, 'observation': metadata,
                    'authorizes_submission': False, 'writes_case': False}
        write_private(destination/'capture.json', json.dumps(manifest, ensure_ascii=True, indent=2).encode('ascii'))
        if hashlib.sha256(body_path.read_bytes()).hexdigest() != evidence['sha256']:
            raise ValueError('Stored evidence hash mismatch')
        return manifest
    except BaseException:
        shutil.rmtree(destination)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--output-root', required=True, type=Path)
    parser.add_argument('--allow-host', required=True, action='append')
    parser.add_argument('--allow-network', action='store_true')
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--total-timeout', type=float, default=30,
                        help='Collection worker wall-clock budget in seconds, including DNS and redirects (max 120)')
    parser.add_argument('--max-redirects', type=int, default=0)
    args = parser.parse_args()
    try:
        result = capture(args.url, args.target_id, args.output_root, args.allow_host,
                         allow_network=args.allow_network, timeout=args.timeout, max_redirects=args.max_redirects,
                         total_timeout=args.total_timeout)
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        print(json.dumps({'errors': ['capture_failed_or_out_of_scope'], 'authorizes_submission': False}))
        return 1
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(collect_worker() if sys.argv[1:] == ['--collect-worker'] else main())
