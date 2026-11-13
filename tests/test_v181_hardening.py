"""Additional synthetic contracts for v1.8.1; no complaint or private data."""
import copy
import hashlib
import http.client
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_submission_review as s
import test_channel_check as c
import test_investigation as i
import test_evidence_export as e
import test_audit_regressions as q
import capture_web as capture
import rdap_lookup as rdap
import record_integrity as integrity
import submission_dependencies as deps


class DependencyContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case, self.approval = s.fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def evidence(self, key, **values):
        data = ('synthetic evidence ' + key).encode()
        (self.root/(key+'.bin')).write_bytes(data)
        row = {'id':key, 'brand_id':'brand-1', 'original_path':key+'.bin',
               'sha256':hashlib.sha256(data).hexdigest(), **values}
        self.case['evidence'].append(row)
        return row

    def material(self, key, **values):
        data = ('synthetic material ' + key).encode()
        (self.root/(key+'.bin')).write_bytes(data)
        row = {'id':key, 'brand_id':'brand-1', 'source_path':key+'.bin',
               'sha256':hashlib.sha256(data).hexdigest(), **values}
        self.case.setdefault('materials', []).append(row)
        return row

    def approve(self):
        value = s.m.snapshot(self.case, 'a1', self.root)
        self.assertIs(value['complete'], True)
        self.approval['snapshot_sha256'] = value['sha256']
        return value

    def blocked(self):
        value = s.m.review(self.case, 'a1', self.approval, self.root, s.NOW)
        self.assertEqual(value['state'], 'needs_review')
        self.assertFalse(value['authorizes_submission'])
        self.assertTrue(value['errors'])
        return value

    def change(self, row):
        path = self.root/row.get('original_path', row.get('source_path'))
        path.write_bytes(b'changed synthetic authorization or fact')
        row['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()

    def test_authority_party_file_change_invalidates(self):
        row = self.evidence('authority')
        self.case['parties'][0]['authorization_evidence_ids'] = ['authority']
        self.approve(); self.change(row); self.blocked()

    def test_action_authority_material_change_invalidates(self):
        row = self.material('authority')
        self.case['actions'][0]['authorization_material_ids'] = ['authority']
        self.approve(); self.change(row); self.blocked()

    def test_observation_bytes_change_invalidates(self):
        row = self.evidence('observed')
        self.case['observations'] = [{'id':'obs', 'target_id':'target-1', 'evidence_ids':['observed']}]
        self.approve(); self.change(row); self.blocked()

    def test_attribution_bytes_change_invalidates(self):
        row = self.evidence('attribution')
        self.case['providers'][0]['attribution_evidence_ids'] = ['attribution']
        self.approve(); self.change(row); self.blocked()

    def test_parent_bytes_change_invalidates(self):
        row = self.evidence('parent')
        self.case['evidence'][0]['derived_from_id'] = 'parent'
        self.approve(); self.change(row); self.blocked()

    def test_missing_dependency_issues_no_approvable_digest(self):
        self.case['rights'][0]['material_ids'] = ['absent']
        value = s.m.snapshot(self.case,'a1',self.root)
        self.assertFalse(value['complete']); self.assertIsNone(value['sha256'])
        self.approval['snapshot_sha256'] = None
        self.blocked()

    def test_byte_change_without_record_update_is_rejected(self):
        row = self.material('right')
        self.case['rights'][0]['material_ids'] = ['right']
        self.approve()
        (self.root/row['source_path']).write_bytes(b'changed without metadata update')
        self.blocked()

    def test_cross_case_material_is_rejected(self):
        self.material('right', case_id='different-case')
        self.case['rights'][0]['material_ids'] = ['right']
        self.blocked()

    def test_cross_brand_material_is_rejected(self):
        self.material('right', brand_id='different-brand')
        self.case['rights'][0]['material_ids'] = ['right']
        self.blocked()

    def test_cross_brand_counter_evidence_is_rejected(self):
        self.evidence('counter', brand_id='different-brand')
        self.case['allegations'] = [{'id':'claim', 'counter_evidence_ids':['counter']}]
        self.blocked()

    def test_cross_namespace_cycle_is_rejected(self):
        self.material('right', evidence_ids=['e1'])
        self.case['evidence'][0]['material_ids'] = ['right']
        self.blocked()

    def test_evidence_parent_cycle_is_rejected(self):
        self.evidence('parent', derived_from_id='e1')
        self.case['evidence'][0]['derived_from_id'] = 'parent'
        self.blocked()

    def test_duplicate_material_ids_are_rejected(self):
        row = self.material('right')
        self.case['materials'].append(copy.deepcopy(row))
        self.case['rights'][0]['material_ids'] = ['right']
        self.blocked()

    def test_dependency_budget_fails_closed(self):
        with patch.object(deps,'TOTAL_BYTES',1):
            self.blocked()

    def test_unknown_right_in_explicit_scope_blocks(self):
        self.case['actions'][0]['right_ids'] = ['missing']
        self.blocked()

    def test_unrelated_material_is_not_read_or_hashed(self):
        self.approve()
        self.material('unused')
        self.change(self.case['materials'][0])
        self.assertEqual(s.m.review(self.case,'a1',self.approval,self.root,s.NOW)['state'],'approval_matches_snapshot')

    def test_explicit_other_target_context_does_not_invalidate(self):
        self.approve()
        self.case['targets'].append({'id':'other-target'})
        self.case['allegations'] = [{'id':'other-claim','target_id':'other-target','counter_evidence_ids':['not-local']}]
        self.assertEqual(s.m.review(self.case,'a1',self.approval,self.root,s.NOW)['state'],'approval_matches_snapshot')

    def test_unscoped_counter_evidence_is_not_discarded(self):
        self.case['allegations'] = [{'id':'casewide','counter_evidence_ids':['missing']}]
        self.blocked()

    def test_explicit_right_scope_excludes_unrelated_right(self):
        action = self.case['actions'][0]
        action.update(right_ids=['right-1'], authority_party_ids=['party-1'])
        self.approve()
        self.case['rights'].append({'id':'other','brand_id':'other-brand','holder_party_id':'unknown','material_ids':['absent']})
        self.assertEqual(s.m.review(self.case,'a1',self.approval,self.root,s.NOW)['state'],'approval_matches_snapshot')

    def test_inputs_are_not_modified(self):
        self.material('right')
        self.case['rights'][0]['material_ids'] = ['right']
        before = copy.deepcopy(self.case)
        self.approve()
        self.assertEqual(self.case,before)


class ScopeContracts(unittest.TestCase):
    def test_investigation_parent_brand_conflict_blocks(self):
        case = i.fixture(); case['brand_id']='A'
        case['evidence'][0]['derived_from_id']='parent'
        case['evidence'].append({'id':'parent','brand_id':'B'})
        self.assertTrue(i.m.assess(case,i.NOW)['errors'])

    def test_investigation_explicit_other_target_blocks(self):
        case=i.fixture(); case['evidence'][0]['target_id']='other'
        self.assertTrue(i.m.assess(case,i.NOW)['errors'])

    def test_investigation_cross_case_evidence_blocks(self):
        case=i.fixture(); case['evidence'][0]['case_id']='other'
        self.assertTrue(i.m.assess(case,i.NOW)['errors'])

    def test_export_parent_brand_conflict_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); case,review=e.fixtures(root)
            case['brand_id']='A';case['evidence'][0]['brand_id']='B'
            with self.assertRaises(ValueError): e.m.prepare(case,review,root,e.NOW)

    def test_export_cross_case_evidence_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); case,review=e.fixtures(root)
            case['evidence'][1]['case_id']='other'
            with self.assertRaises(ValueError): e.m.prepare(case,review,root,e.NOW)

    def test_missing_provider_target_is_not_inferred(self):
        case=c.fixture();case['providers'][0].pop('target_id')
        self.assertEqual(c.m.check(case,'a1',c.NOW)['state'],'needs_review')

    def test_attribution_wrong_target_evidence_blocks(self):
        case=c.fixture();case['evidence'][0]['target_id']='other'
        self.assertEqual(c.m.check(case,'a1',c.NOW)['state'],'needs_review')

    def test_attribution_parent_brand_conflict_blocks(self):
        case=c.fixture();case['evidence'][0]['derived_from_id']='parent'
        case['evidence'].append({'id':'parent','brand_id':'other'})
        self.assertEqual(c.m.check(case,'a1',c.NOW)['state'],'needs_review')

    def two_targets(self):
        case=c.fixture()
        case['targets'].append({'id':'t2','supplied_url':'https://second.example/item'})
        case['actions'][0]['target_ids'].append('t2')
        mapping=copy.deepcopy(case['actions'][0]['channel_review']['work_mappings'][0])
        mapping.update(target_id='t2',reported_url='https://second.example/item',comparison_evidence_ids=['e2'])
        case['actions'][0]['channel_review']['work_mappings'].append(mapping)
        case['evidence'][0]['target_id']='t1'
        case['evidence'].append({'id':'e2','target_id':'t2','brand_id':'b1'})
        return case

    def test_multi_target_partial_attribution_blocks(self):
        case=self.two_targets()
        self.assertEqual(c.m.check(case,'a1',c.NOW)['state'],'needs_review')

    def test_multi_target_independent_attribution_passes(self):
        case=self.two_targets();p=case['providers'][0]
        p.pop('target_id');p['target_ids']=['t1','t2']
        p['target_attributions']=[{'target_id':target,'evidence_ids':[key],'verified_at':p['verified_at']}
                                  for target,key in [('t1','e1'),('t2','e2')]]
        value=c.m.check(case,'a1',c.NOW)
        self.assertEqual(value['state'],'checklist_ready_for_review',value)
        self.assertFalse(value['authorizes_submission'])

    def test_target_attribution_after_review_blocks(self):
        case=c.fixture();p=case['providers'][0]
        p['target_attributions']=[{'target_id':'t1','evidence_ids':['e1'],'verified_at':'2026-11-13T03:30:00Z'}]
        self.assertEqual(c.m.check(case,'a1',c.NOW)['state'],'needs_review')

    def test_invalid_optional_identity_is_not_ignored(self):
        for value in (None,0,False,[],{},''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                integrity.check_identity({'brand_id':'A'},{'brand_id':value},'row')


class InitialNoticeContracts(unittest.TestCase):
    def case(self,state):
        case=q.case_record()
        old=q.task('initial',kind='initial_notice',status='completed')
        case['followup_tasks']=[old,q.task('followup')]
        case['outbound_attempts']=[{'id':'first','task_id':'initial','action_id':'action-1',
                                   'dedupe_key':old['dedupe_key'],'status':state}]
        return case

    def test_confirmed_not_sent_allows_distinct_next_task(self):
        result=q.module.build_queue(self.case('confirmed_not_sent'),q.NOW)
        self.assertIn('followup',[x['id'] for x in result['due_tasks']])

    def test_pending_initial_send_blocks_all_new_rounds(self):
        for state in ('prepared','in_flight','outcome_unknown'):
            with self.subTest(state=state):
                result=q.module.build_queue(self.case(state),q.NOW)
                self.assertNotIn('followup',[x['id'] for x in result['due_tasks']])

    def test_active_initial_worker_blocks_without_send_record(self):
        case=self.case('sent');case['outbound_attempts']=[]
        case['followup_tasks'][0]['status']='in_progress'
        result=q.module.build_queue(case,q.NOW)
        self.assertNotIn('followup',[x['id'] for x in result['due_tasks']])

    def test_duplicate_initial_key_is_still_blocked(self):
        case=self.case('sent')
        case['followup_tasks'][1]['dedupe_key']=case['followup_tasks'][0]['dedupe_key']
        result=q.module.build_queue(case,q.NOW)
        self.assertNotIn('followup',[x['id'] for x in result['due_tasks']])

    def test_initial_notice_is_outbound_not_readonly(self):
        self.assertIn('initial_notice',q.module.OUTBOUND)
        self.assertIn('initial_notice',s.m.OPERATIONS)


class PrivateRootContracts(unittest.TestCase):
    def layout(self,base,relative):
        skill=base/relative;(skill/'scripts').mkdir(parents=True)
        return skill

    def test_source_archive_root_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);repo=base/'repo';skill=self.layout(repo,'skills/brand-protection')
            (repo/'release').mkdir();(repo/'release/skill-files.json').write_text('[]');(repo/'README.md').write_text('synthetic')
            out=repo/'nested';out.mkdir()
            with patch.object(capture,'__file__',str(skill/'scripts/capture_web.py')),self.assertRaises(ValueError):
                capture.private_root(out)

    def test_standalone_install_allows_separate_private_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);skill=self.layout(base,'install/brand-protection');out=base/'private';out.mkdir()
            with patch.object(capture,'__file__',str(skill/'scripts/capture_web.py')):
                self.assertEqual(capture.private_root(out),out.resolve())

    def test_arbitrary_readme_does_not_protect_whole_home(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'README.md').write_text('unrelated');skill=self.layout(base,'skills/brand-protection');out=base/'private';out.mkdir()
            with patch.object(capture,'__file__',str(skill/'scripts/capture_web.py')):
                self.assertEqual(capture.private_root(out),out.resolve())

    def test_host_scan_directory_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);skill=self.layout(base,'.agents/skills/brand-protection');out=skill.parent/'output';out.mkdir()
            with patch.object(capture,'__file__',str(skill/'scripts/capture_web.py')),self.assertRaises(ValueError):
                capture.private_root(out)

    def test_git_worktree_file_marker_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);skill=self.layout(base,'skills/brand-protection');(base/'.git').write_text('gitdir: synthetic')
            with patch.object(capture,'__file__',str(skill/'scripts/capture_web.py')),self.assertRaises(ValueError):
                capture.private_root(base)


class HttpFramingContracts(unittest.TestCase):
    BODY=b'{"objectClassName":"domain","ldhName":"EXAMPLE.COM"}'

    def query(self,headers,body=None):
        raw=b'HTTP/1.1 200 OK\r\nContent-Type: application/rdap+json\r\n'+headers+b'\r\n'+(self.BODY if body is None else body)
        sock=Mock();sock.makefile.return_value=io.BytesIO(raw)
        response=http.client.HTTPResponse(sock);response.begin()
        conn=Mock();conn.getresponse.return_value=response
        with patch.object(rdap.http.client,'HTTPSConnection',return_value=conn):
            result=rdap.lookup('example.com','synthetic',allow_network=True,now=s.NOW)
        conn.close.assert_called_once()
        return result

    def fail(self,result):
        self.assertTrue(result['errors']);self.assertEqual(result['result']['registration'],'unknown')
        self.assertEqual(result['evidence'],[])

    def test_exact_content_length_succeeds(self):
        value=self.query(('Content-Length: %d\r\n'%len(self.BODY)).encode())
        self.assertEqual(value['result']['registration'],'registered');self.assertFalse(value['errors'])

    def test_short_valid_json_is_incomplete(self):
        self.fail(self.query(b'Content-Length: 900\r\n'))

    def test_invalid_and_conflicting_lengths_fail(self):
        for length in (b'-1',b'abc',b'49, 49',b'0\r\nContent-Length: 49'):
            with self.subTest(length=length): self.fail(self.query(b'Content-Length: '+length+b'\r\n'))

    def test_oversized_declared_length_fails(self):
        self.fail(self.query(('Content-Length: %d\r\n'%(rdap.LIMIT+1)).encode()))

    def test_compression_is_not_silently_accepted(self):
        self.fail(self.query(b'Content-Encoding: gzip\r\n'))

    def test_chunked_complete_succeeds(self):
        body=('%x\r\n'%len(self.BODY)).encode()+self.BODY+b'\r\n0\r\n\r\n'
        value=self.query(b'Transfer-Encoding: chunked\r\n',body)
        self.assertEqual(value['result']['registration'],'registered')

    def test_chunked_missing_terminator_fails(self):
        body=('%x\r\n'%len(self.BODY)).encode()+self.BODY+b'\r\n'
        self.fail(self.query(b'Transfer-Encoding: chunked\r\n',body))

    def test_transfer_and_length_conflict_fails(self):
        self.fail(self.query(b'Transfer-Encoding: chunked\r\nContent-Length: 49\r\n'))

    def test_unsupported_transfer_encoding_fails(self):
        self.fail(self.query(b'Transfer-Encoding: gzip, chunked\r\n'))


class GraphContracts(unittest.TestCase):
    def test_long_chain_is_linear_and_iterative(self):
        n=4000
        actions={str(k):{'corrects_action_id':str(k-1) if k else None} for k in range(n)}
        with patch.object(integrity,'identifier',wraps=integrity.identifier) as check:
            result=s.m.family(actions,str(n-1))
        self.assertEqual(len(result),n)
        self.assertLessEqual(check.call_count,3*n)

    def test_disconnected_cycle_is_rejected(self):
        with self.assertRaises(ValueError):
            s.m.family({'a':{},'b':{'corrects_action_id':'c'},'c':{'corrects_action_id':'b'}},'a')

    def test_unrelated_action_is_not_in_family(self):
        self.assertEqual(s.m.family({'a':{},'b':{'corrects_action_id':'a'},'c':{}},'b'),{'a','b'})

    def test_oversized_action_graph_is_rejected(self):
        with patch.object(integrity,'MAX_RECORDS',2),self.assertRaises(ValueError):
            s.m.family({'a':{},'b':{},'c':{}},'a')


if __name__=='__main__':
    unittest.main()
