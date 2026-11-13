#!/usr/bin/env python3
"""Capture one explicitly scoped public HTTPS response as inert private evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import shutil
import socket
import ssl
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
        self.tls = ssl.create_default_context()
        self.tls.set_alpn_protocols(['http/1.1'])
        super().__init__(host, port=443, timeout=timeout, context=self.tls)

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self.tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def request_once(host, path, address, timeout):
    connection = PinnedHTTPS(host, address, timeout)
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
        return response.status, headers, body
    finally:
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
    if any(root == p or p in root.parents for p in protected):
        raise ValueError('Evidence must be stored outside the Skill and repository')
    return root


def write_private(path, data):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def capture(url, target_id, directory, allowed_hosts, **options):
    text(target_id, 'target_id', 200)
    root = private_root(directory)
    metadata, body = collect(url, allowed_hosts, **options)
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
    parser.add_argument('--max-redirects', type=int, default=0)
    args = parser.parse_args()
    try:
        result = capture(args.url, args.target_id, args.output_root, args.allow_host,
                         allow_network=args.allow_network, timeout=args.timeout, max_redirects=args.max_redirects)
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        print(json.dumps({'errors': ['capture_failed_or_out_of_scope'], 'authorizes_submission': False}))
        return 1
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
