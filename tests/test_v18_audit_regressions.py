"""Independent v1.8.0 acceptance probes; synthetic, offline and non-mutating.

Run in the repository checkout: python -B audit/audit_regressions.py
Failures expose missing contracts in the audited version. They are not the
repository's existing regression tests and must not be reported as passes.
"""
import copy
import hashlib
import http.client
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT / 'skills/brand-protection/scripts'))
sys.path[:0] = [str(ROOT/'tests'), str(SCRIPTS)]
import test_submission_review as s
import test_evidence_export as e
import test_investigation as i
import test_channel_check as c
import test_audit_regressions as a
import rdap_lookup as rdap
import capture_web as capture

OBSERVATIONS = []

def observe(test, actual):
    OBSERVATIONS.append({'test': test, 'actual': actual})

class AtomicAudit(unittest.TestCase):
    def test_control_valid_submission_matches_and_changed_body_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, approval = s.fixture(root)
            self.assertEqual(s.m.review(case,'a1',approval,root,s.NOW)['state'], 'approval_matches_snapshot')
            (root/'body.txt').write_bytes(b'changed body')
            self.assertEqual(s.m.review(case,'a1',approval,root,s.NOW)['state'], 'needs_review')

    def test_A01_rights_material_bytes_must_invalidate_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, approval = s.fixture(root)
            original = b'Synthetic original rights authorization'
            (root/'rights.bin').write_bytes(original)
            case['materials'] = [{'id':'m1','source_path':'rights.bin','brand_id':'brand-1',
                                  'sha256':hashlib.sha256(original).hexdigest()}]
            case['rights'][0]['material_ids'] = ['m1']
            approval['snapshot_sha256'] = s.m.snapshot(case,'a1',root)['sha256']
            (root/'rights.bin').write_bytes(b'Synthetic revoked or different authorization')
            case['materials'][0]['sha256'] = hashlib.sha256((root/'rights.bin').read_bytes()).hexdigest()
            result = s.m.review(case,'a1',approval,root,s.NOW)
            observe('A01_rights', {'state':result['state'], 'hash_unchanged':result['snapshot']['sha256']==approval['snapshot_sha256']})
            self.assertEqual(result['state'],'needs_review')

    def test_A01_counter_evidence_bytes_must_invalidate_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, approval = s.fixture(root)
            (root/'counter.bin').write_bytes(b'Synthetic original counter evidence')
            case['evidence'].append({'id':'counter','brand_id':'brand-1','original_path':'counter.bin',
                                     'sha256':hashlib.sha256((root/'counter.bin').read_bytes()).hexdigest()})
            case['allegations'] = [{'id':'claim-1','target_id':'target-1','counter_evidence_ids':['counter']}]
            approval['snapshot_sha256'] = s.m.snapshot(case,'a1',root)['sha256']
            (root/'counter.bin').write_bytes(b'Synthetic new evidence of a valid license')
            case['evidence'][-1]['sha256'] = hashlib.sha256((root/'counter.bin').read_bytes()).hexdigest()
            result = s.m.review(case,'a1',approval,root,s.NOW)
            observe('A01_counter', {'state':result['state'], 'hash_unchanged':result['snapshot']['sha256']==approval['snapshot_sha256']})
            self.assertEqual(result['state'],'needs_review')

    def test_A01_missing_counter_evidence_reference_must_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, approval = s.fixture(root)
            case['allegations'] = [{'id':'claim-1','target_id':'target-1','counter_evidence_ids':['absent-counter']}]
            approval['snapshot_sha256'] = s.m.snapshot(case,'a1',root)['sha256']
            result = s.m.review(case,'a1',approval,root,s.NOW)
            observe('A01_missing_counter', {'state':result['state']})
            self.assertEqual(result['state'],'needs_review')

    def test_A02_export_must_reject_explicit_other_brand_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, review = e.fixtures(root)
            case['brand_id'] = 'brand-A'
            case['evidence'][1]['brand_id'] = 'brand-B'
            accepted = True
            try:
                files, _ = e.m.prepare(case,review,root,e.NOW)
            except ValueError:
                accepted = False
            observe('A02_export_other_brand', {'accepted':accepted})
            self.assertFalse(accepted)

    def test_A02_investigation_must_reject_explicit_other_brand_evidence(self):
        case = i.fixture()
        case['brand_id'] = 'brand-A'
        case['evidence'][0]['brand_id'] = 'brand-B'
        case['investigation']['results'][0]['relationship'] = 'owned'
        result = i.m.assess(case,i.NOW)
        observe('A02_investigation_other_brand', {'errors':result['errors'], 'next_steps':[r['next_step'] for r in result['results']]})
        self.assertTrue(result['errors'])

    def test_A02_provider_attribution_must_reject_other_brand_evidence(self):
        case = c.fixture()
        case['evidence'].append({'id':'attribution-other','brand_id':'brand-B'})
        case['providers'][0]['attribution_evidence_ids'] = ['attribution-other']
        result = c.m.check(case,'a1',c.NOW)
        observe('A02_provider_other_brand', {'state':result['state']})
        self.assertEqual(result['state'],'needs_review')

    def test_A03_settled_initial_notice_must_not_block_followup(self):
        case = a.case_record()
        old = a.task('initial',kind='initial_notice',status='completed')
        case['followup_tasks'] = [old,a.task('followup')]
        case['outbound_attempts'] = [{'id':'sent-first','task_id':'initial','action_id':'action-1',
                                     'dedupe_key':old['dedupe_key'],'status':'sent'}]
        result = a.module.build_queue(case,a.NOW)
        observe('A03_initial_notice_history', {'errors':result['errors'], 'held':result['held_tasks'], 'due':result['due_tasks']})
        self.assertIn('followup',[row['id'] for row in result['due_tasks']])

    def test_A04_provider_target_must_match_action_scope(self):
        case = c.fixture()
        case['targets'].append({'id':'t2','supplied_url':'https://different.example/item'})
        case['providers'][0]['target_id'] = 't2'
        result = c.m.check(case,'a1',c.NOW)
        observe('A04_provider_wrong_target', {'state':result['state'], 'errors':result['errors']})
        self.assertEqual(result['state'],'needs_review')

    def test_A05_source_archive_output_must_not_enter_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base/'unpacked-repository'
            shutil.copytree(SCRIPTS.parent,repo/'skills/brand-protection')
            (repo/'release').mkdir()
            (repo/'release/skill-files.json').write_bytes((ROOT/'release/skill-files.json').read_bytes())
            (repo/'README.md').write_bytes((ROOT/'README.md').read_bytes())
            out = repo/'private-output'
            out.mkdir()
            casefile = base/'case.json'
            casefile.write_text(json.dumps({'case_id':'synthetic','targets':[],'actions':[],'deadlines':[]}),encoding='utf-8')
            result = subprocess.run([sys.executable,'-B',str(repo/'skills/brand-protection/scripts/case_dashboard.py'),
                '--case',str(casefile),'--output-root',str(out)],capture_output=True,text=True,timeout=15)
            observe('A05_unpacked_source_output', {'returncode':result.returncode,'files_created':len(list(out.rglob('*')))})
            self.assertNotEqual(result.returncode,0)

    def test_A06_truncated_rdap_transport_must_not_claim_registration(self):
        body = b'{"objectClassName":"domain","ldhName":"EXAMPLE.COM"}'
        raw = b'HTTP/1.1 200 OK\r\nContent-Type: application/rdap+json\r\nContent-Length: 900\r\n\r\n'+body
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(raw)
        response = http.client.HTTPResponse(sock)
        response.begin()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(rdap.http.client,'HTTPSConnection',return_value=connection):
            result = rdap.lookup('example.com','synthetic',allow_network=True,now=s.NOW)
        observe('A06_truncated_rdap', {'registration':result['result']['registration'],'errors':result['errors'],
                                     'announced_bytes':900,'actual_bytes':len(body)})
        self.assertTrue(result['errors'])
        self.assertEqual(result['result']['registration'],'unknown')

    def test_control_unknown_delivery_still_blocks_new_round(self):
        result = a.module.build_queue(a.unresolved_claim('interrupted'),a.NOW)
        self.assertNotIn('new',[row['id'] for row in result['due_tasks']])
        self.assertIn('urgent',[row['id'] for row in result['deadline_alerts']])

    def test_control_capture_blocks_private_dns(self):
        import socket
        resolve = lambda *args,**kwargs: [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        transport = Mock()
        with self.assertRaises(ValueError):
            capture.collect('https://example.com',['example.com'],allow_network=True,resolver=resolve,transport=transport)
        transport.assert_not_called()

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AtomicAudit)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'baseline_commit':'93c23f483bdd77d728c451343b894e15dae3db4a',
              'python':sys.version.split()[0], 'tests':result.testsRun,
              'failures':[test.id() for test,_ in result.failures],
              'errors':[{'test':test.id(),'traceback':trace} for test,trace in result.errors],
              'observations':OBSERVATIONS,'synthetic_only':True,'production_files_modified':False}
    print('ATOMIC_AUDIT_JSON='+json.dumps(report,ensure_ascii=True,sort_keys=True))
    sys.exit(0 if result.wasSuccessful() else 1)
