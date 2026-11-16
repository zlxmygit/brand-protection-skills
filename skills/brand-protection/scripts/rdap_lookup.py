#!/usr/bin/env python3
"""Single-domain Verisign RDAP lookup with optional private response archive."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import math
import shutil
import ssl
import sys
import tempfile
from pathlib import Path
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import domain_ascii, text, reject_duplicate_pairs, reject_constant, finite_float
from capture_web import private_root, write_private
from evidence_export import read_evidence

HOST = 'rdap.verisign.com'
LIMIT = 1024 * 1024


def endpoint(domain):
    domain_ascii(domain)
    labels = domain.split('.')
    if len(labels) != 2 or labels[-1] not in {'com', 'net'}:
        raise ValueError('Only explicit second-level .com/.net domains are supported')
    return 'https://' + HOST + '/' + labels[-1] + '/v1/domain/' + domain


def fetch(domain, timeout):
    url = endpoint(domain)
    path = url.split(HOST, 1)[1]
    connection = http.client.HTTPSConnection(HOST, timeout=timeout, context=ssl.create_default_context())
    response = None
    try:
        connection.request('GET', path, headers={'Accept': 'application/rdap+json',
                           'Accept-Encoding': 'identity', 'User-Agent': 'brand-protection-skill/1.8.1'})
        response = connection.getresponse()
        media_type = response.getheader('Content-Type', '')
        if response.status not in {200, 404}:
            return response.status, media_type, b''
        encoding = response.getheader('Content-Encoding', 'identity')
        if not isinstance(encoding, str) or encoding.strip().lower() != 'identity':
            raise ValueError('Encoded RDAP responses are not supported')
        transfer = response.getheader('Transfer-Encoding')
        length = response.getheader('Content-Length')
        if transfer is not None:
            if (not isinstance(transfer, str) or transfer.strip().lower() != 'chunked'
                    or length is not None):
                raise ValueError('Ambiguous or unsupported RDAP message framing')
        expected = None
        if length is not None:
            if (not isinstance(length, str) or not length.isascii()
                    or not length.isdigit() or len(length) > 20):
                raise ValueError('Invalid RDAP Content-Length')
            expected = int(length)
            if expected > LIMIT:
                raise ValueError('RDAP response exceeds 1 MiB')
        body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise ValueError('RDAP response exceeds 1 MiB')
        if expected is not None and len(body) != expected:
            raise ValueError('Incomplete RDAP response')
        return response.status, media_type, body
    finally:
        if response is not None:
            response.close()
        connection.close()


def interpret(domain, status, media_type, body):
    if type(status) is not int or status not in {200, 404}:
        raise ValueError('RDAP HTTP response requires review')
    if not isinstance(body, bytes) or len(body) > LIMIT:
        raise ValueError('Invalid or oversized RDAP response')
    if not isinstance(media_type, str) or media_type.split(';', 1)[0].strip().lower() not in {'application/json', 'application/rdap+json'}:
        raise ValueError('RDAP JSON content type required')
    data = json.loads(body.decode('utf-8-sig'), object_pairs_hook=reject_duplicate_pairs,
                      parse_constant=reject_constant, parse_float=finite_float)
    if not isinstance(data, dict):
        raise ValueError('RDAP response must be an object')
    if status == 404:
        if type(data.get('errorCode')) is not int or data['errorCode'] != 404:
            raise ValueError('Unverified RDAP not-found response')
        return 'not_found'
    if data.get('objectClassName') != 'domain' or not isinstance(data.get('ldhName'), str) or data['ldhName'].lower() != domain:
        raise ValueError('RDAP response does not identify the requested domain')
    if 'errorCode' in data:
        raise ValueError('Conflicting RDAP response')
    return 'registered'


def archive_response(root, body):
    """Create a private original without overwriting, then verify downstream reads."""
    root = private_root(root)
    destination = Path(tempfile.mkdtemp(prefix='rdap-', dir=root))
    try:
        saved = destination / 'response.bin'
        write_private(saved, body)
        if read_evidence(root, str(saved), hashlib.sha256(body).hexdigest()) != body:
            raise ValueError('Archived RDAP bytes do not match the original response')
        return str(saved)
    except BaseException:
        shutil.rmtree(destination)
        raise


def lookup(domain, target_id, *, allow_network=False, timeout=10, fresh_hours=1,
           transport=fetch, now=None, archive_root=None):
    url = endpoint(domain)
    text(target_id, 'target_id', 200)
    for label, value, maximum in [('timeout', timeout, 30), ('fresh_hours', fresh_hours, 24)]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError(label + ': invalid bound')
    if allow_network is not True:
        raise ValueError('Explicit network permission is required')
    checked = now or datetime.now(timezone.utc)
    if not isinstance(checked, datetime) or checked.tzinfo is None or checked.utcoffset() is None:
        raise ValueError('now requires timezone')
    checked = checked.astimezone(timezone.utc)
    root = private_root(archive_root) if archive_root is not None else None
    identifier = 'rdap-' + uuid.uuid4().hex
    result = {'id': identifier, 'target_id': target_id, 'domain_ascii': domain,
              'queried_at': checked.isoformat(), 'expires_at': (checked + timedelta(hours=fresh_hours)).isoformat(),
              'registration': 'unknown', 'relationship': 'unknown', 'signals': [], 'evidence_ids': [],
              'source': {'name': 'Verisign RDAP', 'reference': url, 'coverage': 'partial', 'cache': 'unknown',
                         'limitations': ['registry lookup only; not ownership, safety or infringement proof',
                                         'freshness window is an operational review interval']}}
    report = {'read_only': root is None, 'writes_case': False, 'writes_archive': False,
              'authorizes_submission': False,
              'result': result, 'evidence': [], 'errors': []}
    phase = 'lookup'
    try:
        status, content_type, body = transport(domain, timeout)
        registration = interpret(domain, status, content_type, body)
        original_path = None
        limitations = ['original response bytes not archived; preserve separately when needed']
        if root is not None:
            phase = 'archive'
            original_path = archive_response(root, body)
            limitations = ['registry response only; ownership, safety and infringement require separate evidence']
        result['registration'] = registration
        evidence_id = identifier + '-response'
        result['evidence_ids'] = [evidence_id]
        report['evidence'] = [{'id': evidence_id, 'source_url_or_document': url,
                              'acquired_at': checked.isoformat(), 'method': 'registry_rdap_lookup',
                              'sha256': hashlib.sha256(body).hexdigest(), 'original_path': original_path,
                              'supports': {'domain_ascii': domain, 'registration': registration, 'http_status': status},
                              'limitations': limitations,
                              'disclosure_class': 'restricted'}]
        report['writes_archive'] = original_path is not None
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, http.client.HTTPException):
        result['source']['coverage'] = 'failed'
        if phase == 'archive':
            result['source']['limitations'].append('response archive failed or could not be verified; no assertion retained')
            report['errors'].append({'code': 'archive_unavailable', 'retry': 'repair the private archive and review; no automatic retry'})
        else:
            result['source']['limitations'].append('request unavailable, rejected or response invalid; no inference made')
            report['errors'].append({'code': 'lookup_unavailable', 'retry': 'manual review; do not loop or bypass limits'})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--allow-network', action='store_true')
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--fresh-hours', type=float, default=1)
    parser.add_argument('--archive-root', type=Path,
                        help='Existing private directory outside the Skill/repository; archive original response bytes')
    args = parser.parse_args()
    try:
        result = lookup(args.domain, args.target_id, allow_network=args.allow_network,
                        timeout=args.timeout, fresh_hours=args.fresh_hours, archive_root=args.archive_root)
    except ValueError as exc:
        print(json.dumps({'errors': [str(exc)], 'authorizes_submission': False}, ensure_ascii=True))
        return 2
    except OSError:
        print(json.dumps({'errors': ['private_archive_directory_unavailable'], 'authorizes_submission': False}))
        return 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
