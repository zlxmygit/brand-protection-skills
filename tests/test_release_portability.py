"""Build-time encoding and honest platform-skip accounting; no publication."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable_release_builder', ROOT/'tools/build_release.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ReleasePortability(unittest.TestCase):
    def test_normal_summary(self):
        self.assertEqual(m.test_summary('Ran 357 tests in 1.2s\n\nOK\n'),
                         {'discovered':357,'passed':357,'skipped':0})

    def test_platform_skips_are_not_counted_as_passed(self):
        self.assertEqual(m.test_summary('Ran 383 tests in 1.2s\r\n\r\nOK (skipped=3)\r\n'),
                         {'discovered':383,'passed':380,'skipped':3})

    def test_missing_failed_and_expected_failure_summaries_are_rejected(self):
        for value in ('OK\n', 'Ran 3 tests in 1s\n', 'Ran 3 tests in 1s\nFAILED (errors=1)\n',
                      'Ran 3 tests in 1s\nOK (expected failures=1)\n',
                      'Ran 3 tests in 1s\nOK\nFAILED (failures=1)\n'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                m.test_summary(value)

    def test_empty_or_impossible_counts_are_rejected(self):
        for value in ('Ran 0 tests in 1s\nOK\n', 'Ran 2 tests in 1s\nOK (skipped=3)\n',
                      'Ran 2 tests in 1s\nOK (skipped=2)\n'):
            with self.assertRaises(RuntimeError):
                m.test_summary(value)

    def test_extracted_test_environment_uses_explicit_utf8(self):
        result = subprocess.CompletedProcess([], 0, '', 'Ran 3 tests in 1s\nOK\n')
        with patch.object(m.subprocess, 'run', return_value=result) as run:
            m.run_tests(Path('Skill 安装/scripts'))
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs['encoding'], 'utf-8')
        self.assertEqual(kwargs['env']['PYTHONIOENCODING'], 'utf-8')
        self.assertEqual(kwargs['env']['BRAND_SKILL_SCRIPTS'], str(Path('Skill 安装/scripts')))

    def test_source_run_cannot_inherit_external_script_override(self):
        result = subprocess.CompletedProcess([], 0, '', 'Ran 3 tests in 1s\nOK\n')
        with patch.dict(os.environ, {'BRAND_SKILL_SCRIPTS':'other','FOLLOWUP_QUEUE_SCRIPT':'other'}):
            with patch.object(m.subprocess, 'run', return_value=result) as run:
                m.run_tests()
        self.assertNotIn('BRAND_SKILL_SCRIPTS', run.call_args.kwargs['env'])
        self.assertNotIn('FOLLOWUP_QUEUE_SCRIPT', run.call_args.kwargs['env'])

    def test_nonzero_test_exit_is_never_accepted(self):
        result = subprocess.CompletedProcess([], 1, '', 'Ran 3 tests in 1s\nOK\n')
        with patch.object(m.subprocess, 'run', return_value=result), self.assertRaises(RuntimeError):
            m.run_tests()

    def test_artifact_text_is_utf8_with_exact_lf_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'说明.md'
            m.write_utf8(file, '合成说明\n第二行\n')
            self.assertEqual(file.read_bytes(), '合成说明\n第二行\n'.encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
