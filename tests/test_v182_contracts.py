"""Cross-module residual repair contracts; real helpers, synthetic local data."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_v181_residual_regressions as r
import record_integrity as integrity


class ConsumerLineageContracts(unittest.TestCase):
    def setUp(self):
        self.case = r.consumer_case()
        self.case['evidence'][0]['derived_from_id'] = 'parent'
        self.case['evidence'].append({'id':'parent','case_id':self.case['case_id'],
                                     'brand_id':self.case['brand_id'],'sha256':'2'*64})

    def prepare(self):
        return r.m.prepare(self.case, r.NOW)

    def test_parent_case_conflict_is_rejected(self):
        self.case['evidence'][1]['case_id'] = 'other-case'
        with self.assertRaises(ValueError):
            self.prepare()

    def test_self_and_multinode_cycles_are_rejected(self):
        for parent in ('e1','parent'):
            with self.subTest(parent=parent):
                self.case['evidence'][1]['derived_from_id'] = parent
                with self.assertRaises(ValueError):
                    self.prepare()

    def test_deep_ancestor_change_invalidates_approval(self):
        self.case['evidence'][1]['derived_from_id'] = 'grandparent'
        self.case['evidence'].append({'id':'grandparent','sha256':'3'*64})
        approval = r.approval_for(self.prepare())
        self.case['evidence'][2]['sha256'] = '4'*64
        with self.assertRaises(ValueError):
            r.m.match_approval(self.prepare(), approval, r.NOW)

    def test_duplicate_parent_is_quarantined(self):
        self.case['evidence'].append(copy.deepcopy(self.case['evidence'][1]))
        with self.assertRaises(ValueError):
            self.prepare()

    def test_shared_parent_is_counted_once(self):
        self.case['evidence'].append({'id':'sibling','derived_from_id':'parent'})
        self.case['consumer_communications']['channels'][0]['evidence_ids'].append('sibling')
        with patch.object(integrity,'MAX_DEPENDENCIES',3):
            self.assertFalse(self.prepare()['authorizes_publication'])
        with patch.object(integrity,'MAX_DEPENDENCIES',2), self.assertRaises(ValueError):
            self.prepare()

    def test_limit_is_shared_across_channels(self):
        profile = self.case['consumer_communications']
        second = copy.deepcopy(profile['channels'][0])
        second.update(id='site',kind='website',url='https://official.example/',evidence_ids=['e2'])
        profile['channels'].append(second)
        self.case['evidence'].extend([{'id':'e2','derived_from_id':'parent2'},{'id':'parent2'}])
        with patch.object(integrity,'MAX_DEPENDENCIES',3), self.assertRaises(ValueError):
            self.prepare()

    def test_long_lineage_is_iterative_and_bounded(self):
        self.case['evidence'] = [{'id':'e1','derived_from_id':'p0'}]
        self.case['evidence'] += [{'id':'p'+str(i),'derived_from_id':'p'+str(i+1)} for i in range(998)]
        self.case['evidence'].append({'id':'p998'})
        self.assertEqual(self.prepare()['state'],'draft_needs_review')
        self.case['evidence'][-1]['derived_from_id'] = 'overflow'
        self.case['evidence'].append({'id':'overflow'})
        with self.assertRaises(ValueError):
            self.prepare()

    def test_profile_and_channel_identity_conflicts_are_rejected(self):
        for section in ('profile','channel'):
            for field in ('case_id','brand_id'):
                changed = copy.deepcopy(self.case)
                row = changed['consumer_communications']
                if section == 'channel':
                    row = row['channels'][0]
                row[field] = 'other'
                with self.subTest(section=section,field=field), self.assertRaises(ValueError):
                    r.m.prepare(changed,r.NOW)

    def test_invalid_explicit_identity_is_not_treated_as_omission(self):
        for field in ('case_id','brand_id'):
            for value in (None,False,0,[],{},'','bad\nidentifier'):
                changed = copy.deepcopy(self.case)
                changed['evidence'][1][field] = value
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    r.m.prepare(changed,r.NOW)

    def test_unreferenced_evidence_does_not_change_fingerprint(self):
        before = self.prepare()['snapshot_sha256']
        self.case['evidence'].append({'id':'unrelated','brand_id':'other','derived_from_id':'unknown'})
        self.assertEqual(self.prepare()['snapshot_sha256'],before)

    def test_record_order_does_not_change_fingerprint(self):
        before = self.prepare()['snapshot_sha256']
        self.case['evidence'].reverse()
        self.assertEqual(self.prepare()['snapshot_sha256'],before)

    def test_preparation_does_not_mutate_records(self):
        before = copy.deepcopy(self.case)
        self.prepare()
        self.assertEqual(self.case,before)

    def test_parent_details_are_not_exported_as_public_text(self):
        self.case['evidence'][1]['private_reference'] = 'PRIVATE-ANCESTOR-SECRET'
        self.case['evidence'][1]['original_path'] = '/private/secret-certificate'
        with tempfile.TemporaryDirectory() as directory:
            receipt = r.m.generate(self.case,directory,r.approval_for(self.prepare()),r.NOW)
            self.assertEqual(receipt['state'],'review_matches_draft')
            self.assertFalse(receipt['authorizes_publication'])
            self.assertFalse(receipt['sends_messages'])
            for path in Path(receipt['directory']).iterdir():
                content = path.read_text(encoding='utf-8')
                self.assertNotIn('PRIVATE-ANCESTOR-SECRET',content)
                self.assertNotIn('/private/secret-certificate',content)

    def test_changed_parent_leaves_no_new_output(self):
        approval = r.approval_for(self.prepare())
        self.case['evidence'][1]['sha256'] = '5'*64
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                r.m.generate(self.case,directory,approval,r.NOW)
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_cli_conflict_is_structured_and_writes_nothing(self):
        self.case['evidence'][1]['case_id'] = 'other-case'
        # Keep timestamps valid at execution time so the test reaches lineage checks.
        self.case['consumer_communications']['review_before'] = '2099-01-01T00:00:00Z'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case_path = root/'case.json'
            case_path.write_text(json.dumps(self.case),encoding='utf-8')
            output = root/'out'
            output.mkdir()
            result = subprocess.run([sys.executable,'-B',str(r.SCRIPTS/'consumer_materials.py'),
                                     '--case',str(case_path),'--output-root',str(output)],
                                     capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,1)
            report = json.loads(result.stdout)
            self.assertTrue(report['errors'])
            self.assertFalse(report['authorizes_publication'])
            self.assertEqual(list(output.iterdir()),[])

    def test_legacy_fingerprint_requires_actual_new_review(self):
        case = r.consumer_case()
        prepared = r.m.prepare(case,r.NOW)
        source = {'case_id':case['case_id'],'brand_id':case['brand_id'],'brand':case['brand'],
                  'official_domains':sorted(case['official_domains']),
                  'profile':case['consumer_communications'],'evidence':case['evidence'],
                  'templates':r.m.read_json(r.m.ASSETS/'consumer-templates.json'),
                  'drafts':prepared['drafts']}
        legacy = hashlib.sha256(json.dumps(source,ensure_ascii=True,sort_keys=True,
                               separators=(',',':'),allow_nan=False).encode('ascii')).hexdigest()
        approval = r.approval_for(prepared)
        approval['snapshot_sha256'] = legacy
        self.assertEqual(prepared['snapshot_version'],'1.1')
        with self.assertRaises(ValueError):
            r.m.match_approval(prepared,approval,r.NOW)


class RightsScopeContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.case = r.rights_case(self.root)
        self.action = self.case['actions'][0]
        self.action['right_ids'] = ['r2']

    def assert_scope_blocked(self):
        before = copy.deepcopy(self.case)
        channel = r.c.check(self.case,'a1',r.NOW)
        self.assertEqual(channel['state'],'needs_review')
        self.assertTrue(channel['errors'])
        snapshot = r.s.snapshot(self.case,'a1',self.root)
        self.assertFalse(snapshot['complete'])
        self.assertIsNone(snapshot['sha256'])
        self.assertEqual(self.case,before)

    def test_resolver_independently_rejects_conflict_without_channel_call(self):
        self.action['right_ids'] = ['r1']
        snapshot = r.s.snapshot(self.case,'a1',self.root)
        self.assertFalse(snapshot['complete'])
        self.assertIsNone(snapshot['sha256'])

    def test_multiple_selected_rights_bind_all_materials(self):
        self.action['right_ids'] = ['r1','r2']
        approved = r.submission_approval(self.case,self.root)
        self.assertEqual(r.c.check(self.case,'a1',r.NOW)['state'],'checklist_ready_for_review')
        self.case['materials'][0]['sha256'] = r.write(self.root,'m1.bin',b'changed selected R1')
        self.assertEqual(r.s.review(self.case,'a1',approved,self.root,r.NOW)['state'],'needs_review')

    def test_omitted_scope_retains_legacy_all_rights(self):
        self.action.pop('right_ids')
        approved = r.submission_approval(self.case,self.root)
        self.assertEqual(r.c.check(self.case,'a1',r.NOW)['state'],'checklist_ready_for_review')
        (self.root/'m1.bin').unlink()
        self.assertEqual(r.s.review(self.case,'a1',approved,self.root,r.NOW)['state'],'needs_review')

    def test_explicit_valid_scope_does_not_read_unrelated_files(self):
        approved = r.submission_approval(self.case,self.root)
        (self.root/'m1.bin').unlink()
        self.assertEqual(r.s.review(self.case,'a1',approved,self.root,r.NOW)['state'],'approval_matches_snapshot')

    def test_invalid_explicit_right_lists_fail_both_modules(self):
        for value in ([],None,False,'r2',{},[None],['unknown'],['r2','r2']):
            with self.subTest(value=value):
                self.action['right_ids'] = value
                self.assert_scope_blocked()

    def test_unknown_mapped_right_is_not_inferred(self):
        self.action.pop('right_ids')
        self.action['channel_review']['work_mappings'][0]['right_id'] = 'missing'
        self.assert_scope_blocked()

    def test_right_case_and_brand_conflicts_fail_both_modules(self):
        for field in ('case_id','brand_id'):
            old = copy.deepcopy(self.case['rights'][1])
            self.case['rights'][1][field] = 'other'
            self.assert_scope_blocked()
            self.case['rights'][1] = old

    def test_malformed_work_mappings_fail_both_modules(self):
        for value in (None,[],{},False,'mapping',[None],[{}]):
            with self.subTest(value=value):
                self.action['channel_review']['work_mappings'] = value
                self.assert_scope_blocked()

    def test_mapping_other_case_target_is_not_action_scope(self):
        self.case['targets'].append({'id':'t2','supplied_url':'https://other.example/'})
        self.action['channel_review']['work_mappings'][0]['target_id'] = 't2'
        self.assert_scope_blocked()

    def test_mapping_change_within_scope_invalidates_previous_approval(self):
        self.action['right_ids'] = ['r1','r2']
        approved = r.submission_approval(self.case,self.root)
        self.action['channel_review']['work_mappings'][0]['right_id'] = 'r1'
        self.assertEqual(r.c.check(self.case,'a1',r.NOW)['state'],'checklist_ready_for_review')
        self.assertEqual(r.s.review(self.case,'a1',approved,self.root,r.NOW)['state'],'needs_review')

    def test_missing_mapped_file_blocks_approval(self):
        approved = r.submission_approval(self.case,self.root)
        (self.root/'m2.bin').unlink()
        result = r.s.review(self.case,'a1',approved,self.root,r.NOW)
        self.assertEqual(result['state'],'needs_review')
        self.assertIsNone(result['snapshot']['sha256'])

    def test_changed_mapped_bytes_without_record_update_blocks(self):
        approved = r.submission_approval(self.case,self.root)
        r.write(self.root,'m2.bin',b'changed without new record hash')
        self.assertEqual(r.s.review(self.case,'a1',approved,self.root,r.NOW)['state'],'needs_review')

    def test_one_bad_mapping_blocks_entire_action_in_either_order(self):
        good = self.action['channel_review']['work_mappings'][0]
        bad = copy.deepcopy(good)
        bad['right_id'] = 'r1'
        for rows in ([good,bad],[bad,good]):
            self.action['channel_review']['work_mappings'] = rows
            self.assert_scope_blocked()

    def test_conflicting_mapping_target_fields_are_rejected(self):
        self.action['channel_review']['work_mappings'][0]['target_ids'] = ['other']
        self.assert_scope_blocked()

    def test_channel_and_mapping_case_labels_are_checked(self):
        for name in ('channel','mapping'):
            row = self.action['channel_review']
            if name == 'mapping':
                row = row['work_mappings'][0]
            row['case_id'] = 'other'
            self.assert_scope_blocked()
            row.pop('case_id')

    def test_holder_case_conflict_is_rejected(self):
        self.case['parties'][0]['case_id'] = 'other'
        self.assert_scope_blocked()

    def test_invalid_non_object_channel_is_not_silently_ignored(self):
        for value in (None,False,[],0,'review'):
            self.action['channel_review'] = value
            self.assert_scope_blocked()

    def test_record_only_scope_changes_never_grant_submission(self):
        before = copy.deepcopy(self.case)
        approved = r.submission_approval(self.case,self.root)
        channel = r.c.check(self.case,'a1',r.NOW)
        submission = r.s.review(self.case,'a1',approved,self.root,r.NOW)
        self.assertEqual(submission['state'],'approval_matches_snapshot')
        self.assertFalse(channel['authorizes_submission'])
        self.assertFalse(submission['authorizes_submission'])
        self.assertEqual(self.case,before)


if __name__ == '__main__':
    unittest.main()
