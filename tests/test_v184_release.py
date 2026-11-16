"""Release input provenance, blank templates and real native installation boundaries."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('v184_release_builder', ROOT/'tools/build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
import native_install_smoke as native
spec = importlib.util.spec_from_file_location('v184_release_preflight', ROOT/'tools/release_preflight.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class CommittedReleaseInputs(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skill = self.root/'skills/brand-protection'
        shutil.copytree(SCRIPTS.parent, self.skill, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (self.root/'release').mkdir()
        (self.root/'docs/releases').mkdir(parents=True)
        (self.root/'LICENSE').write_bytes((self.skill/'LICENSE').read_bytes())
        (self.root/'.gitignore').write_bytes(b'/dist/\n')
        (self.root/'.gitattributes').write_bytes(b'* text=auto eol=lf\n')
        self.write_json('release/version.json', {'tag':'v1.8.4','previous':'v1.8.3',
                        'minimum_tests':1,'required_live_checks':[]})
        paths = sorted(p.relative_to(self.skill).as_posix() for p in self.skill.rglob('*') if p.is_file())
        self.write_json('release/skill-files.json', paths)
        self.write_json('release/template-sha256.json', {
            p.relative_to(self.skill).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.skill/'assets').rglob('*') if p.is_file()})
        (self.root/'docs/installation.md').write_bytes(b'Install {{tag}}.\n')
        (self.root/'docs/releases/v1.8.4.md').write_bytes(b'Release v1.8.4.\n')
        self.git('init', '-q')
        self.commit()

    def git(self, *arguments):
        return subprocess.check_output(['git', *arguments], cwd=self.root, stderr=subprocess.PIPE)

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'user.name=Release Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Synthetic release inputs')

    def write_json(self, path, value):
        (self.root/path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))

    def build(self, runner=None):
        with patch.object(builder, 'ROOT', self.root), patch.object(builder, 'SKILL', self.skill), \
             patch.object(builder, 'run_tests', side_effect=runner,
                          return_value={'discovered':1,'passed':1,'skipped':0}), \
             patch.object(sys, 'argv', ['build_release.py']), contextlib.redirect_stdout(io.StringIO()):
            builder.main()

    def test_dirty_version_allowlist_template_manifest_install_and_notes_are_rejected(self):
        for name in ('release/version.json', 'release/skill-files.json', 'release/template-sha256.json',
                     'docs/installation.md', 'docs/releases/v1.8.4.md'):
            with self.subTest(path=name):
                path = self.root/name
                original = path.read_bytes()
                path.write_bytes(original+b' ')
                try:
                    with self.assertRaisesRegex(ValueError, 'dirty'):
                        self.build()
                finally:
                    path.write_bytes(original)

    def test_untracked_input_cannot_silently_enter_a_release(self):
        (self.root/'unreviewed-input.txt').write_bytes(b'synthetic extra input\n')
        with self.assertRaisesRegex(ValueError, 'dirty'):
            self.build()

    def test_committed_filled_intake_is_rejected_without_reapproving_blank_template(self):
        intake = self.skill/'assets/intake-form.md'
        intake.write_bytes(intake.read_text(encoding='utf-8').replace('品牌名称：\n',
                           '品牌名称：SYNTHETIC-PRIVATE-BRAND\n').encode('utf-8'))
        self.commit()
        with self.assertRaisesRegex(ValueError, 'template'):
            self.build()

    def test_committed_template_addition_without_blank_baseline_is_rejected(self):
        (self.skill/'assets/unreviewed.json').write_bytes(b'{"case_id":"synthetic-private"}\n')
        listed = json.loads((self.root/'release/skill-files.json').read_text(encoding='utf-8'))
        self.write_json('release/skill-files.json', sorted([*listed, 'assets/unreviewed.json']))
        self.commit()
        with self.assertRaisesRegex(ValueError, 'template'):
            self.build()

    def test_release_sidecars_record_the_committed_input_hashes(self):
        self.build()
        manifest = json.loads((self.root/'dist/PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
        validation = json.loads((self.root/'dist/VALIDATION.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['source_commit'], self.git('rev-parse', 'HEAD').decode().strip())
        for name in ('release/version.json', 'release/skill-files.json', 'release/template-sha256.json',
                     'docs/installation.md', 'docs/releases/v1.8.4.md'):
            with self.subTest(path=name):
                expected = hashlib.sha256(self.git('show', 'HEAD:'+name)).hexdigest()
                self.assertEqual(manifest['source_inputs'][name], expected)
                self.assertEqual(validation['source_inputs'][name], expected)
        self.assertTrue(validation['native_installation']['passed'])
        self.assertFalse(validation['native_installation']['host_runtime_loaded'])
        self.assertEqual(preflight.local_release(manifest['source_commit'], self.root)['tag'], 'v1.8.4')

    def test_source_change_while_tests_run_is_rejected(self):
        def changed(*args):
            (self.root/'docs/installation.md').write_bytes(b'Changed during tests.\n')
            return {'discovered':1,'passed':1,'skipped':0}
        with self.assertRaisesRegex(ValueError, 'dirty'):
            self.build(changed)

    def test_new_head_while_tests_run_is_rejected(self):
        def changed(*args):
            (self.root/'docs/installation.md').write_bytes(b'Changed commit.\n')
            self.commit()
            return {'discovered':1,'passed':1,'skipped':0}
        with self.assertRaisesRegex(ValueError, 'commit changed'):
            self.build(changed)

    def test_native_installation_failure_stops_the_build(self):
        with patch.object(builder, 'smoke_install', side_effect=RuntimeError('native install failed')):
            with self.assertRaisesRegex(RuntimeError, 'native install failed'):
                self.build()
        self.assertFalse((self.root/'dist/VALIDATION.json').exists())

    def test_changed_download_sidecars_fail_before_release_network_requests(self):
        self.build()
        source = self.git('rev-parse', 'HEAD').decode().strip()
        for name in ('INSTALL.md', 'RELEASE-NOTES.md', 'PACKAGE-MANIFEST.json', 'VALIDATION.json',
                     'brand-protection-v1.8.4.zip'):
            with self.subTest(name=name):
                path = self.root/'dist'/name
                original = path.read_bytes()
                path.write_bytes(original+b' ')
                try:
                    with self.assertRaises(ValueError):
                        preflight.local_release(source, self.root)
                finally:
                    path.write_bytes(original)

    def test_recomputed_checksums_do_not_authorize_a_modified_archive(self):
        self.build()
        source = self.git('rev-parse', 'HEAD').decode().strip()
        archive = self.root/'dist/brand-protection-v1.8.4.zip'
        with zipfile.ZipFile(archive) as handle:
            files = {name:handle.read(name) for name in handle.namelist()}
        files['brand-protection/SKILL.md'] += b'\nUnreviewed synthetic change.\n'
        with zipfile.ZipFile(archive, 'w') as handle:
            for name, data in files.items():
                handle.writestr(name, data)
        sums = self.root/'dist/SHA256SUMS.txt'
        lines = sums.read_text(encoding='utf-8').splitlines()
        sums.write_bytes(('\n'.join(
            hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name
            if line.endswith('  '+archive.name) else line for line in lines)+'\n').encode('utf-8'))
        with self.assertRaisesRegex(ValueError, 'fixed source'):
            preflight.local_release(source, self.root)

    def check_release_version(self, version, accepted=False):
        self.write_json('release/version.json', {'minimum_tests':1,
                        'required_live_checks':[], **version})
        tag = version.get('tag')
        if isinstance(tag, str):
            (self.root/'docs/releases'/f'{tag}.md').write_bytes(f'Release {tag}.\n'.encode('utf-8'))
        self.commit()
        source = self.git('rev-parse', 'HEAD').decode().strip()
        with self.subTest(version=version, gate='build'):
            if accepted:
                self.build()
            else:
                with self.assertRaisesRegex(ValueError, 'release'):
                    self.build()
        with self.subTest(version=version, gate='preflight'):
            if accepted:
                self.assertEqual(preflight.local_release(source, self.root)['tag'], tag)
            else:
                with self.assertRaisesRegex(ValueError, 'release'):
                    preflight.local_release(source, self.root)

    def test_release_policy_rejects_major_or_minor_bumps(self):
        for tag in ('v2.0.0', 'v1.10.0'):
            self.check_release_version({'tag':tag, 'previous':'v1.8.3'})

    def test_release_policy_requires_exactly_one_patch(self):
        for tag, previous in (('v0.0.1', 'v0.0.0'), ('v1.8.10', 'v1.8.9')):
            self.check_release_version({'tag':tag, 'previous':previous}, accepted=True)
        for tag in ('v1.8.3', 'v1.8.2', 'v1.8.5'):
            self.check_release_version({'tag':tag, 'previous':'v1.8.3'})

    def test_release_policy_rejects_missing_or_malformed_versions(self):
        for version in (
            {'tag':'v1.8.4'},
            {'tag':'v1.8.4', 'previous':'1.8.3'},
            {'tag':'v1.8.4', 'previous':'v01.8.3'},
            {'tag':'v1.8.4', 'previous':'v1.8.3-rc.1'},
            {'tag':'v1.8.4', 'previous':None},
            {'tag':'v1.8.4', 'previous':183},
            {'previous':'v1.8.3'},
            {'tag':'v1.8.04', 'previous':'v1.8.3'},
            {'tag':'v1.8.4-rc.1', 'previous':'v1.8.3'},
            {'tag':None, 'previous':'v1.8.3'},
        ):
            self.check_release_version(version)


class NativeInstallationRejections(unittest.TestCase):
    def test_every_asset_has_a_reviewed_blank_digest(self):
        files = {'brand-protection/'+path.relative_to(SCRIPTS.parent).as_posix():path.read_bytes()
                 for path in (SCRIPTS.parent/'assets').rglob('*') if path.is_file()}
        baseline = {name.removeprefix('brand-protection/'):hashlib.sha256(data).hexdigest()
                    for name, data in files.items()}
        self.assertEqual(builder.blank_templates(files, baseline), len(files))
        for name in files:
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, 'template'):
                builder.blank_templates({**files, name:files[name]+b'\nSYNTHETIC-PRIVATE-DATA'}, baseline)

    def test_rebaselining_cannot_enable_case_records_or_approvals(self):
        files = {'brand-protection/'+path.relative_to(SCRIPTS.parent).as_posix():path.read_bytes()
                 for path in (SCRIPTS.parent/'assets').rglob('*') if path.is_file()}
        for name, mutate in (
            ('assets/case-record.json', lambda value:value.update(official_domains=['synthetic.invalid'])),
            ('assets/case-record.json', lambda value:value['intake'].update(private_case_directory='/private/synthetic')),
            ('assets/submission-approval.json', lambda value:value['checks'].update(rights_checked=True)),
            ('assets/consumer-approval.json', lambda value:value.update(approved_at='2026-11-16T00:00:00Z')),
        ):
            with self.subTest(path=name):
                key = 'brand-protection/'+name
                value = json.loads(files[key])
                mutate(value)
                changed = {**files, key:json.dumps(value).encode('utf-8')}
                baseline = {path.removeprefix('brand-protection/'):hashlib.sha256(data).hexdigest()
                            for path, data in changed.items()}
                with self.assertRaisesRegex(ValueError, 'template'):
                    builder.blank_templates(changed, baseline)

    def test_unsafe_manifest_paths_and_duplicate_entries_are_rejected(self):
        for name in ('brand-protection/../outside', 'brand-protection/../../outside',
                     'brand-protection/\\outside', '/brand-protection/outside'):
            with self.subTest(path=name), self.assertRaises(ValueError):
                native.expected_files({'files':[{'path':name, 'sha256':'a'*64}]})
        record = {'path':'brand-protection/SKILL.md','sha256':'a'*64}
        with self.assertRaises(ValueError):
            native.expected_files({'files':[record, record]})

    def test_zip_bytes_and_symbolic_links_are_rejected_before_native_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary)/'archive.zip'
            name, data = 'brand-protection/SKILL.md', b'synthetic entry'
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr(name, data)
            with self.assertRaisesRegex(ValueError, 'byte mismatch'):
                native.verify_archive(archive, {name:'a'*64})
            with zipfile.ZipFile(archive, 'w') as handle:
                item = zipfile.ZipInfo(name)
                item.create_system = 3
                item.external_attr = 0o120777 << 16
                handle.writestr(item, data)
            with self.assertRaisesRegex(ValueError, 'regular files'):
                native.verify_archive(archive, {name:hashlib.sha256(data).hexdigest()})

    def test_changed_installed_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            collection = Path(temporary)
            file = collection/'brand-protection/SKILL.md'
            file.parent.mkdir()
            file.write_bytes(b'changed installed bytes')
            with self.assertRaisesRegex(ValueError, 'byte mismatch'):
                native.verify_files(collection, {'brand-protection/SKILL.md':hashlib.sha256(b'reviewed').hexdigest()})

    def test_entry_file_parse_failure_is_not_host_loading_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary)
            (skill/'SKILL.md').write_bytes(b'# Missing entry metadata\n')
            with self.assertRaisesRegex(ValueError, 'front matter'):
                native.validate_entry_files(skill)


class PublicCaptureProbeContract(unittest.TestCase):
    def test_public_probe_requires_the_bounded_collection_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary)
            (scripts/'capture_web.py').write_bytes(b'''
import hashlib
from pathlib import Path
def capture(url, target_id, directory, allowed_hosts, *, allow_network, total_timeout):
    assert url == 'https://example.com/' and allowed_hosts == ['example.com']
    assert allow_network is True and total_timeout == 30
    body = b'Example Domain'
    file = Path(directory)/'body.bin'
    file.write_bytes(body)
    return {'observation':{'http_status':200},
            'evidence':{'original_path':str(file), 'sha256':hashlib.sha256(body).hexdigest(),
                        'acquired_at':'2026-11-16T00:00:00+00:00'}}
''')
            result = subprocess.run([sys.executable, '-B', str(ROOT/'tools/live_probe.py'),
                                     '--scripts', str(scripts), '--checks', 'capture'],
                                    cwd=ROOT, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, repr(result.stderr))
            check = json.loads(result.stdout)['checks'][0]
            self.assertEqual(check['collection_worker_timeout_seconds'], 30)
            self.assertTrue(check['collection_worker_exercised'])
            self.assertFalse(check['final_persistence_timeout_enforced'])


if __name__ == '__main__':
    unittest.main()
