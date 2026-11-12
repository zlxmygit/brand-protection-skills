"""Verify an already published same-source release; never replace its ZIP assets."""
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import stat
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

from build import ALLOW, PREFIX, REPO, SOURCE, TAG, digest, git, require, run, suite

RELEASE_ID = 442909129
ORIGINAL = {
    'brand-protection-v1.0.0.zip': 'dc5cffa47ddf3016c4e4e110829ceb0f405b615d0bf1279375f8e1903984be3c',
    'brand-protection-skills-v1.0.0.zip': '41caca7dbdd42c6dee048788bf2d7629e1d8ab629c9ac98de171537ab42c6cc6',
    'SHA256SUMS.txt': 'b0213060a77695b25187bdbc1c788d9a9b17121c578753f060c86df5499be305',
}


def gh(*args):
    return run(['gh', *args])


def release():
    value = json.loads(gh('api', 'repos/' + REPO + '/releases/tags/' + TAG))
    require(value['id'] == RELEASE_ID and not value['draft'] and not value['prerelease'], 'Release identity changed')
    tag = json.loads(gh('api', 'repos/' + REPO + '/git/ref/tags/' + TAG))
    require(tag['object']['type'] == 'commit' and tag['object']['sha'] == SOURCE, 'Tag source changed')
    for name, sha in ORIGINAL.items():
        matches = [a for a in value['assets'] if a['name'] == name]
        require(len(matches) == 1 and matches[0]['state'] == 'uploaded', 'Original asset missing')
        require(matches[0]['digest'] == 'sha256:' + sha, 'Original asset digest changed')
    return value


def check_zip(path, prefix, expected, destination):
    directories = {prefix.rstrip('/') + '/'}
    for name in expected:
        parent = posixpath.dirname(prefix + name)
        while parent:
            directories.add(parent + '/')
            parent = posixpath.dirname(parent)
    with zipfile.ZipFile(path) as z:
        require(len(z.namelist()) == len(set(z.namelist())), 'Duplicate ZIP entries')
        actual = {i.filename for i in z.infolist() if not i.is_dir()}
        require(actual == {prefix + name for name in expected}, 'Unexpected archive inventory')
        for item in z.infolist():
            require(not stat.S_ISLNK(item.external_attr >> 16), 'ZIP symlink forbidden')
            if item.is_dir():
                require(item.filename in directories, 'Unexpected directory')
            else:
                data = expected[item.filename[len(prefix):]]
                require(item.file_size == len(data), 'Archive entry size differs')
                require(z.read(item) == data, 'Archive entry differs from pinned source')
        require(z.testzip() is None, 'Archive CRC failed')
        z.extractall(destination)


def main():
    source = Path('source').resolve()
    require(git(source, 'rev-parse', 'HEAD').decode().strip() == SOURCE, 'Wrong source checkout')
    full = {}
    blobs = {}
    for row in filter(None, git(source, 'ls-tree', '-r', '-z', SOURCE).split(b'\0')):
        metadata, name = row.decode().split('\t', 1)
        mode, kind, sha = metadata.split()
        require(mode == '100644' and kind == 'blob', 'Unexpected source file mode')
        full[name] = git(source, 'show', SOURCE + ':' + name)
        blobs[name] = sha
    require(len(full) == 35, 'Unexpected repository inventory')
    skill = {name[len(PREFIX):]: data for name, data in full.items() if name.startswith(PREFIX)}
    require({name[len(PREFIX):]: sha for name, sha in blobs.items() if name.startswith(PREFIX)} == ALLOW,
            'Skill whitelist changed')
    current = release()
    original_dir = Path('published-assets')
    original_dir.mkdir()
    for name, sha in ORIGINAL.items():
        gh('release', 'download', TAG, '--repo', REPO, '--dir', str(original_dir), '--pattern', name)
        require(digest((original_dir / name).read_bytes()) == sha, 'Downloaded original hash mismatch')
    sums = {}
    for line in (original_dir / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        checksum, name = line.split(None, 1)
        sums[name.lstrip('*')] = checksum
    require(sums == {k: v for k, v in ORIGINAL.items() if k.endswith('.zip')}, 'Original checksum file differs')
    source_count = suite(source, source / PREFIX / 'scripts/followup_queue.py')
    with tempfile.TemporaryDirectory(prefix='published-generic-skill-') as tmp:
        temp = Path(tmp)
        check_zip(original_dir / 'brand-protection-v1.0.0.zip', 'brand-protection/', skill, temp / 'skill')
        check_zip(original_dir / 'brand-protection-skills-v1.0.0.zip', 'brand-protection-skills/', full, temp / 'repo')
        installed = temp / 'skill/brand-protection'
        installed_count = suite(source, installed / 'scripts/followup_queue.py')
        case = json.loads((installed / 'assets/case-record.json').read_text(encoding='utf-8'))
        require(case['case_id'] is None and case['brand'] is None and case['targets'] == [] and case['actions'] == [],
                'Populated case found')
        require(case['follow_up']['requested'] is False and case['follow_up']['enabled'] is False, 'Monitoring enabled')
        for name, data in skill.items():
            require((installed / name).read_bytes() == data, 'Tests changed installed files')
    links = 0
    for name, data in skill.items():
        if not name.endswith('.md'):
            continue
        for url in re.findall(r'\[[^\]]*\]\(([^\s)]+)\)', data.decode('utf-8')):
            parts = urlsplit(url)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parts.path)))
            require(target in skill, 'Broken installed relative link')
            links += 1
    additions = Path('release-additions')
    additions.mkdir()
    (additions / 'INSTALL.md').write_bytes(Path(__file__).with_name('INSTALL.md').read_bytes())
    report = {
        'release': TAG, 'release_id': RELEASE_ID, 'source_commit': SOURCE,
        'verification_commit': os.environ['GITHUB_SHA'],
        'workflow_url': 'https://github.com/' + REPO + '/actions/runs/' + os.environ['GITHUB_RUN_ID'],
        'python': platform.python_version(), 'platform': platform.system(),
        'source_tests_passed': source_count, 'downloaded_skill_tests_passed': installed_count,
        'field_mutations_per_suite': 1120, 'skill_files': len(skill), 'repository_files': len(full),
        'relative_file_links_checked': links,
        'original_assets': [{'name': name, 'sha256': sha, 'size': (original_dir / name).stat().st_size}
                            for name, sha in ORIGINAL.items()],
        'skill_manifest': [{'path': name, 'sha256': digest(data), 'git_blob': ALLOW[name]}
                           for name, data in sorted(skill.items())],
        'checks': {'tag_source': True, 'original_asset_download_hashes': True, 'original_checksums': True,
                   'skill_exact_source_bytes': True, 'repository_exact_source_bytes': True,
                   'no_extra_archive_files': True, 'no_symlinks': True, 'archive_crc': True,
                   'blank_case_template': True, 'monitoring_disabled': True, 'installed_files_unchanged': True},
        'publication_note': 'Release was published concurrently before this verifier. Original tag, ZIP assets and SHA256SUMS.txt were preserved; this run adds verification and installation documents only.',
        'limitations': 'Synthetic tests only. No real complaints, accounts, signatures, payments or business monitoring. Not a certification of live legal outcomes.',
    }
    (additions / 'VALIDATION.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sums = ''.join(digest((additions / name).read_bytes()) + '  ' + name + '\n' for name in ('INSTALL.md', 'VALIDATION.json'))
    (additions / 'VALIDATION-SHA256SUMS.txt').write_text(sums, encoding='ascii')
    current = release()
    names = ['INSTALL.md', 'VALIDATION.json', 'VALIDATION-SHA256SUMS.txt']
    require(not ({a['name'] for a in current['assets']} & set(names)), 'Supplement already exists; refusing overwrite')
    gh('release', 'upload', TAG, '--repo', REPO, *(str(additions / name) for name in names))
    verify_dir = Path('verified-additions')
    verify_dir.mkdir()
    for name in names:
        gh('release', 'download', TAG, '--repo', REPO, '--dir', str(verify_dir), '--pattern', name)
        require((verify_dir / name).read_bytes() == (additions / name).read_bytes(), 'Supplement download differs')
    current = release()
    marker = '<!-- supplemental-release-verification-v1.0.0 -->'
    require(marker not in (current['body'] or ''), 'Supplemental note already present')
    text = (current['body'] or '') + '\n\n' + marker + '\n## 补充下载复验与安装附件\n\n'
    text += '保留已发布的两个 ZIP、原 SHA256SUMS.txt 与源码标签，不替换原文件。'
    text += '下载后的独立 Skill 与 17 个固定源码文件逐字节一致，完整源码包与 35 个固定文件一致；'
    text += '源码及实际下载解压后的脚本分别通过 76 项测试（各含 1,120 次字段变异），45 个相对文件链接通过检查。\n\n'
    base = 'https://github.com/' + REPO + '/releases/download/' + TAG + '/'
    text += '[详细安装说明](' + base + 'INSTALL.md) · [补充验证报告](' + base + 'VALIDATION.json) · '
    text += '[补充附件校验值](' + base + 'VALIDATION-SHA256SUMS.txt)\n\n'
    text += '[本次验收运行](' + report['workflow_url'] + ')。安装与验证附件不写入原 ZIP；不含真实品牌材料或案件。\n'
    notes = Path('verified-release-notes.md')
    notes.write_text(text, encoding='utf-8')
    gh('release', 'edit', TAG, '--repo', REPO, '--notes-file', str(notes))
    final = release()
    require(marker in final['body'], 'Notes not saved')
    require(set(names) <= {a['name'] for a in final['assets']}, 'Supplement assets missing')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('VERIFIED_RELEASE', final['html_url'])
    print('ASSETS', [(a['name'], a['digest']) for a in final['assets']])


if __name__ == '__main__':
    main()
