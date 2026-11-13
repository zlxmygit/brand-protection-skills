"""Probe documented public examples; no private cases or credentials."""
import argparse
from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--scripts', type=Path, required=True)
parser.add_argument('--checks', nargs='+', required=True, choices=['rdap'])
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
source = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip()
print(json.dumps({'source_commit': source, 'checks': checks}))
