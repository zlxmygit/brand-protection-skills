#!/usr/bin/env python3
"""Validate recorded investigation results. Read-only; no network or sending."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import re
import sys

from record_integrity import check_identity, evidence_closure

REGISTRATION = {'registered', 'not_found', 'unknown'}
RELATIONSHIP = {'owned', 'authorized', 'third_party', 'unknown'}
SIGNALS = {'brand_use', 'copied_content', 'impersonation', 'transaction',
           'suspected_counterfeit', 'credential_collection'}
COVERAGE = {'complete', 'partial', 'failed'}
CACHE = {'live', 'cached', 'unknown'}
MAX_FILE_BYTES = 8 * 1024 * 1024


def text(value, label, limit=2048):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(label + ': expected non-empty bounded text')
    if any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f or c in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069' for c in value):
        raise ValueError(label + ': control characters are not allowed')
    return value


def timestamp(value, label):
    text(value, label, 64)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('timezone is required')
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(label + ': invalid timestamp with timezone') from exc


def domain_ascii(value):
    """Accept explicit ASCII DNS names; never infer a registrable apex."""
    text(value, 'domain_ascii', 253)
    if not value.isascii() or value != value.lower() or value.endswith('.'):
        raise ValueError('domain_ascii: use lowercase ASCII/punycode without a final dot')
    labels = value.split('.')
    if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in labels):
        raise ValueError('domain_ascii: expected a DNS name without scheme, path or port')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError('domain_ascii: an IP address is not a domain')
    for label in labels:
        if label.startswith('xn--'):
            try:
                if label.encode('ascii').decode('idna').encode('idna').decode('ascii') != label:
                    raise ValueError('non-canonical punycode')
            except (UnicodeError, ValueError) as exc:
                raise ValueError('domain_ascii: invalid punycode') from exc
    return value


def choice(value, values, label):
    if not isinstance(value, str) or value not in values:
        raise ValueError(label + ': unsupported value')
    return value


def ids(value, label, allow_empty=True):
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(label + ': expected a list')
    for item in value:
        text(item, label + ' item', 200)
    if len(value) != len(set(value)):
        raise ValueError(label + ': duplicate values')
    return value


def index_rows(case, section, errors):
    rows = case.get(section, [])
    if not isinstance(rows, list):
        errors.append({'section': section, 'error': 'expected a list'})
        return {}
    counts = Counter(row['id'] for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str))
    indexed = {}
    for number, row in enumerate(rows):
        try:
            if not isinstance(row, dict):
                raise ValueError('expected an object')
            key = text(row.get('id'), section + '.id', 200)
            if counts[key] != 1:
                raise ValueError('duplicate id; all conflicting rows quarantined')
            indexed[key] = row
        except ValueError as exc:
            errors.append({'section': section, 'row': number, 'error': str(exc)})
    return indexed


def reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON member')
        result[key] = value
    return result


def finite_float(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError('non-finite JSON number')
    return value


def reject_constant(_):
    raise ValueError('non-standard JSON number')


def read_json(path):
    # Bounded binary read avoids loading an arbitrarily large file into memory.
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('input file exceeds 8 MiB')
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=reject_duplicate_pairs,
                      parse_constant=reject_constant, parse_float=finite_float)


def assess(case, now):
    if not isinstance(case, dict):
        raise ValueError('case must be an object')
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now requires timezone')
    now = now.astimezone(timezone.utc)
    report = {'case_id': case.get('case_id'), 'read_only': True,
              'authorizes_submission': False, 'requires_live_verification': True,
              'results': [], 'errors': [], 'warnings': []}
    block = case.get('investigation')
    if block is None:
        report['state'] = 'no_records'
        return report
    if not isinstance(block, dict) or block.get('version') != '1.0':
        report['errors'].append({'section': 'investigation', 'error': 'expected version 1.0 object'})
        report['state'] = 'invalid_input'
        return report
    text(case.get('case_id'), 'case_id', 200)
    targets = index_rows(case, 'targets', report['errors'])
    evidence = index_rows(case, 'evidence', report['errors'])
    results = index_rows(block, 'results', report['errors'])
    for key, row in results.items():
        try:
            target_id = text(row.get('target_id'), 'target_id', 200)
            if target_id not in targets:
                raise ValueError('target_id: unknown or quarantined target')
            check_identity(case, row, 'investigation result', {target_id})
            check_identity(case, targets[target_id], 'target')
            name = domain_ascii(row.get('domain_ascii'))
            if targets[target_id].get('normalized_domain') != name:
                raise ValueError('domain_ascii does not match the recorded target')
            queried = timestamp(row.get('queried_at'), 'queried_at')
            if queried > now:
                raise ValueError('queried_at is in the future')
            expiry = row.get('expires_at')
            expires = timestamp(expiry, 'expires_at') if expiry is not None else None
            if expires is not None and expires < queried:
                raise ValueError('expires_at precedes queried_at')
            freshness = 'unknown' if expires is None else ('stale' if expires <= now else 'current')
            source = row.get('source')
            if not isinstance(source, dict):
                raise ValueError('source: expected an object')
            source_name = text(source.get('name'), 'source.name', 200)
            reference = text(source.get('reference'), 'source.reference')
            coverage = choice(source.get('coverage'), COVERAGE, 'source.coverage')
            cache = choice(source.get('cache'), CACHE, 'source.cache')
            limits = ids(source.get('limitations'), 'source.limitations')
            if coverage != 'complete' and not limits:
                raise ValueError('incomplete coverage requires limitations')
            registration = choice(row.get('registration'), REGISTRATION, 'registration')
            relationship = choice(row.get('relationship'), RELATIONSHIP, 'relationship')
            signals = ids(row.get('signals'), 'signals')
            if any(signal not in SIGNALS for signal in signals):
                raise ValueError('signals: unsupported signal')
            evidence_ids = ids(row.get('evidence_ids'), 'evidence_ids')
            if any(item not in evidence for item in evidence_ids):
                raise ValueError('evidence_ids: unknown or quarantined evidence')
            evidence_closure(case, evidence, evidence_ids, {target_id})
            asserted = registration != 'unknown' or relationship != 'unknown' or bool(signals)
            if asserted and not evidence_ids:
                raise ValueError('recorded assertions require evidence references')
            if coverage == 'failed' and asserted:
                raise ValueError('failed query cannot establish new assertions')
            if coverage == 'failed':
                next_step = 'retry_source'
            elif freshness != 'current':
                next_step = 'refresh_source'
            elif relationship in {'owned', 'authorized'}:
                next_step = 'review_known_asset' if signals else 'exclude_known_asset'
            elif registration != 'registered':
                next_step = 'verify_registration'
            elif signals:
                next_step = 'review_evidence'
            else:
                next_step = 'monitor'
            report['results'].append({
                'id': key, 'target_id': target_id, 'domain_ascii': name,
                'registration': registration, 'relationship': relationship,
                'signals': list(signals), 'evidence_ids': list(evidence_ids),
                'source': {'name': source_name, 'reference': reference, 'coverage': coverage,
                           'cache': cache, 'limitations': list(limits)},
                'queried_at': queried.isoformat(), 'freshness': freshness, 'next_step': next_step,
                'authorizes_submission': False,
            })
        except (ValueError, TypeError, OverflowError) as exc:
            report['errors'].append({'section': 'investigation.results', 'id': key, 'error': str(exc)})
    report['results'].sort(key=lambda row: row['id'])
    report['state'] = 'needs_repair' if report['errors'] else 'review_only'
    report['warnings'].append('Recorded scope only; no matches is not proof of absence or safety.')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, action='append', type=Path)
    parser.add_argument('--now')
    args = parser.parse_args()
    try:
        now = timestamp(args.now, 'now') if args.now else datetime.now(timezone.utc)
    except ValueError as exc:
        print(json.dumps({'cases': [], 'errors': [str(exc)]}, ensure_ascii=True))
        return 2
    report = {'read_only': True, 'cases': [], 'errors': []}
    for path in args.case:
        try:
            result = assess(read_json(path), now)
            report['cases'].append(result)
            report['errors'].extend(result['errors'])
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
            report['errors'].append({'section': 'file', 'error': str(exc)})
    print(json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
