#!/usr/bin/env python3
"""Single-domain readonly Verisign RDAP lookup. No redirects or case writes."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import math
import ssl
import sys
from pathlib import Path
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import domain_ascii, text, reject_duplicate_pairs, reject_constant, finite_float

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
    try:
        connection.request('GET', path, headers={'Accept': 'application/rdap+json',
                           'Accept-Encoding': 'identity', 'User-Agent': 'brand-protection-skill/1.2'})
        response = connection.getresponse()
        body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise ValueError('RDAP response exceeds 1 MiB')
        return response.status, response.getheader('Content-Type', ''), body
    finally:
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


def lookup(domain, target_id, *, allow_network=False, timeout=10, fresh_hours=1, transport=fetch, now=None):
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
    identifier = 'rdap-' + uuid.uuid4().hex
    result = {'id': identifier, 'target_id': target_id, 'domain_ascii': domain,
              'queried_at': checked.isoformat(), 'expires_at': (checked + timedelta(hours=fresh_hours)).isoformat(),
              'registration': 'unknown', 'relationship': 'unknown', 'signals': [], 'evidence_ids': [],
              'source': {'name': 'Verisign RDAP', 'reference': url, 'coverage': 'partial', 'cache': 'unknown',
                         'limitations': ['registry lookup only; not ownership, safety or infringement proof',
                                         'freshness window is an operational review interval']}}
    report = {'read_only': True, 'writes_case': False, 'authorizes_submission': False,
              'result': result, 'evidence': [], 'errors': []}
    try:
        status, content_type, body = transport(domain, timeout)
        registration = interpret(domain, status, content_type, body)
        result['registration'] = registration
        evidence_id = identifier + '-response'
        result['evidence_ids'] = [evidence_id]
        report['evidence'] = [{'id': evidence_id, 'source_url_or_document': url,
                              'acquired_at': checked.isoformat(), 'method': 'registry_rdap_lookup',
                              'sha256': hashlib.sha256(body).hexdigest(), 'original_path': None,
                              'supports': {'domain_ascii': domain, 'registration': registration, 'http_status': status},
                              'limitations': ['original response bytes not archived; preserve separately when needed'],
                              'disclosure_class': 'restricted'}]
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, http.client.HTTPException):
        result['source']['coverage'] = 'failed'
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
    args = parser.parse_args()
    try:
        result = lookup(args.domain, args.target_id, allow_network=args.allow_network,
                        timeout=args.timeout, fresh_hours=args.fresh_hours)
    except ValueError as exc:
        print(json.dumps({'errors': [str(exc)], 'authorizes_submission': False}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
