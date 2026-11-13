"""Build an allowlisted Skill archive and test the actual extracted scripts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/brand-protection'


def test_summary(output):
    """Separate discovered, passed and platform-skipped tests; reject ambiguity."""
    output = output.replace('\r\n', '\n')
    found = re.search(r'^Ran (\d+) tests? in ', output, re.MULTILINE)
    status = re.search(r'^OK(?: \(skipped=(\d+)\))?$', output, re.MULTILINE)
    if found is None or status is None or re.search(r'^FAILED', output, re.MULTILINE):
        raise RuntimeError('successful test summary is unavailable')
    total, skipped = int(found.group(1)), int(status.group(1) or 0)
    if total < 1 or skipped >= total:
        raise RuntimeError('invalid test or skip count')
    return {'discovered': total, 'passed': total - skipped, 'skipped': skipped}


def python_environment():
    return {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}


def run_tests(scripts=None):
    env = python_environment()
    # A caller's overrides must not turn the source run into another package run.
    for key in ('FOLLOWUP_QUEUE_SCRIPT', 'BRAND_SKILL_SCRIPTS'):
        env.pop(key, None)
    if scripts is not None:
        env.update(FOLLOWUP_QUEUE_SCRIPT=str(scripts / 'followup_queue.py'),
                   BRAND_SKILL_SCRIPTS=str(scripts))
    completed = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                               cwd=ROOT, env=env, capture_output=True, encoding='utf-8', timeout=120)
    print(completed.stdout)
    print(completed.stderr)
    if completed.returncode:
        raise RuntimeError('contract tests failed')
    return test_summary(completed.stderr)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_utf8(path, content):
    """Keep artifact text byte-identical across native newline conventions."""
    Path(path).write_bytes(content.encode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow required public integration probes')
    args = parser.parse_args()
    version = json.loads((ROOT / 'release/version.json').read_text(encoding='utf-8'))
    tag = version['tag']
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('invalid release tag')
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, encoding='ascii').strip()
    listed = json.loads((ROOT / 'release/skill-files.json').read_text(encoding='utf-8'))
    if len(listed) != len(set(listed)):
        raise ValueError('duplicate allowlist path')
    tracked = subprocess.check_output(['git', 'ls-tree', '-rz', 'HEAD', '--', 'skills/brand-protection'], cwd=ROOT)
    expected = {}
    for entry in tracked.split(b'\0'):
        if not entry:
            continue
        meta, name = entry.split(b'\t')
        mode, kind, blob = meta.decode().split()
        relative = name.decode().removeprefix('skills/brand-protection/')
        if mode != '100644' or kind != 'blob':
            raise ValueError('only regular source files may ship')
        expected[relative] = blob
    if set(expected) != set(listed):
        raise ValueError('tracked Skill files differ from reviewed allowlist')
    files = {}
    for name in sorted(listed):
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name:
            raise ValueError('unsafe archive path')
        source_path = SKILL / path
        if source_path.is_symlink():
            raise ValueError('symbolic links may not ship')
        data = source_path.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if blob != expected[name]:
            raise ValueError('worktree content differs from committed source')
        decoded = data.decode('utf-8')
        if re.search(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', decoded):
            raise ValueError('credential-shaped text detected')
        files['brand-protection/' + name] = data
    template = json.loads(files['brand-protection/assets/case-record.json'])
    for field in ('brand', 'brand_id', 'case_id', 'created_at', 'updated_at'):
        if template.get(field) is not None:
            raise ValueError('filled case template')
    for field in ('targets','evidence','rights','actions','deadlines','runs','outbound_attempts','materials','parties'):
        if template.get(field) != []:
            raise ValueError('case template contains records')
    if template['follow_up']['requested'] is not False or template['follow_up']['enabled'] is not False:
        raise ValueError('template enables monitoring')
    if files['brand-protection/LICENSE'] != (ROOT/'LICENSE').read_bytes():
        raise ValueError('standalone license differs')
    links = 0
    for name, data in files.items():
        if not name.endswith('.md'):
            continue
        for match in re.finditer(r'\[[^\]]*\]\(([^)]+)\)', data.decode('utf-8')):
            target = match.group(1).split('#', 1)[0]
            if not target or '://' in target or target.startswith('mailto:'):
                continue
            candidate = (ROOT/'skills'/Path(name).parent/target).resolve()
            try:
                relative = candidate.relative_to(SKILL.resolve()).as_posix()
            except ValueError as exc:
                raise ValueError('installed documentation link escapes Skill') from exc
            if relative not in listed:
                raise ValueError('broken installed file link: ' + relative)
            links += 1
    if version.get('required_live_checks') and not args.live:
        raise ValueError('This release requires --live public integration validation')
    source_result = run_tests()
    out = ROOT / 'dist'
    out.mkdir(exist_ok=True)
    package = out / ('brand-protection-' + tag + '.zip')

    def archive(path):
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name, (2026, 11, 13, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                handle.writestr(info, data)
    archive(package)
    live_report = {'checks': []}
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        rebuilt = temporary / 'rebuilt.zip'
        archive(rebuilt)
        if rebuilt.read_bytes() != package.read_bytes():
            raise ValueError('archive is not reproducible in this environment')
        install = temporary / 'Skill 安装'
        with zipfile.ZipFile(package) as handle:
            if handle.testzip() is not None or set(handle.namelist()) != set(files):
                raise ValueError('archive inventory/CRC mismatch')
            for name, expected_data in files.items():
                if handle.read(name) != expected_data:
                    raise ValueError('archive byte mismatch')
            handle.extractall(install)  # Names above are allowlisted and checked.
        installed_result = run_tests(install / 'brand-protection/scripts')
        if source_result != installed_result or source_result['discovered'] < version['minimum_tests']:
            raise ValueError('unexpected source/installed test counts')
        for name, expected_data in files.items():
            if (install/name).read_bytes() != expected_data:
                raise ValueError('installed source was modified by tests')
        if version.get('required_live_checks'):
            completed = subprocess.run([sys.executable, '-B', str(ROOT/'tools/live_probe.py'),
                '--scripts', str(install/'brand-protection/scripts'),
                '--checks', *version['required_live_checks']], cwd=ROOT, env=python_environment(),
                capture_output=True, encoding='utf-8', timeout=90)
            if completed.returncode:
                raise RuntimeError('Public integration validation failed: ' + completed.stderr)
            live_report = json.loads(completed.stdout)
            if live_report.get('source_commit') != source or any(c.get('passed') is not True for c in live_report.get('checks', [])):
                raise ValueError('Invalid live verification report')
            if {c.get('name') for c in live_report.get('checks', [])} != set(version['required_live_checks']):
                raise ValueError('Missing required live check')
    manifest = {'tag': tag, 'source_commit': source,
                'files': [{'path': name, 'sha256': sha256(data)} for name, data in sorted(files.items())]}
    validation = {'tag': tag, 'source_commit': source,
                  'source_tests': source_result['discovered'], 'installed_tests': installed_result['discovered'],
                  'source_result': source_result, 'installed_result': installed_result,
                  'environment': {'system': platform.system(), 'release': platform.release(),
                                  'machine': platform.machine(), 'python': platform.python_version()},
                  'ci_run_id': os.environ.get('GITHUB_RUN_ID'), 'archive_files': len(files),
                  'relative_file_links_checked': links, 'original_mutations_per_suite': 1120,
                  'package_sha256': sha256(package.read_bytes()),
                  'blank_template': True, 'monitoring_disabled': True, 'live_checks': live_report['checks'],
                  'scope': 'Synthetic contracts and listed public probes only; no live sending, legal decision or scheduler assurance.'}
    for name, value in [('PACKAGE-MANIFEST.json', manifest), ('VALIDATION.json', validation)]:
        write_utf8(out/name, json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    install_text = (ROOT/'docs/installation.md').read_text(encoding='utf-8').replace('{{tag}}', tag)
    write_utf8(out/'INSTALL.md', install_text)
    notes = (ROOT/('docs/releases/'+tag+'.md')).read_text(encoding='utf-8')
    notes += '\n## 发布构建验证\n\n'
    notes += f'源码提交：`{source}`。本构建在 {platform.system()} / {platform.machine()} / Python {platform.python_version()} 执行。\n\n'
    notes += (f'源码与解压包各发现 {source_result["discovered"]} 项测试，'
              f'各通过 {source_result["passed"]} 项，跳过 {source_result["skipped"]} 项平台专属测试。'
              '其他原生系统的执行结果见同提交的 GitHub Actions 任务，不能把跳过项计作通过。\n\n')
    notes += f'安装包包含 {len(files)} 个文件。案件模板为空，持续跟进默认关闭。逐文件哈希见 `PACKAGE-MANIFEST.json`。\n\n'
    notes += '回归测试使用合成数据。已执行的公开接口检查见 `VALIDATION.json`。实际反馈读取、外发和定时运行需要在部署环境验收。\n'
    write_utf8(out/'RELEASE-NOTES.md', notes)
    shipped = [package.name, 'INSTALL.md', 'PACKAGE-MANIFEST.json', 'VALIDATION.json']
    write_utf8(out/'SHA256SUMS.txt', ''.join(sha256((out/name).read_bytes())+'  '+name+'\n' for name in shipped))
    print(json.dumps(validation))


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    main()
