"""Synthetic evidence-priority and opt-in RDAP original-byte regressions."""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT / 'skills/brand-protection/scripts'))
sys.path.insert(0, str(SCRIPTS))


def load_module(name):
    spec = importlib.util.spec_from_file_location(name + '_v184', SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


investigation = load_module('investigation')
rdap = load_module('rdap_lookup')
from evidence_export import read_evidence
from capture_web import write_private

NOW = datetime(2026, 11, 16, 4, tzinfo=timezone.utc)
RAW_RESPONSE = (b'\xef\xbb\xbf{ "objectClassName": "domain", "ldhName": "EXAMPLE.COM",\n'
                b'"remarks": [{"description": ["synthetic private contact"]}]}\r\n')


def synthetic_case(registration='unknown', signal='impersonation'):
    return {
        'schema_version': '1.3', 'case_id': 'synthetic-v184', 'brand_id': 'synthetic-brand',
        'targets': [{'id': 'target-1', 'case_id': 'synthetic-v184',
                     'brand_id': 'synthetic-brand', 'normalized_domain': 'example.com'}],
        'evidence': [{'id': 'observation-1', 'case_id': 'synthetic-v184',
                      'brand_id': 'synthetic-brand', 'target_id': 'target-1'}],
        'investigation': {'version': '1.0', 'results': [{
            'id': 'investigation-1', 'case_id': 'synthetic-v184', 'brand_id': 'synthetic-brand',
            'target_id': 'target-1', 'domain_ascii': 'example.com',
            'queried_at': '2026-11-16T03:00:00Z', 'expires_at': '2026-11-16T05:00:00Z',
            'registration': registration, 'relationship': 'third_party',
            'signals': [signal] if signal else [], 'evidence_ids': ['observation-1'],
            'source': {'name': 'synthetic page observation', 'reference': 'fixture:v184',
                       'coverage': 'complete', 'cache': 'live', 'limitations': []}}]},
    }


class EvidencePriorityRegressions(unittest.TestCase):
    def test_concrete_signals_survive_unknown_or_not_found_registration(self):
        for registration in ('unknown', 'not_found'):
            for signal in ('copied_content', 'impersonation', 'credential_collection'):
                with self.subTest(registration=registration, signal=signal):
                    case = synthetic_case(registration, signal)
                    before = copy.deepcopy(case)
                    report = investigation.assess(case, NOW)
                    self.assertFalse(report['errors'])
                    result = report['results'][0]
                    self.assertEqual(result['next_step'], 'review_evidence')
                    self.assertEqual(result['registration'], registration)
                    self.assertTrue(result['registration_review_required'])
                    self.assertEqual(result['signals'], [signal])
                    self.assertEqual(result['evidence_ids'], ['observation-1'])
                    self.assertFalse(result['authorizes_submission'])
                    self.assertFalse(report['authorizes_submission'])
                    self.assertEqual(case, before)

    def test_registration_review_remains_explicit_without_concrete_signals(self):
        for signal in (None, 'brand_use', 'transaction', 'suspected_counterfeit'):
            with self.subTest(signal=signal):
                result = investigation.assess(synthetic_case(signal=signal), NOW)['results'][0]
                self.assertEqual(result['next_step'], 'verify_registration')
                self.assertTrue(result['registration_review_required'])
        result = investigation.assess(synthetic_case('registered'), NOW)['results'][0]
        self.assertEqual(result['next_step'], 'review_evidence')
        self.assertFalse(result['registration_review_required'])

    def test_failed_stale_and_known_asset_rules_are_preserved(self):
        case = synthetic_case()
        row = case['investigation']['results'][0]
        row['source'].update(coverage='failed', limitations=['synthetic unavailable source'])
        report = investigation.assess(case, NOW)
        self.assertTrue(report['errors'])
        self.assertEqual(report['results'], [])
        row['source']['coverage'] = 'complete'
        row['expires_at'] = '2026-11-16T04:00:00Z'
        self.assertEqual(investigation.assess(case, NOW)['results'][0]['next_step'], 'refresh_source')
        row['expires_at'] = '2026-11-16T05:00:00Z'
        for relationship in ('owned', 'authorized'):
            row['relationship'] = relationship
            result = investigation.assess(case, NOW)['results'][0]
            self.assertEqual(result['next_step'], 'review_known_asset')
            self.assertTrue(result['registration_review_required'])

    def test_other_case_brand_or_target_evidence_cannot_promote_review(self):
        for field, value in (('case_id', 'another-case'), ('brand_id', 'another-brand'),
                             ('target_id', 'another-target')):
            with self.subTest(field=field):
                case = synthetic_case()
                case['evidence'][0][field] = value
                report = investigation.assess(case, NOW)
                self.assertTrue(report['errors'])
                self.assertEqual(report['results'], [])
                self.assertFalse(report['authorizes_submission'])


class RdapOriginalArchiveRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def query(self, body=RAW_RESPONSE, **options):
        return rdap.lookup('example.com', 'target-1', allow_network=True, now=NOW,
                           transport=Mock(return_value=(200, 'application/rdap+json', body)), **options)

    def test_old_lookup_api_remains_read_only_without_archive_option(self):
        with patch.object(rdap, 'write_private', create=True,
                          side_effect=AssertionError('unexpected archive write')):
            report = self.query()
        self.assertFalse(report['errors'])
        self.assertTrue(report['read_only'])
        self.assertFalse(report['writes_case'])
        self.assertFalse(report['authorizes_submission'])
        self.assertIsNone(report['evidence'][0]['original_path'])
        self.assertEqual(report['evidence'][0]['sha256'], hashlib.sha256(RAW_RESPONSE).hexdigest())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_private_archive_preserves_original_bytes_for_downstream_review(self):
        report = self.query(archive_root=self.root)
        self.assertFalse(report['errors'])
        self.assertFalse(report['read_only'])
        self.assertTrue(report['writes_archive'])
        self.assertFalse(report['writes_case'])
        self.assertFalse(report['authorizes_submission'])
        record = report['evidence'][0]
        path = Path(record['original_path'])
        self.assertTrue(path.is_absolute())
        path.relative_to(self.root)
        self.assertEqual(path.read_bytes(), RAW_RESPONSE)
        self.assertEqual(record['sha256'], hashlib.sha256(RAW_RESPONSE).hexdigest())
        self.assertEqual(read_evidence(self.root, record['original_path'], record['sha256']), RAW_RESPONSE)
        self.assertEqual(record['disclosure_class'], 'restricted')
        self.assertNotIn('synthetic private contact', str(report))
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_valid_not_found_response_can_be_archived_without_claiming_availability(self):
        body = b'{"errorCode": 404, "title": "synthetic not found"}\n'
        report = rdap.lookup('example.net', 'target-1', allow_network=True, now=NOW,
                             archive_root=self.root,
                             transport=Mock(return_value=(404, 'application/json', body)))
        self.assertFalse(report['errors'])
        self.assertEqual(report['result']['registration'], 'not_found')
        self.assertFalse(report['authorizes_submission'])
        self.assertEqual(read_evidence(self.root, report['evidence'][0]['original_path'],
                                       report['evidence'][0]['sha256']), body)

    def test_repeated_archives_do_not_overwrite_existing_originals(self):
        first = self.query(archive_root=self.root)
        changed = b'{"objectClassName":"domain","ldhName":"example.com","status":["active"]}'
        second = self.query(changed, archive_root=self.root)
        self.assertFalse(first['errors'])
        self.assertFalse(second['errors'])
        first_path = Path(first['evidence'][0]['original_path'])
        second_path = Path(second['evidence'][0]['original_path'])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.read_bytes(), RAW_RESPONSE)
        self.assertEqual(second_path.read_bytes(), changed)

    def test_archive_write_failure_emits_no_success_evidence_and_keeps_unrelated_files(self):
        marker = self.root / 'unrelated.txt'
        marker.write_bytes(b'synthetic prior evidence')
        with patch.object(rdap, 'write_private', create=True,
                          side_effect=OSError('synthetic private path')):
            report = self.query(archive_root=self.root)
        self.assertTrue(report['errors'])
        self.assertEqual(report['errors'][0]['code'], 'archive_unavailable')
        self.assertEqual(report['result']['registration'], 'unknown')
        self.assertEqual(report['result']['evidence_ids'], [])
        self.assertEqual(report['evidence'], [])
        self.assertFalse(report['writes_archive'])
        self.assertFalse(report['authorizes_submission'])
        self.assertNotIn('synthetic private path', str(report))
        self.assertEqual(list(self.root.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b'synthetic prior evidence')

    def test_readback_detects_changed_stored_bytes_before_emitting_evidence(self):
        def corrupt_after_write(path, data):
            write_private(path, data)
            Path(path).write_bytes(b'synthetic changed response')

        with patch.object(rdap, 'write_private', create=True, side_effect=corrupt_after_write):
            report = self.query(archive_root=self.root)
        self.assertTrue(report['errors'])
        self.assertEqual(report['errors'][0]['code'], 'archive_unavailable')
        self.assertEqual(report['result']['registration'], 'unknown')
        self.assertEqual(report['evidence'], [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_archive_root_rejects_traversal_non_directory_and_repository_before_network(self):
        ordinary_file = self.root / 'ordinary.bin'
        ordinary_file.write_bytes(b'synthetic existing file')
        for root in (self.root / '..' / self.root.name, ordinary_file, ROOT, SCRIPTS.parent):
            with self.subTest(root=str(root)):
                transport = Mock()
                with self.assertRaises((ValueError, OSError)):
                    rdap.lookup('example.com', 'target-1', allow_network=True,
                                archive_root=root, transport=transport, now=NOW)
                transport.assert_not_called()
        self.assertEqual(ordinary_file.read_bytes(), b'synthetic existing file')

    def test_archive_root_rejects_user_controlled_symbolic_link(self):
        link = self.root / 'linked'
        destination = self.root / 'destination'
        destination.mkdir()
        try:
            link.symlink_to(destination, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links unavailable')
        transport = Mock()
        with self.assertRaises(ValueError):
            rdap.lookup('example.com', 'target-1', allow_network=True,
                        archive_root=link, transport=transport, now=NOW)
        transport.assert_not_called()
        self.assertEqual(list(destination.iterdir()), [])

    def test_archive_root_changed_to_link_during_lookup_cannot_escape(self):
        selected = self.root / 'selected'
        outside = self.root / 'outside'
        selected.mkdir()
        outside.mkdir()
        probe = self.root / 'symlink-probe'
        try:
            probe.symlink_to(outside, target_is_directory=True)
            probe.unlink()
        except (OSError, NotImplementedError):
            self.skipTest('symbolic links unavailable')

        def replace_root(domain, timeout):
            selected.rename(self.root / 'original-selected')
            selected.symlink_to(outside, target_is_directory=True)
            return 200, 'application/rdap+json', RAW_RESPONSE

        report = rdap.lookup('example.com', 'target-1', allow_network=True,
                             archive_root=selected, transport=replace_root, now=NOW)
        self.assertTrue(report['errors'])
        self.assertEqual(report['errors'][0]['code'], 'archive_unavailable')
        self.assertEqual(report['evidence'], [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_responses_leave_no_original_or_success_evidence(self):
        report = self.query(b'{"objectClassName":"domain","ldhName":"other.com"}', archive_root=self.root)
        self.assertTrue(report['errors'])
        self.assertEqual(report['evidence'], [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_archive_option_writes_and_reports_reviewable_original(self):
        original_lookup = rdap.lookup

        def synthetic_lookup(*args, **options):
            return original_lookup(*args, now=NOW,
                                   transport=Mock(return_value=(200, 'application/rdap+json', RAW_RESPONSE)),
                                   **options)

        output = io.StringIO()
        arguments = ['rdap_lookup.py', '--domain', 'example.com', '--target-id', 'target-1',
                     '--allow-network', '--archive-root', str(self.root)]
        with patch.object(sys, 'argv', arguments), patch.object(rdap, 'lookup', side_effect=synthetic_lookup):
            with redirect_stdout(output):
                code = rdap.main()
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(read_evidence(self.root, report['evidence'][0]['original_path'],
                                       report['evidence'][0]['sha256']), RAW_RESPONSE)

    def test_cli_missing_private_root_is_nonzero_without_disclosing_path(self):
        output = io.StringIO()
        missing = self.root / 'synthetic-private-case-name'
        arguments = ['rdap_lookup.py', '--domain', 'example.com', '--target-id', 'target-1',
                     '--allow-network', '--archive-root', str(missing)]
        with patch.object(sys, 'argv', arguments), redirect_stdout(output):
            code = rdap.main()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['errors'], ['private_archive_directory_unavailable'])
        self.assertNotIn('synthetic-private-case-name', output.getvalue())
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
