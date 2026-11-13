"""Synthetic, offline selective-export contracts."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('evidence_export', SCRIPTS/'evidence_export.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026,11,13,4,tzinfo=timezone.utc)


def fixtures(root):
    original, redacted = b'private original synthetic identity', b'reviewed synthetic copy'
    (root/'original.bin').write_bytes(original)
    (root/'copy.bin').write_bytes(redacted)
    (root/'unrelated.bin').write_bytes(b'never export this')
    case = {'case_id': 'synthetic', 'evidence': [
        {'id': 'original', 'original_path': 'original.bin', 'sha256': hashlib.sha256(original).hexdigest(), 'disclosure_class': 'restricted'},
        {'id': 'copy', 'original_path': 'copy.bin', 'sha256': hashlib.sha256(redacted).hexdigest(),
         'derived_from_id': 'original', 'disclosure_class': 'restricted'}]}
    review = {'version': '1.0', 'id': 'review-1', 'case_id': 'synthetic', 'recipient': 'provider.example',
              'purpose': 'specified complaint', 'reviewed_by': 'synthetic reviewer',
              'authorization_source': 'synthetic user instruction', 'approved_at': '2026-11-13T03:00:00Z',
              'expires_at': '2026-11-13T05:00:00Z', 'items': [
                  {'evidence_id': 'copy', 'sha256': case['evidence'][1]['sha256'],
                   'derived_from_id': 'original', 'disclosure_reviewed': True}]}
    return case, review


class ExportContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.case, self.review = fixtures(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def prepare(self):
        return m.prepare(self.case, self.review, self.root, NOW)

    def test_only_selected_bytes_and_minimal_manifest_are_exported(self):
        files, manifest = self.prepare()
        self.assertEqual(set(files), {'files/0001.bin', 'MANIFEST.json'})
        self.assertEqual(files['files/0001.bin'], b'reviewed synthetic copy')
        self.assertNotIn(b'private original', b''.join(files.values()))
        self.assertNotIn(str(self.root).encode(), files['MANIFEST.json'])
        self.assertNotIn('authorization_source', manifest)

    def test_original_requires_explicit_per_item_approval(self):
        self.review['items'] = [{'evidence_id': 'original', 'sha256': self.case['evidence'][0]['sha256'], 'disclosure_reviewed': True}]
        with self.assertRaises(ValueError): self.prepare()
        self.review['items'][0]['allow_original'] = True
        self.assertEqual(self.prepare()[0]['files/0001.bin'], (self.root/'original.bin').read_bytes())

    def test_case_and_review_are_never_modified(self):
        before = copy.deepcopy((self.case, self.review))
        self.prepare()
        self.assertEqual((self.case, self.review), before)

    def test_changed_bytes_invalidate_review(self):
        (self.root/'copy.bin').write_bytes(b'changed')
        with self.assertRaises(ValueError): self.prepare()

    def test_changed_record_hash_invalidates_review(self):
        self.case['evidence'][1]['sha256'] = 'a'*64
        with self.assertRaises(ValueError): self.prepare()

    def test_cross_case_review_is_rejected(self):
        self.review['case_id'] = 'other'
        with self.assertRaises(ValueError): self.prepare()

    def test_missing_or_nonboolean_disclosure_review_is_rejected(self):
        for value in (None, False, 1, 'true'):
            self.review['items'][0]['disclosure_reviewed'] = value
            with self.assertRaises(ValueError): self.prepare()

    def test_expired_future_and_naive_approvals_are_rejected(self):
        for field, value in [('expires_at','2026-11-13T04:00:00Z'), ('approved_at','2026-11-14T03:00:00Z'), ('approved_at','2026-11-13T03:00:00')]:
            case, review = fixtures(self.root)
            review[field] = value
            with self.assertRaises(ValueError): m.prepare(case, review, self.root, NOW)

    def test_missing_reviewer_recipient_or_authority_is_rejected(self):
        for field in ('reviewed_by','recipient','authorization_source','purpose'):
            changed = copy.deepcopy(self.review)
            changed.pop(field)
            with self.assertRaises(ValueError): m.prepare(self.case, changed, self.root, NOW)

    def test_unknown_or_repeated_selection_is_rejected(self):
        self.review['items'].append(copy.deepcopy(self.review['items'][0]))
        with self.assertRaises(ValueError): self.prepare()
        self.review['items'] = [{'evidence_id':'missing'}]
        with self.assertRaises(ValueError): self.prepare()

    def test_duplicate_evidence_ids_block_export(self):
        self.case['evidence'].append(copy.deepcopy(self.case['evidence'][0]))
        with self.assertRaises(ValueError): self.prepare()

    def test_copy_lineage_changes_and_cycles_are_rejected(self):
        self.review['items'][0]['derived_from_id'] = 'missing'
        with self.assertRaises(ValueError): self.prepare()
        self.review['items'][0]['derived_from_id'] = 'original'
        self.case['evidence'][0]['derived_from_id'] = 'copy'
        with self.assertRaises(ValueError): self.prepare()

    def test_out_of_root_and_parent_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside)/'copy.bin'
            path.write_bytes(b'reviewed synthetic copy')
            for value in (str(path), '../copy.bin'):
                self.case['evidence'][1]['original_path'] = value
                with self.assertRaises(ValueError): self.prepare()

    def test_directory_is_not_evidence(self):
        self.case['evidence'][1]['original_path'] = str(self.root)
        with self.assertRaises(ValueError): self.prepare()

    def test_file_and_total_limits_are_enforced(self):
        with patch.object(m, 'FILE_LIMIT', 1), self.assertRaises(ValueError): self.prepare()
        with patch.object(m, 'TOTAL_LIMIT', 1), self.assertRaises(ValueError): self.prepare()

    def test_symlink_path_is_rejected_before_read(self):
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path.name == 'copy.bin' or original(path)), self.assertRaises(ValueError):
            self.prepare()

    def test_archive_is_deterministic_and_uses_safe_paths(self):
        files, _ = self.prepare()
        first = m.build_archive(files)
        self.assertEqual(first, m.build_archive(files))
        with zipfile.ZipFile(io.BytesIO(first)) as handle:
            self.assertIsNone(handle.testzip())
            self.assertEqual(handle.read('files/0001.bin'), files['files/0001.bin'])

    def test_export_creates_private_unique_packages_without_overwrite(self):
        before = (self.root/'original.bin').read_bytes()
        first = m.export(self.case,self.review,self.root,self.root,NOW)
        second = m.export(self.case,self.review,self.root,self.root,NOW)
        self.assertNotEqual(first['package'], second['package'])
        self.assertEqual(Path(first['package']).read_bytes(), Path(second['package']).read_bytes())
        self.assertFalse(first['authorizes_submission'])
        self.assertEqual((self.root/'original.bin').read_bytes(), before)

    def test_write_failure_removes_only_new_directory(self):
        with patch.object(m, 'write_private', side_effect=OSError('disk full')), self.assertRaises(OSError):
            m.export(self.case,self.review,self.root,self.root,NOW)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['copy.bin','original.bin','unrelated.bin'])

    def test_skill_output_directory_is_rejected(self):
        with self.assertRaises(ValueError): m.export(self.case,self.review,self.root,SCRIPTS,NOW)


if __name__ == '__main__': unittest.main()
