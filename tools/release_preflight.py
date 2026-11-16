"""Fail-closed release preflight. Only an HTTP 404 means absent."""
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(path):
    request = Request('https://api.github.com/' + path, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'Accept': 'application/vnd.github+json', 'User-Agent': 'brand-protection-release'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('GitHub response exceeds limit')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('Unexpected GitHub response')
            return value
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError('GitHub request failed with HTTP ' + str(exc.code)) from exc
    except URLError as exc:
        raise RuntimeError('GitHub request unavailable') from exc


def preflight(repository, tag, source, fetch=get_json):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('Invalid release tag')
    if not re.fullmatch(r'[0-9a-f]{40}', source):
        raise ValueError('Invalid source commit')
    prefix = 'repos/' + repository
    if fetch(prefix + '/releases/tags/' + tag) is not None:
        raise ValueError('Refusing to replace an existing release')
    existing = fetch(prefix + '/git/ref/tags/' + tag)
    if existing is not None:
        obj = existing.get('object', {})
        if not isinstance(obj, dict) or obj.get('type') != 'commit' or obj.get('sha') != source:
            raise ValueError('Tag does not match tested source')
    return {'tag_exists': existing is not None, 'tag': tag, 'source': source}


def local_release(source, root=None):
    """Do not publish artifacts made from another commit or dirty sidecar inputs."""
    root = ROOT if root is None else Path(root)
    if not re.fullmatch(r'[0-9a-f]{40}', source):
        raise ValueError('Invalid source commit')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, encoding='ascii').strip()
    status = subprocess.check_output(['git', 'status', '--porcelain=v1', '--untracked-files=all'], cwd=root)
    if head != source or status:
        raise ValueError('dirty source or source commit differs from tested HEAD')
    def committed(name):
        return subprocess.check_output(['git', 'show', source+':'+name], cwd=root)
    version = json.loads(committed('release/version.json'))
    tag = version['tag']
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('Invalid release tag')
    inputs = {name:committed(name) for name in (
        'release/version.json', 'release/skill-files.json', 'release/template-sha256.json',
        'docs/installation.md', 'docs/releases/'+tag+'.md', 'LICENSE')}
    digests = {name:hashlib.sha256(data).hexdigest() for name, data in inputs.items()}
    out = root/'dist'
    manifest = json.loads((out/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
    validation = json.loads((out/'VALIDATION.json').read_text(encoding='utf-8'))
    for value in (manifest, validation):
        if value.get('source_commit') != source or value.get('tag') != tag or value.get('source_inputs') != digests:
            raise ValueError('release sidecar source provenance mismatch')
    listed = json.loads(inputs['release/skill-files.json'])
    if not isinstance(listed, list) or len(listed) != len(set(listed)):
        raise ValueError('invalid committed Skill allowlist')
    expected_files = {}
    for name in listed:
        if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts or '\\' in name:
            raise ValueError('unsafe committed Skill path')
        data = committed('skills/brand-protection/'+name)
        expected_files['brand-protection/'+name] = hashlib.sha256(data).hexdigest()
    expected_manifest = [{'path':name, 'sha256':digest} for name, digest in sorted(expected_files.items())]
    if manifest.get('files') != expected_manifest:
        raise ValueError('package manifest differs from fixed source')
    with zipfile.ZipFile(out/('brand-protection-'+tag+'.zip')) as archive:
        if len(archive.namelist()) != len(expected_files) or set(archive.namelist()) != set(expected_files):
            raise ValueError('package archive differs from fixed source')
        for item in archive.infolist():
            if (((item.external_attr >> 16) & 0o170000) not in (0, 0o100000)
                    or hashlib.sha256(archive.read(item)).hexdigest() != expected_files[item.filename]):
                raise ValueError('package archive differs from fixed source')
    native = validation.get('native_installation', {})
    if native.get('passed') is not True or native.get('host_runtime_loaded') is not False:
        raise ValueError('native installation verification is unavailable')
    checks = validation.get('live_checks', [])
    if ({check.get('name') for check in checks} != set(version.get('required_live_checks', []))
            or any(check.get('passed') is not True for check in checks)):
        raise ValueError('required live checks are unavailable')
    install = inputs['docs/installation.md'].decode('utf-8').replace('{{tag}}', tag).encode('utf-8')
    if (out/'INSTALL.md').read_bytes() != install:
        raise ValueError('INSTALL sidecar differs from fixed source')
    notes = inputs['docs/releases/'+tag+'.md']
    if not (out/'RELEASE-NOTES.md').read_bytes().startswith(notes+b'\n## '):
        raise ValueError('release notes sidecar differs from fixed source')
    shipped = {'brand-protection-'+tag+'.zip', 'INSTALL.md', 'PACKAGE-MANIFEST.json',
               'VALIDATION.json', 'RELEASE-NOTES.md'}
    recorded = {}
    for line in (out/'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if match is None or match[2] in recorded:
            raise ValueError('invalid release checksum inventory')
        recorded[match[2]] = match[1]
    if set(recorded) != shipped:
        raise ValueError('release checksum inventory mismatch')
    for name, expected in recorded.items():
        if hashlib.sha256((out/name).read_bytes()).hexdigest() != expected:
            raise ValueError('release artifact checksum mismatch')
    return version


def main():
    try:
        version = local_release(os.environ['SOURCE'])
        result = preflight(os.environ['GH_REPO'], version['tag'], os.environ['SOURCE'])
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print('Release preflight failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
