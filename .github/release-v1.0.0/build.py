"""Build only audited, generic Skill files from one immutable source commit."""
import hashlib
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

SOURCE = '400e3a00710474d8a321dcd06b1148d910391609'
TAG = 'v1.0.0'
REPO = 'zlxmygit/brand-protection-skills'
PREFIX = 'skills/brand-protection/'
ALLOW = {
    'LICENSE': '1be6a484d51ffe726f356a51a8f494eebf44f8c6',
    'SKILL.md': 'f3b55c019e45b2a735220e0c8e88d786676dae64',
    'agents/openai.yaml': '024c08d7ebb13bcefaba23eeeb7528723ba538df',
    'assets/case-record.json': '4e6d687a9e729971bdea4418a58635f7529e4bbb',
    'assets/complaint-templates.md': '33ce8031f7a5da355d80dc5cf980910d24f625b0',
    'assets/follow-up-policy.json': '03a4ab375461d05a84015ca09b3f0b083a6c3283',
    'assets/intake-form.md': '3fe1ac90f570b10091cbe4c1178858abd6e9bf96',
    'assets/monitoring-prompt.md': '02ffb59ec42ba64c8852e66cd5c32cea51384952',
    'references/case-format.md': 'cc2056384280748ac34af46b588f0ea97f5eddce',
    'references/evidence.md': 'd8bf52bb6b97de6d261756a0a3b47dbf3edc8cc1',
    'references/follow-up.md': 'f76da4dbe3bf961e61b7eed1fa1058c4f70f219d',
    'references/intake.md': '46411e9ad13f3bd2da917ec99d4f2db98368f253',
    'references/legal-regions.md': 'a4c7f72f99f4816bd14b859da3bdb5164f326ae9',
    'references/online-channels.md': 'a3929712f51d73f189528bef0734db2f4a25e41e',
    'references/patents-designs.md': '81836c67e99460364600d08872039196b1a80a12',
    'references/queue-safety.md': '5a1776d3cdc33bb6aa8bc092c6cc246c2b024881',
    'scripts/followup_queue.py': '77773443685e2d6b2f929fa2df2e8914bdefdaed',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=120, **kwargs).stdout


def git(source, *args):
    return run(['git', '-C', str(source), *args])


def suite(source, script):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
           'FOLLOWUP_QUEUE_SCRIPT': str(script.resolve())}
    proc = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                          cwd=source, env=env, capture_output=True, text=True, timeout=120)
    output = proc.stdout + proc.stderr
    print(output)
    count = re.search(r'^Ran (\d+) tests? in ', output, re.M)
    require(proc.returncode == 0 and count and int(count[1]) == 76, '76-test suite did not pass')
    return 76


def archive(path, files):
    # ZIP_STORED avoids compressor-version differences; this text-only bundle is small.
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as out:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo('brand-protection/' + name, (2026, 11, 12, 12, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            out.writestr(info, data)


def main():
    root = Path.cwd()
    source = root / 'source'
    tooling = Path(__file__).resolve().parent
    require(git(source, 'rev-parse', 'HEAD').decode().strip() == SOURCE, 'Source SHA changed')
    rows = git(source, 'ls-tree', '-r', '-z', SOURCE, '--', PREFIX).split(b'\0')
    observed = {}
    for row in filter(None, rows):
        metadata, path = row.decode().split('\t', 1)
        mode, kind, sha = metadata.split()
        require(mode == '100644' and kind == 'blob', 'Unexpected file mode')
        observed[path[len(PREFIX):]] = sha
    require(observed == ALLOW, 'Audited Skill whitelist differs from source tree')
    files = {}
    for name, sha in ALLOW.items():
        data = git(source, 'show', SOURCE + ':' + PREFIX + name)
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        require(blob == sha, 'Source blob mismatch: ' + name)
        files[name] = data
    require(files['LICENSE'] == git(source, 'show', SOURCE + ':LICENSE'), 'License mismatch')
    require(files['SKILL.md'].startswith(b'---\nname: brand-protection\n'), 'Skill frontmatter invalid')
    compile(files['scripts/followup_queue.py'].decode('utf-8'), 'followup_queue.py', 'exec')
    case = json.loads(files['assets/case-record.json'])
    require(case['schema_version'] == '1.3', 'Unexpected case schema')
    for key in ('case_id', 'brand', 'brand_id', 'created_at', 'updated_at'):
        require(case[key] is None, 'Filled case identifier: ' + key)
    for key in ('official_domains', 'markets', 'requested_outcomes', 'rights', 'targets',
                'allegations', 'observations', 'evidence', 'providers', 'actions', 'deadlines',
                'acceptance_checklist', 'missing_information', 'parties', 'material_sources',
                'materials', 'jurisdiction_research', 'followup_tasks', 'outbound_attempts', 'runs'):
        require(case[key] == [], 'Nonempty case data: ' + key)
    follow = case['follow_up']
    require(follow['requested'] is False and follow['enabled'] is False, 'Monitoring enabled')
    require(all(v is None for v in follow['authorizations'].values()), 'Stored authorization')
    require(follow['feedback_sources'] == [] and follow['processed_message_ids'] == []
            and follow['source_cursors'] == [], 'Stored feedback data')
    for name in ('INSTALL.md', 'RELEASE-NOTES.md'):
        files[name] = (tooling / name).read_bytes()
    patterns = (
        r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b',
        r'\bgithub_pat_[A-Za-z0-9_]{30,}\b',
        r'\bAKIA[A-Z0-9]{16}\b',
        r'/(?:Users|home)/[A-Za-z0-9_.-]+/',
    )
    links = 0
    for name, data in files.items():
        text = data.decode('utf-8')
        require(not any(re.search(pattern, text) for pattern in patterns), 'Sensitive pattern: ' + name)
        if name.endswith('.json'):
            json.loads(text)
        if name.endswith('.md'):
            for url in re.findall(r'\[[^\]]*\]\(([^\s)]+)\)', text):
                parts = urlsplit(url)
                if parts.scheme or parts.netloc or not parts.path:
                    continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parts.path)))
                require(target in files, 'Broken relative file link: ' + name + ' -> ' + target)
                links += 1
    manifest = {
        'release': TAG, 'source_commit': SOURCE, 'repository': REPO, 'case_schema_version': '1.3',
        'entrypoint': 'SKILL.md', 'license': 'MIT',
        'scope': 'Audited generic files and release documentation only; no populated cases.',
        'manifest_self_excluded': True,
        'files': [{'path': name, 'size': len(data), 'sha256': digest(data),
                   'source_git_blob': ALLOW.get(name)} for name, data in sorted(files.items())],
    }
    files['MANIFEST.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    require(len(files) == 20, 'Unexpected package inventory')
    source_count = suite(source, source / PREFIX / 'scripts/followup_queue.py')
    dist = root / 'dist'
    dist.mkdir(exist_ok=False)
    zip_path = dist / 'brand-protection-v1.0.0.zip'
    archive(zip_path, files)
    with tempfile.TemporaryDirectory(prefix='generic-skill-release-') as tmp:
        temp = Path(tmp)
        archive(temp / 'repeat.zip', files)
        require(zip_path.read_bytes() == (temp / 'repeat.zip').read_bytes(), 'Non-reproducible archive')
        with zipfile.ZipFile(zip_path) as built:
            require(built.testzip() is None, 'Archive CRC check failed')
            require(set(built.namelist()) == {'brand-protection/' + k for k in files}, 'Unexpected ZIP entries')
            require(len(built.namelist()) == len(files), 'Duplicate ZIP paths')
            for name, data in files.items():
                require(built.read('brand-protection/' + name) == data, 'Archive byte mismatch')
            built.extractall(temp / 'unpacked')
        installed = temp / 'unpacked/brand-protection'
        installed_count = suite(source, installed / 'scripts/followup_queue.py')
        smoke = json.loads(run([sys.executable, '-B', str(installed / 'scripts/followup_queue.py'),
                               '--case', str(installed / 'assets/case-record.json'),
                               '--now', '2026-11-12T12:00:00Z']))
        require(not smoke['errors'] and smoke['cases'][0]['state'] == 'not_enabled', 'Blank template smoke failed')
        for name, data in files.items():
            require((installed / name).read_bytes() == data, 'Tests changed installed data')
    for name in ('INSTALL.md', 'RELEASE-NOTES.md'):
        (dist / name).write_bytes(files[name])
    validation = {
        'release': TAG, 'source_commit': SOURCE, 'builder_commit': os.environ.get('GITHUB_SHA'),
        'workflow_url': 'https://github.com/' + REPO + '/actions/runs/' + os.environ.get('GITHUB_RUN_ID', 'local'),
        'python': platform.python_version(), 'platform': platform.system(),
        'source_tests_passed': source_count, 'unpacked_package_tests_passed': installed_count,
        'deterministic_field_mutations_per_suite': 1120, 'source_allowlist_files': len(ALLOW),
        'archive_files': len(files), 'relative_file_links_checked': links,
        'checks': {'source_blob_hashes': True, 'blank_case_template': True, 'monitoring_disabled': True,
                   'credential_pattern_scan': True, 'package_inventory': True, 'reproducible_zip': True,
                   'archive_crc': True, 'archive_bytes': True, 'blank_template_smoke': True,
                   'installed_files_unchanged': True},
        'package': {'name': zip_path.name, 'size': zip_path.stat().st_size, 'sha256': digest(zip_path.read_bytes())},
        'limitations': 'No real complaints, accounts, schedules or live legal outcomes were tested. Pattern scans are not an absolute privacy guarantee.',
    }
    (dist / 'VALIDATION.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    names = ('brand-protection-v1.0.0.zip', 'INSTALL.md', 'VALIDATION.json')
    sums = ''.join(digest((dist / name).read_bytes()) + '  ' + name + '\n' for name in names)
    (dist / 'SHA256SUMS.txt').write_text(sums, encoding='ascii')
    with (dist / 'RELEASE-NOTES.md').open('a', encoding='utf-8') as out:
        out.write('\n## 本次制品校验\n\n发布门禁已通过；源码与解压包分别通过 76 项测试。\n\n')
        out.write('ZIP SHA-256：`' + validation['package']['sha256'] + '`\n\n')
        out.write('[构建与验收记录](' + validation['workflow_url'] + ')。\n')
    print(json.dumps(validation, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
