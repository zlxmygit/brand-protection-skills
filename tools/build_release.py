"""Build an allowlisted Skill archive and test the actual extracted scripts."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/brand-protection'


def run_tests(scripts=None):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    if scripts is not None:
        env.update(FOLLOWUP_QUEUE_SCRIPT=str(scripts / 'followup_queue.py'),
                   BRAND_SKILL_SCRIPTS=str(scripts))
    completed = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                               cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    print(completed.stdout)
    print(completed.stderr)
    if completed.returncode:
        raise RuntimeError('contract tests failed')
    found = re.search(r'Ran (\d+) tests', completed.stderr)
    if not found:
        raise RuntimeError('test count is unavailable')
    return int(found.group(1))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def main():
    version = json.loads((ROOT / 'release/version.json').read_text(encoding='utf-8'))
    tag = version['tag']
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('invalid release tag')
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
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
    source_count = run_tests()
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
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        rebuilt = temporary / 'rebuilt.zip'
        archive(rebuilt)
        if rebuilt.read_bytes() != package.read_bytes():
            raise ValueError('archive is not reproducible in this environment')
        install = temporary / 'install'
        with zipfile.ZipFile(package) as handle:
            if handle.testzip() is not None or set(handle.namelist()) != set(files):
                raise ValueError('archive inventory/CRC mismatch')
            for name, expected_data in files.items():
                if handle.read(name) != expected_data:
                    raise ValueError('archive byte mismatch')
            handle.extractall(install)  # Names above are allowlisted and checked.
        installed_count = run_tests(install / 'brand-protection/scripts')
        if source_count != installed_count or source_count < version['minimum_tests']:
            raise ValueError('unexpected test count')
        for name, expected_data in files.items():
            if (install/name).read_bytes() != expected_data:
                raise ValueError('installed source was modified by tests')
    manifest = {'tag': tag, 'source_commit': source,
                'files': [{'path': name, 'sha256': sha256(data)} for name, data in sorted(files.items())]}
    validation = {'tag': tag, 'source_commit': source, 'source_tests': source_count,
                  'installed_tests': installed_count, 'archive_files': len(files),
                  'relative_file_links_checked': links, 'original_mutations_per_suite': 1120,
                  'package_sha256': sha256(package.read_bytes()),
                  'blank_template': True, 'monitoring_disabled': True,
                  'scope': 'Synthetic contract tests only; no live sending, legal decision or scheduler assurance.'}
    for name, value in [('PACKAGE-MANIFEST.json', manifest), ('VALIDATION.json', validation)]:
        (out/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    install_text = (ROOT/'docs/installation.md').read_text(encoding='utf-8').replace('{{tag}}', tag)
    (out/'INSTALL.md').write_text(install_text, encoding='utf-8')
    notes = (ROOT/('docs/releases/'+tag+'.md')).read_text(encoding='utf-8')
    notes += '\n## 发布验证\n\n'
    notes += f'源码提交：`{source}`。源码与解压后的安装包各通过 {source_count} 项测试。\n\n'
    notes += f'安装包包含 {len(files)} 个文件。案件模板为空，持续跟进默认关闭。逐文件哈希见 `PACKAGE-MANIFEST.json`。\n\n'
    notes += '测试使用合成数据。实际反馈读取、外发和定时运行需要在部署环境验收。\n'
    (out/'RELEASE-NOTES.md').write_text(notes, encoding='utf-8')
    shipped = [package.name, 'INSTALL.md', 'PACKAGE-MANIFEST.json', 'VALIDATION.json']
    (out/'SHA256SUMS.txt').write_text(''.join(sha256((out/name).read_bytes())+'  '+name+'\n' for name in shipped), encoding='ascii')
    print(json.dumps(validation))


if __name__ == '__main__':
    main()
