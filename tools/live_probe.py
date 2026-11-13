"""Probe documented public examples; no private cases or credentials."""
import argparse
from pathlib import Path
import json
import subprocess
import tempfile
import hashlib
import sys

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--scripts', type=Path, required=True)
parser.add_argument('--checks', nargs='+', required=True, choices=['rdap', 'capture'])
args = parser.parse_args()
sys.path.insert(0, str(args.scripts.resolve()))
checks = []
if 'rdap' in args.checks:
    from rdap_lookup import lookup
    report = lookup('example.com', 'public-integration-probe', allow_network=True)
    if report['errors'] or report['result']['registration'] != 'registered':
        raise SystemExit('Public RDAP integration probe failed; publication must stop')
    checks.append({'name': 'rdap', 'passed': True, 'domain': 'example.com',
                   'checked_at': report['result']['queried_at'],
                   'response_sha256': report['evidence'][0]['sha256']})
if 'capture' in args.checks:
    from capture_web import capture
    with tempfile.TemporaryDirectory() as directory:
        report = capture('https://example.com/', 'public-integration-probe', directory,
                         ['example.com'], allow_network=True)
        data = Path(report['evidence']['original_path']).read_bytes()
        if report['observation']['http_status'] != 200 or b'Example Domain' not in data:
            raise SystemExit('Public HTTPS integration probe failed; publication must stop')
        if hashlib.sha256(data).hexdigest() != report['evidence']['sha256']:
            raise SystemExit('Public HTTPS evidence hash mismatch')
        checks.append({'name': 'capture', 'passed': True, 'domain': 'example.com',
                       'checked_at': report['evidence']['acquired_at'],
                       'response_sha256': report['evidence']['sha256']})
source = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip()
print(json.dumps({'source_commit': source, 'checks': checks}))
