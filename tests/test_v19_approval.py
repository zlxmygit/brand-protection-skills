"""Selected material-source restrictions bind synthetic submission approvals."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_submission_review as base
import submission_dependencies as dependencies


# Captured from the unmodified v1.8.2 resolver using this deterministic fixture.
LEGACY_NO_MATERIALS = '4d35c0d2ba9fdbf9fe8d83d70e188fe0f00e49df38c246640984c9a45702e1e7'
LEGACY_WITH_SOURCE = '10bf9370990c9a487a7e1d66801849960b4b3e0d35b36d7b532504c88e3d892b'


class ApprovalSourceContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.case, self.approval = base.fixture(self.root)
        content = b'Synthetic owner supplied rights certificate'
        (self.root/'material.bin').write_bytes(content)
        self.case['materials'] = [{'id':'m1','source_id':'source-1','source_path':'material.bin',
                                  'sha256':hashlib.sha256(content).hexdigest()}]
        self.case['rights'][0]['material_ids'] = ['m1']
        self.case['material_sources'] = [{
            'id':'source-1','case_id':'synthetic','brand_id':'brand-1','kind':'attachment',
            'location':'Synthetic owner attachment','provided_by':'Synthetic owner',
            'provided_at':'2026-11-13T00:00:00Z',
            'authorized_read_scope':'review this synthetic case',
            'disclosure_limits':'redacted disclosure only'}]
        self.approval['snapshot_sha256'] = self.snapshot()['sha256']

    def snapshot(self):
        return base.m.snapshot(self.case,'a1',self.root)

    def review(self):
        return base.m.review(self.case,'a1',self.approval,self.root,base.NOW)

    def assert_blocked(self, incomplete=False):
        result = self.review()
        self.assertEqual(result['state'],'needs_review')
        self.assertTrue(result['errors'])
        self.assertFalse(result['authorizes_submission'])
        if incomplete:
            self.assertFalse(result['snapshot']['complete'])
            self.assertIsNone(result['snapshot']['sha256'])

    def test_referenced_read_and_disclosure_changes_invalidate_approval(self):
        for field in ('authorized_read_scope','disclosure_limits'):
            with self.subTest(field=field):
                source = self.case['material_sources'][0]
                original = source[field]
                source[field] = 'changed synthetic restriction'
                self.assert_blocked()
                source[field] = original

    def test_referenced_source_metadata_is_part_of_the_reviewed_context(self):
        for field in ('kind','location','provided_by','provided_at'):
            with self.subTest(field=field):
                source = self.case['material_sources'][0]
                original = source[field]
                source[field] = 'changed synthetic source context'
                self.assert_blocked()
                source[field] = original

    def test_missing_source_or_source_collection_is_incomplete(self):
        for value in ([],[{'id':'other-source'}],None,{}):
            with self.subTest(value=value):
                self.case['material_sources'] = value
                self.assert_blocked(incomplete=True)
        self.case.pop('material_sources')
        self.assert_blocked(incomplete=True)

    def test_duplicate_referenced_source_is_incomplete_in_either_order(self):
        original = copy.deepcopy(self.case['material_sources'][0])
        conflicting = copy.deepcopy(original)
        conflicting['disclosure_limits'] = 'do not disclose'
        for rows in ([original,conflicting],[conflicting,original]):
            self.case['material_sources'] = rows
            self.assert_blocked(incomplete=True)

    def test_source_case_brand_and_target_conflicts_are_incomplete(self):
        for field,value in (('case_id','other-case'),('brand_id','other-brand'),
                            ('target_ids',['other-target'])):
            with self.subTest(field=field):
                original = copy.deepcopy(self.case['material_sources'][0])
                self.case['material_sources'][0][field] = value
                self.assert_blocked(incomplete=True)
                self.case['material_sources'][0] = original

    def test_invalid_explicit_source_id_is_not_legacy_omission(self):
        for value in (None,False,0,[],{},'', 'bad\nsource', 'x'*201):
            with self.subTest(value=value):
                self.case['materials'][0]['source_id'] = value
                self.assert_blocked(incomplete=True)

    def test_source_of_transitive_material_is_also_bound(self):
        self.case['materials'][0]['derived_from_id'] = 'parent-material'
        parent = copy.deepcopy(self.case['materials'][0])
        parent.update(id='parent-material',source_id='parent-source')
        parent.pop('derived_from_id')
        self.case['materials'].append(parent)
        source = copy.deepcopy(self.case['material_sources'][0])
        source['id'] = 'parent-source'
        self.case['material_sources'].append(source)
        self.approval['snapshot_sha256'] = self.snapshot()['sha256']
        source['disclosure_limits'] = 'synthetic parent may not be disclosed'
        self.assert_blocked()

    def test_unreferenced_sources_do_not_change_snapshot(self):
        before = self.snapshot()['sha256']
        unrelated = {'id':'unrelated','case_id':'other-case','brand_id':'other-brand',
                     'authorized_read_scope':'unrelated private scope',
                     'disclosure_limits':'unrelated private restriction'}
        self.case['material_sources'].append(unrelated)
        self.assertEqual(self.snapshot()['sha256'],before)
        unrelated['authorized_read_scope'] = 'changed'
        unrelated['disclosure_limits'] = 'changed'
        self.assertEqual(self.snapshot()['sha256'],before)
        self.case['material_sources'].append(copy.deepcopy(unrelated))
        self.assertEqual(self.snapshot()['sha256'],before)
        self.assertEqual(self.review()['state'],'approval_matches_snapshot')

    def test_unselected_material_source_is_not_resolved(self):
        self.case['materials'].append({'id':'unselected','source_id':'missing-source',
                                       'source_path':'missing.bin','sha256':'0'*64})
        self.assertEqual(self.review()['state'],'approval_matches_snapshot')

    def test_shared_source_is_counted_once_and_has_a_dependency_budget(self):
        second = copy.deepcopy(self.case['materials'][0])
        second['id'] = 'm2'
        self.case['materials'].append(second)
        self.case['rights'][0]['material_ids'].append('m2')
        with patch.object(dependencies,'MAX_DEPENDENCIES',4):
            self.assertTrue(self.snapshot()['complete'])
        with patch.object(dependencies,'MAX_DEPENDENCIES',3):
            self.assert_blocked(incomplete=True)

    def test_legacy_omitted_source_id_remains_readable(self):
        self.case['materials'][0].pop('source_id')
        self.case.pop('material_sources')
        prepared = self.snapshot()
        self.assertTrue(prepared['complete'])
        self.assertEqual(prepared['snapshot_version'],'1.2')
        self.approval['snapshot_sha256'] = prepared['sha256']
        self.assertEqual(self.review()['state'],'approval_matches_snapshot')
        self.assertFalse(self.review()['authorizes_submission'])

    def test_v11_approvals_require_a_new_review_with_and_without_sources(self):
        self.approval['snapshot_sha256'] = LEGACY_WITH_SOURCE
        self.assert_blocked()
        self.case,self.approval = base.fixture(self.root)
        self.approval['snapshot_sha256'] = LEGACY_NO_MATERIALS
        self.assert_blocked()

    def test_matching_source_review_is_read_only_and_never_authorizes_submission(self):
        before = copy.deepcopy((self.case,self.approval))
        result = self.review()
        self.assertEqual(result['state'],'approval_matches_snapshot')
        self.assertFalse(result['authorizes_submission'])
        self.assertEqual((self.case,self.approval),before)


if __name__ == '__main__':
    unittest.main()
