"""Native OS integration checks with synthetic data only; no network or sending."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
sys.path.insert(0, str(SCRIPTS))
import filesystem_paths as paths
import capture_web as capture
import evidence_export as export
import consumer_materials as consumer
import test_consumer_materials as consumer_cases

CLI = ('followup_queue', 'investigation', 'rdap_lookup', 'capture_web', 'evidence_export',
       'submission_review', 'channel_check', 'case_dashboard', 'consumer_materials')


def run_cli(name, arguments, cwd, encoding='ascii'):
    return subprocess.run([sys.executable, '-B', str(SCRIPTS/(name+'.py')), *map(str, arguments)],
                          cwd=cwd, env={**os.environ, 'PYTHONIOENCODING': encoding,
                                        'PYTHONDONTWRITEBYTECODE': '1'},
                          capture_output=True, timeout=30)


class PortablePaths(unittest.TestCase):
    def test_windows_local_paths_accept_unicode_spaces_and_both_separators(self):
        for value in ('C:/Private Cases/资料/file.bin', r'C:\Private Cases\资料\file.bin',
                      '证据 素材/file.bin', 'normal.bin'):
            with self.subTest(value=value):
                paths.validate_windows_path(value)

    def test_windows_ambiguous_device_ads_and_reserved_paths_are_rejected(self):
        for value in (r'C:relative', r'\relative', r'\\server\share\case', r'\\?\C:\case',
                      r'\\.\NUL', 'C:/file.bin:secret', 'folder./file', 'folder /file',
                      'NUL', 'nul.txt', 'COM1.bin', 'LPT9', 'COM¹', 'CONOUT$',
                      'file?.bin', '../file', 'a/../b', 'a\nfile'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                paths.validate_windows_path(value)

    def test_exact_root_owned_macos_aliases_are_allowed(self):
        for alias, destination in paths.SYSTEM_ALIASES.items():
            for link in (destination, destination.lstrip('/')):
                self.assertEqual(paths.system_alias_target(alias, link, 'darwin', 0), Path(destination))

    def test_macos_alias_exception_does_not_apply_on_other_platforms(self):
        for platform in ('win32', 'linux', 'freebsd'):
            self.assertIsNone(paths.system_alias_target('/tmp', 'private/tmp', platform, 0))

    def test_non_root_alias_is_not_trusted(self):
        for uid in (None, 1, 501, 1000):
            self.assertIsNone(paths.system_alias_target('/tmp', 'private/tmp', 'darwin', uid))

    def test_alias_basename_or_unexpected_destination_cannot_bypass(self):
        for alias, link in (('/home/tmp', '/private/tmp'), ('/tmp', '/other'),
                            ('/var', 'private/tmp'), ('/tmp', '../private/tmp')):
            self.assertIsNone(paths.system_alias_target(alias, link, 'darwin', 0))

    def test_private_root_accepts_native_unicode_and_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'私有 案件 café'
            root.mkdir()
            self.assertEqual(capture.private_root(root), root.resolve())

    def test_existing_path_accepts_path_objects_and_file_names(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'证据 文件.bin'
            file.write_bytes(b'synthetic')
            self.assertEqual(paths.checked_existing_path(file), file.resolve())
            self.assertEqual(paths.checked_existing_path(str(file)), file.resolve())

    def test_parent_traversal_is_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'child').mkdir()
            with self.assertRaises(ValueError):
                capture.private_root(root/'child'/'..')

    def test_missing_directory_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory)/'missing'
            with self.assertRaises(OSError):
                capture.private_root(missing)
            self.assertFalse(missing.exists())

    def test_file_is_not_a_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'file'
            file.write_bytes(b'x')
            with self.assertRaises(ValueError):
                capture.private_root(file)

    def test_binary_evidence_bytes_survive_native_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = capture.private_root(directory)
            (root/'证据 素材').mkdir()
            data = b'\x00\n\r\n\x1a\xff' + '合成资料'.encode('utf-8')
            file = root/'证据 素材'/'响应.bin'
            capture.write_private(file, data)
            expected = hashlib.sha256(data).hexdigest()
            for name in (str(file), '证据 素材/响应.bin', str(file.relative_to(root))):
                self.assertEqual(export.read_evidence(root, name, expected), data)

    def test_other_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            file = Path(b)/'outside.bin'
            file.write_bytes(b'x')
            with self.assertRaises(ValueError):
                export.read_evidence(capture.private_root(a), str(file), hashlib.sha256(b'x').hexdigest())

    def test_changed_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = capture.private_root(directory)
            (root/'证据.bin').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                export.read_evidence(root, '证据.bin', hashlib.sha256(b'original').hexdigest())

    def test_private_write_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'私有.bin'
            capture.write_private(file, b'first\r\n')
            with self.assertRaises(FileExistsError):
                capture.write_private(file, b'second')
            self.assertEqual(file.read_bytes(), b'first\r\n')

    def test_posix_permissions_are_restricted_windows_bytes_still_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'private.bin'
            capture.write_private(file, b'x')
            self.assertEqual(file.read_bytes(), b'x')
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE(file.stat().st_mode) & 0o077, 0)
            # Windows ACLs are a deployment requirement, not proven by mode 0600.

    @unittest.skipIf(os.name == 'nt', 'Native Windows junction contract covers unprivileged link creation')
    def test_user_directory_symlink_remains_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'real').mkdir()
            (root/'link').symlink_to(root/'real', target_is_directory=True)
            with self.assertRaises(ValueError):
                capture.private_root(root/'link')

    @unittest.skipIf(os.name == 'nt', 'Native Windows junction contract covers unprivileged link creation')
    def test_user_file_symlink_remains_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = capture.private_root(directory)
            (root/'real.bin').write_bytes(b'x')
            (root/'link.bin').symlink_to(root/'real.bin')
            with self.assertRaises(ValueError):
                export.read_evidence(root, 'link.bin', hashlib.sha256(b'x').hexdigest())

    @unittest.skipUnless(os.name == 'nt', 'Requires a native Windows NTFS junction')
    def test_native_windows_junction_blocks_root_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target, link = root/'real', root/'junction'
            target.mkdir()
            (target/'file.bin').write_bytes(b'x')
            created = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                                     capture_output=True, timeout=15)
            self.assertEqual(created.returncode, 0, repr(created.stderr))
            try:
                with self.assertRaises(ValueError):
                    capture.private_root(link)
                with self.assertRaises(ValueError):
                    export.read_evidence(root, str(link/'file.bin'), hashlib.sha256(b'x').hexdigest())
            finally:
                os.rmdir(link)

    @unittest.skipUnless(sys.platform == 'darwin', 'Requires macOS system directory aliases')
    def test_native_macos_system_aliases_support_evidence(self):
        for alias, target in paths.SYSTEM_ALIASES.items():
            self.assertTrue(Path(alias).is_symlink())
            self.assertEqual(Path(alias).resolve(), Path(target))
            # /var/folders or /tmp are writable; no changes to system aliases.
            parent = '/tmp' if alias == '/tmp' else tempfile.gettempdir()
            with tempfile.TemporaryDirectory(dir=parent) as directory:
                canonical = Path(directory).resolve()
                supplied = Path(alias)/canonical.relative_to(target)
                root = capture.private_root(supplied)
                file = supplied/'证据.bin'
                file.write_bytes(b'x')
                self.assertEqual(export.read_evidence(root, str(file), hashlib.sha256(b'x').hexdigest()), b'x')

    @unittest.skipUnless(os.name == 'nt', 'Requires native case-insensitive Windows paths')
    def test_windows_drive_and_path_case_preserve_containment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = capture.private_root(directory)
            file = root/'CaseFile.bin'
            file.write_bytes(b'x')
            self.assertEqual(export.read_evidence(root, str(file).upper(), hashlib.sha256(b'x').hexdigest()), b'x')


class NativeCommands(unittest.TestCase):
    def test_all_nine_cli_entrypoints_work_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in CLI:
                with self.subTest(name=name):
                    result = run_cli(name, ['--help'], directory)
                    self.assertEqual(result.returncode, 0, repr(result.stderr))
                    self.assertIn(b'usage:', result.stdout)

    def test_unicode_case_bom_crlf_and_ascii_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'私有 案件'
            root.mkdir()
            case = json.loads((SCRIPTS.parent/'assets/case-record.json').read_text(encoding='utf-8'))
            case['case_id'] = '合成案件'
            file = root/'案件 record.json'
            data = ('\ufeff'+json.dumps(case, ensure_ascii=False, indent=2).replace('\n', '\r\n')).encode('utf-8')
            file.write_bytes(data)
            for name in ('followup_queue', 'investigation'):
                with self.subTest(name=name):
                    result = run_cli(name, ['--case', file], root)
                    self.assertEqual(result.returncode, 0, repr(result.stderr))
                    json.loads(result.stdout.decode('ascii'))
            self.assertEqual(file.read_bytes(), data)

    def test_capture_cli_needs_network_permission_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli('capture_web', ['--url', 'https://example.com', '--target-id', 'synthetic',
                '--allow-host', 'example.com', '--output-root', directory], directory)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)['authorizes_submission'])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_consumer_outputs_use_native_unicode_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'私有 草稿'
            root.mkdir()
            result = consumer.generate(consumer_cases.fixture(), root, now=consumer_cases.NOW)
            self.assertFalse(result['authorizes_publication'])
            for name, expected in result['files'].items():
                data = (Path(result['directory'])/name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
                self.assertIn('合成品牌', data.decode('utf-8'))

    def test_native_shell_quotes_paths_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            case_path = Path(directory)/'合成 案件.json'
            case_path.write_bytes((SCRIPTS.parent/'assets/case-record.json').read_bytes())
            env = {**os.environ, 'BP_PYTHON': sys.executable,
                   'BP_SCRIPT': str(SCRIPTS/'followup_queue.py'), 'BP_CASE': str(case_path),
                   'PYTHONIOENCODING': 'ascii', 'PYTHONDONTWRITEBYTECODE': '1'}
            if os.name == 'nt':
                shells = [shutil.which(name) for name in ('powershell', 'pwsh')]
                shells = [shell for shell in shells if shell]
                self.assertTrue(shells, 'Windows requires PowerShell for the documented shell checks')
                command = '& $env:BP_PYTHON -B $env:BP_SCRIPT --case $env:BP_CASE; exit $LASTEXITCODE'
                commands = [[shell, '-NoProfile', '-NonInteractive', '-Command', command] for shell in shells]
            else:
                shell = shutil.which('zsh' if sys.platform == 'darwin' else 'bash')
                self.assertIsNotNone(shell)
                commands = [[shell, '-c', '"$BP_PYTHON" -B "$BP_SCRIPT" --case "$BP_CASE"']]
            for command in commands:
                with self.subTest(shell=command[0]):
                    result = subprocess.run(command, cwd=directory, env=env, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, repr(result.stderr))
                    json.loads(result.stdout)


if __name__ == '__main__':
    unittest.main()
