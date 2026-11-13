#!/usr/bin/env python3
"""Export explicitly reviewed evidence bytes. No network, redaction or sending."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import text, timestamp, read_json, index_rows
from capture_web import private_root, write_private
from filesystem_paths import checked_existing_path, validate_windows_path

from record_integrity import check_identity, evidence_closure

FILE_LIMIT = 10 * 1024 * 1024
TOTAL_LIMIT = 50 * 1024 * 1024


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Expected a lowercase SHA-256 value')
    return value


def read_evidence(root, location, expected_hash):
    """Read only a named regular file beneath the trusted private root."""
    digest(expected_hash)
    text(location, 'evidence path', 4096)
    candidate = Path(location)
    if '..' in candidate.parts:
        raise ValueError('Parent traversal is not allowed')
    if os.name == 'nt':
        validate_windows_path(location)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = checked_existing_path(candidate)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError('Evidence is outside the selected root') from exc
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError('Evidence must be a regular file')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(str(resolved), flags)
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('Evidence must remain a regular file')
        data = handle.read(FILE_LIMIT + 1)
    if len(data) > FILE_LIMIT:
        raise ValueError('Evidence exceeds the 10 MiB limit')
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError('Evidence bytes changed after review')
    return data


def prepare(case, review, source_root, now):
    if not isinstance(case, dict) or not isinstance(review, dict):
        raise ValueError('Case and disclosure review must be objects')
    case_id = text(case.get('case_id'), 'case_id', 200)
    if review.get('version') != '1.0' or review.get('case_id') != case_id:
        raise ValueError('Disclosure review does not match the case')
    check_identity(case, review, 'disclosure review')
    for name in ('id', 'recipient', 'purpose', 'reviewed_by', 'authorization_source'):
        text(review.get(name), name)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now requires timezone')
    now = now.astimezone(timezone.utc)
    approved = timestamp(review.get('approved_at'), 'approved_at')
    expires = timestamp(review.get('expires_at'), 'expires_at')
    if not approved <= now < expires:
        raise ValueError('Disclosure approval is future-dated or expired')
    rows = review.get('items')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Select between 1 and 100 evidence items')
    errors = []
    evidence = index_rows(case, 'evidence', errors)
    if errors:
        raise ValueError('Repair evidence IDs before exporting')
    root = private_root(source_root)
    selected, seen, total = [], set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Disclosure item must be an object')
        key = text(row.get('evidence_id'), 'evidence_id', 200)
        if key in seen or key not in evidence:
            raise ValueError('Unknown or repeated evidence selection')
        seen.add(key)
        evidence_closure(case, evidence, [key])
        check_identity(case, row, 'disclosure item')
        record = evidence[key]
        if row.get('disclosure_reviewed') is not True:
            raise ValueError('Each selected file needs an explicit disclosure review')
        expected = digest(row.get('sha256'))
        if digest(record.get('sha256')) != expected:
            raise ValueError('Review does not match the recorded evidence hash')
        if record.get('disclosure_class') not in {'public', 'restricted'}:
            raise ValueError('Unknown disclosure class')
        parent = record.get('derived_from_id')
        if row.get('derived_from_id') != parent:
            raise ValueError('Review does not match the copy lineage')
        chain, current = {key}, parent
        while current is not None:
            text(current, 'derived_from_id', 200)
            if current in chain or current not in evidence:
                raise ValueError('Invalid or cyclic copy lineage')
            chain.add(current)
            current = evidence[current].get('derived_from_id')
        if parent is None:
            if row.get('allow_original') is not True:
                raise ValueError('Original evidence is excluded unless explicitly approved')
        else:
            text(parent, 'derived_from_id', 200)
            if parent == key or parent not in evidence:
                raise ValueError('Derived copy must reference different evidence in this case')
        data = read_evidence(root, record.get('original_path'), expected)
        total += len(data)
        if total > TOTAL_LIMIT:
            raise ValueError('Selected evidence exceeds the 50 MiB limit')
        selected.append((key, parent, expected, data))
    selected.sort(key=lambda item: item[0])
    manifest = {'version': '1.0', 'case_id': case_id, 'files': []}
    files = {}
    for number, (key, parent, expected, data) in enumerate(selected, 1):
        name = 'files/%04d.bin' % number
        files[name] = data
        manifest['files'].append({'path': name, 'evidence_id': key, 'derived_from_id': parent,
                                  'sha256': expected, 'size': len(data)})
    files['MANIFEST.json'] = json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2).encode('ascii')
    return files, manifest


def build_archive(files):
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 11, 13, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            handle.writestr(info, data)
    data = memory.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as handle:
        if handle.testzip() is not None or set(handle.namelist()) != set(files):
            raise ValueError('Export archive validation failed')
        for name, expected in files.items():
            if handle.read(name) != expected:
                raise ValueError('Exported bytes differ from approved bytes')
    return data


def export(case, review, source_root, output_root, now=None):
    root = private_root(output_root)
    files, manifest = prepare(case, review, source_root, datetime.now(timezone.utc) if now is None else now)
    data = build_archive(files)
    destination = Path(tempfile.mkdtemp(prefix='export-', dir=root))
    try:
        path = destination/'evidence.zip'
        write_private(path, data)
        expected = hashlib.sha256(data).hexdigest()
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Stored export hash mismatch')
        receipt = {'version': '1.0', 'case_id': case['case_id'], 'review_id': review['id'],
                   'recipient': review['recipient'], 'package': str(path), 'sha256': expected,
                   'file_count': len(manifest['files']), 'authorizes_submission': False,
                   'writes_case': False, 'limitations': ['Review records are not authenticated identities or legal conclusions',
                                                       'No automatic redaction; approved bytes are copied unchanged']}
        write_private(destination/'export-receipt.json', json.dumps(receipt, ensure_ascii=True, indent=2).encode('ascii'))
        return receipt
    except BaseException:
        shutil.rmtree(destination)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = export(read_json(args.case), read_json(args.review), args.source_root, args.output_root)
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({'errors': ['export_validation_failed'], 'authorizes_submission': False}))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
