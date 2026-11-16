"""Install a release with native copy tools into an isolated Skill collection.

This checks entry files and Python execution, not discovery by a running agent host.
No user Skill directory, private case, network service or scheduler is accessed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

CLI = ('followup_queue', 'investigation', 'rdap_lookup', 'capture_web', 'evidence_export',
       'submission_review', 'channel_check', 'case_dashboard', 'consumer_materials')


def expected_files(manifest):
    records = manifest.get('files')
    if not isinstance(records, list) or not records:
        raise ValueError('installation manifest has no files')
    files = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('invalid installation manifest entry')
        name, digest = record.get('path'), record.get('sha256')
        if (not isinstance(name, str) or not name.startswith('brand-protection/')
                or '\\' in name or '..' in Path(name).parts or Path(name).is_absolute()
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or name in files):
            raise ValueError('unsafe or duplicate installation manifest entry')
        files[name] = digest
    return files


def verify_archive(archive, files):
    with zipfile.ZipFile(archive) as handle:
        if len(handle.namelist()) != len(files) or set(handle.namelist()) != set(files):
            raise ValueError('native installation archive inventory mismatch')
        for item in handle.infolist():
            if item.is_dir() or ((item.external_attr >> 16) & 0o170000) not in (0, 0o100000):
                raise ValueError('native installation archive must contain regular files')
            if hashlib.sha256(handle.read(item)).hexdigest() != files[item.filename]:
                raise ValueError('native installation archive byte mismatch')


def verify_files(collection, files):
    skill = collection/'brand-protection'
    actual = set()
    for path in skill.rglob('*'):
        if path.is_symlink():
            raise ValueError('native installation contains a symbolic link')
        if path.is_file():
            name = path.relative_to(collection).as_posix()
            actual.add(name)
            if name not in files or hashlib.sha256(path.read_bytes()).hexdigest() != files[name]:
                raise ValueError('native installation file byte mismatch')
    if actual != set(files):
        raise ValueError('native installation file inventory mismatch')
    return skill


def validate_entry_files(skill):
    text = (skill/'SKILL.md').read_text(encoding='utf-8')
    match = re.match(r'\A---\r?\n(.*?)\r?\n---\r?\n', text, re.DOTALL)
    if match is None:
        raise ValueError('installed Skill has no readable front matter')
    fields = re.findall(r'^([a-z_]+):[ \t]*(.+)$', match.group(1), re.MULTILINE)
    if len({name for name, _ in fields}) != len(fields):
        raise ValueError('installed Skill has duplicate front matter fields')
    metadata = dict(fields)
    if metadata.get('name', '').strip('\"\'') != 'brand-protection' or not metadata.get('description', '').strip():
        raise ValueError('installed Skill entry metadata is invalid')
    agent = (skill/'agents/openai.yaml').read_text(encoding='utf-8')
    if not re.search(r'^interface:\s*$', agent, re.MULTILINE):
        raise ValueError('installed agent interface is unavailable')
    for field in ('display_name', 'short_description', 'default_prompt'):
        values = re.findall(r'^  '+field+r':[ \t]*(.+)$', agent, re.MULTILINE)
        if len(values) != 1 or not values[0].strip('\"\' '):
            raise ValueError('installed agent interface field is invalid')
        if field == 'default_prompt' and '$brand-protection' not in values[0]:
            raise ValueError('installed agent prompt does not name the Skill')
    checked = 0
    for path in skill.rglob('*.md'):
        for match in re.finditer(r'\[[^\]]*\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
            target = match.group(1).split('#', 1)[0]
            if not target or '://' in target or target.startswith('mailto:'):
                continue
            destination = (path.parent/target).resolve()
            try:
                destination.relative_to(skill.resolve())
            except ValueError as exc:
                raise ValueError('installed entry link escapes the Skill') from exc
            if not destination.is_file():
                raise ValueError('installed entry link is unavailable')
            checked += 1
    return checked


def run_checked(command, cwd, env):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            encoding='utf-8', timeout=60)
    if result.returncode:
        # Do not include arbitrary tool output or temporary/private paths in attestations.
        raise RuntimeError('native installation command failed with exit '+str(result.returncode))
    return result


def check_execution(skill, cwd, env):
    entrypoints = [*CLI]
    if (skill/'scripts/host_adapter.py').is_file():
        entrypoints.append('host_adapter')
    for name in entrypoints:
        result = run_checked([sys.executable, '-B', str(skill/'scripts'/(name+'.py')), '--help'], cwd, env)
        if 'usage:' not in result.stdout:
            raise ValueError('installed CLI help is unavailable: '+name)
    result = run_checked([sys.executable, '-B', str(skill/'scripts/followup_queue.py'),
                          '--case', str(skill/'assets/case-record.json')], cwd, env)
    queue = json.loads(result.stdout)
    if (queue.get('queue_only') is not True or queue.get('errors') != []
            or not isinstance(queue.get('cases'), list) or len(queue['cases']) != 1):
        raise ValueError('installed blank queue check failed')
    case = queue['cases'][0]
    if case.get('state') != 'not_enabled' or case.get('errors') != [] or case.get('due_tasks') != []:
        raise ValueError('installed template is not an idle blank queue')
    return entrypoints


def install_one(archive, files, root, shell=None):
    collection = root/'原生 宿主' / '.agents/skills'
    stage = root/'解压 暂存'
    collection.mkdir(parents=True)
    env = {**os.environ, 'PYTHONIOENCODING':'utf-8', 'PYTHONDONTWRITEBYTECODE':'1',
           'BP_ARCHIVE':str(archive), 'BP_STAGE':str(stage),
           'BP_COLLECTION':str(collection), 'BP_PYTHON':sys.executable}
    if shell is not None:
        command = (
            "$ErrorActionPreference = 'Stop'; "
            'Expand-Archive -LiteralPath $env:BP_ARCHIVE -DestinationPath $env:BP_STAGE; '
            "Copy-Item -LiteralPath (Join-Path $env:BP_STAGE 'brand-protection') "
            "-Destination (Join-Path $env:BP_COLLECTION 'brand-protection') -Recurse"
        )
        run_checked([shell, '-NoProfile', '-NonInteractive', '-Command', command], root, env)
        method = {'shell':Path(shell).name, 'extract':'Expand-Archive', 'copy':'Copy-Item -Recurse'}
    else:
        run_checked([sys.executable, '-B', '-m', 'zipfile', '-e', str(archive), str(stage)], root, env)
        copy = shutil.which('cp')
        if not copy:
            raise RuntimeError('native cp is unavailable')
        run_checked([copy, '-R', str(stage/'brand-protection'), str(collection/'brand-protection')], root, env)
        method = {'shell':'native', 'extract':'python -m zipfile -e', 'copy':'cp -R'}
    skill = verify_files(collection, files)
    links = validate_entry_files(skill)
    entrypoints = check_execution(skill, root, env)
    verify_files(collection, files)
    return {**method, 'files_compared':len(files), 'relative_links_checked':links,
            'cli_entrypoints':entrypoints, 'blank_queue_passed':True, 'entry_files_parsed':True}


def smoke_install(archive, manifest):
    archive = Path(archive).resolve()
    files = expected_files(manifest)
    verify_archive(archive, files)
    methods = []
    with tempfile.TemporaryDirectory(prefix='brand-native-install-') as temporary:
        root = Path(temporary)
        if os.name == 'nt':
            shells = list(dict.fromkeys(value for name in ('powershell', 'pwsh')
                                        for value in [shutil.which(name)] if value))
            if not shells:
                raise RuntimeError('native Windows installation requires PowerShell')
            for index, shell in enumerate(shells):
                methods.append(install_one(archive, files, root/str(index), shell))
        else:
            methods.append(install_one(archive, files, root/'posix'))
    return {'passed':True, 'checked_at':datetime.now(timezone.utc).isoformat(),
            'system':platform.system(), 'machine':platform.machine(),
            'python':platform.python_version(), 'methods':methods,
            'host_runtime_loaded':False,
            'scope':'Isolated native extraction/copy, byte comparison, entry-file parsing and Python execution only.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    print(json.dumps(smoke_install(args.archive, manifest)))


if __name__ == '__main__':
    main()
