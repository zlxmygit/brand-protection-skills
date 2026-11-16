"""Synthetic local host transactions; no mailbox, sender or scheduler connection."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_followup_queue as queue_base

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('host_adapter', SCRIPTS/'host_adapter.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026,11,16,4,tzinfo=timezone.utc)


def fixture(root):
    receipt = b'{"synthetic_feedback_receipt":true}'
    (root/'receipt.json').write_bytes(receipt)
    receipt_hash = hashlib.sha256(receipt).hexdigest()
    case = {'case_id':'synthetic-host','brand_id':'synthetic-brand','revision':0,
            'actions':[{'id':'a1','status':'submitted'}], 'outbound_attempts':[], 'runs':[],
            'evidence':[{'id':'receipt-evidence','original_path':'receipt.json','sha256':receipt_hash}],
            'follow_up':{'requested':True,'enabled':True,
                'host_adapter':{'version':'1.0','status':'active','authorization_source':'synthetic operator',
                                'expected_interval_hours':6,'grace_hours':1},
                'scheduler':{'status':'configured','automation_id':'synthetic-scheduler','kind':'synthetic-host',
                             'configuration_verified_at':'2026-11-16T01:00:00Z',
                             'health_observer':{'mode':'independent','status':'claimed'}},
                'feedback_sources':[{'id':'mail-source','account_scope':'synthetic-account',
                    'authorized_thread_or_ticket_ids':['thread-1','thread-2'],
                    'authorization_source':'synthetic limited feedback access'}],
                'processed_message_ids':[],'source_cursors':[]}}
    message = {'source_id':'mail-source','account_scope':'synthetic-account',
               'thread_or_ticket_id':'thread-1','message_id':'same-message-id','content_sha256':'a'*64,
               'derived_state':{'category':'needs_information','summary':'Synthetic request for evidence',
                                'observed_at':'2026-11-16T03:01:00Z','action_id':'a1',
                                'effect':'requires_review','reason':'Synthetic evidence request needs operator review',
                                'review_at':'2026-11-16T05:00:00Z'}}
    batch = {'source_id':'mail-source','account_scope':'synthetic-account',
             'covered_thread_or_ticket_ids':['thread-1','thread-2'],'from_cursor':None,
             'read_at':'2026-11-16T03:02:00Z','read_receipt':{'path':'receipt.json','sha256':receipt_hash},
             'pages':[{'cursor':None,'next_cursor':'cursor-1','complete':True,'messages':[message]}]}
    request = {'version':'1.0','expected_revision':0,
               'run':{'id':'run-1','kind':'scheduled','scheduler_id':'synthetic-scheduler',
                      'started_at':'2026-11-16T03:00:00Z','completed_at':'2026-11-16T03:03:00Z',
                      'source_read_results':[{'source_id':'mail-source','account_scope':'synthetic-account','status':'complete'}],
                      'errors':[]}, 'feedback_batches':[batch]}
    return case,request


class HostAdapterContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.case,self.request = fixture(self.root)
        self.case_path = self.root/'case.json'
        self.request_path = self.root/'request.json'
        self.save()

    def save(self):
        self.case_path.write_text(json.dumps(self.case),encoding='utf-8')
        self.request_path.write_text(json.dumps(self.request),encoding='utf-8')

    def commit(self):
        return m.commit(self.root,self.case_path,self.request_path,NOW)

    def read(self):
        return m.read_json(self.case_path)

    def assert_rejected_unchanged(self):
        self.save()
        before = self.case_path.read_bytes()
        with self.assertRaises((ValueError,OSError,TypeError,KeyError)):
            self.commit()
        self.assertEqual(self.case_path.read_bytes(),before)

    def control(self, previous, following, run_id):
        self.request = {'version':'1.0','expected_revision':self.case['revision'],
            'run':{'id':run_id,'kind':'manual_control','scheduler_id':None,
                'started_at':'2026-11-16T03:00:00Z','completed_at':'2026-11-16T03:03:00Z',
                'source_read_results':[],'errors':[]},
            'transition':{'from':previous,'to':following,'authorization_source':'synthetic explicit instruction',
                          'recorded_at':'2026-11-16T03:00:00Z'}}

    def test_default_status_and_validation_do_not_write(self):
        before = {path.name:path.read_bytes() for path in self.root.iterdir()}
        result = m.prepare(self.case,self.request,self.root,NOW)
        self.assertEqual(result['revision'],1)
        m.health(self.case,NOW)
        self.assertEqual({path.name:path.read_bytes() for path in self.root.iterdir()},before)
        self.assertEqual(m.health(self.case,NOW)['state'],'unverified')

    def test_one_atomic_commit_persists_state_keys_cursor_and_run(self):
        result = self.commit()
        saved = self.read()
        self.assertEqual(result['state'],'committed')
        self.assertTrue(result['persisted_verified'])
        self.assertFalse(result['authorizes_submission'])
        follow = saved['follow_up']
        self.assertEqual(saved['revision'],1)
        self.assertEqual(follow['source_cursors'][0]['cursor'],'cursor-1')
        self.assertEqual(follow['source_cursors'][0]['committed_revision'],1)
        self.assertEqual(follow['feedback_events'][0]['derived_state']['category'],'needs_information')
        self.assertEqual(follow['feedback_review_items'][0]['status'],'open')
        self.assertEqual(follow['processed_message_ids'],[follow['feedback_events'][0]['message_key']])
        self.assertEqual(saved['runs'][0]['record_revision_after'],1)
        report = m.health(saved,NOW)
        self.assertEqual(report['state'],'healthy')
        self.assertEqual(report['independent_observer'],'unverified')
        self.assertEqual(report['connector_access'],'unverified')

    def test_revision_cas_rejects_stale_request(self):
        self.commit()
        before = self.case_path.read_bytes()
        with self.assertRaisesRegex(ValueError,'compare-and-swap'):
            self.commit()
        self.assertEqual(self.case_path.read_bytes(),before)

    def test_real_os_lock_blocks_another_process_and_recovers_after_death(self):
        code = ('import sys; from pathlib import Path; '
                'sys.path.insert(0,sys.argv[1]); import host_adapter as m; '
                'lock=m.case_lock(Path(sys.argv[2])); lock.__enter__(); '
                'print("locked",flush=True); sys.stdin.read()')
        process = subprocess.Popen([sys.executable,'-B','-c',code,str(SCRIPTS),str(self.case_path)],
                                   stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            before = self.case_path.read_bytes()
            with self.assertRaises(m.LockBusy):
                self.commit()
            self.assertEqual(self.case_path.read_bytes(),before)
        finally:
            process.terminate()
            process.communicate(timeout=10)
        self.assertEqual(self.commit()['revision'],1)

    def test_fsync_and_replace_failures_preserve_old_cursor_and_remove_only_temp(self):
        for method in ('fsync','replace'):
            with self.subTest(method=method):
                self.save()
                before = self.case_path.read_bytes()
                with patch.object(m.os,method,side_effect=OSError('synthetic disk failure')):
                    with self.assertRaises(OSError):
                        self.commit()
                self.assertEqual(self.case_path.read_bytes(),before)
                self.assertFalse(list(self.root.glob('*.tmp')))
                self.assertEqual(self.read()['follow_up']['source_cursors'],[])

    def test_missing_pages_coverage_or_message_state_never_advance_cursor(self):
        original = copy.deepcopy(self.request)
        variants = []
        changed = copy.deepcopy(original)
        changed['feedback_batches'][0]['pages'][0]['complete'] = False
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed['feedback_batches'][0]['covered_thread_or_ticket_ids'] = ['thread-1']
        variants.append(changed)
        changed = copy.deepcopy(original)
        first = changed['feedback_batches'][0]['pages'][0]
        first['complete'] = False
        changed['feedback_batches'][0]['pages'].append({'cursor':'missing-page-cursor',
             'next_cursor':'cursor-3','complete':True,'messages':[]})
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed['feedback_batches'][0]['pages'][0]['messages'][0].pop('derived_state')
        variants.append(changed)
        for request in variants:
            with self.subTest(request=request):
                self.request = request
                self.assert_rejected_unchanged()

    def test_cross_source_account_and_thread_are_rejected(self):
        original = copy.deepcopy(self.request)
        for field,value in (('source_id','another-source'),('account_scope','another-account'),
                            ('thread_or_ticket_id','unauthorized-thread')):
            self.request = copy.deepcopy(original)
            self.request['feedback_batches'][0]['pages'][0]['messages'][0][field] = value
            self.assert_rejected_unchanged()

    def test_failed_reads_do_not_advance_any_cursor(self):
        self.request['run']['errors'] = ['synthetic connector failure']
        self.assert_rejected_unchanged()
        self.request.pop('feedback_batches')
        self.request['run']['source_read_results'][0]['status'] = 'failed'
        self.save()
        self.commit()
        self.assertEqual(self.read()['follow_up']['source_cursors'],[])
        self.assertEqual(m.health(self.read(),NOW)['state'],'unverified')

    def test_source_and_account_are_in_message_dedupe_keys(self):
        source = copy.deepcopy(self.case['follow_up']['feedback_sources'][0])
        source.update(id='second-source',account_scope='second-account')
        self.case['follow_up']['feedback_sources'].append(source)
        batch = copy.deepcopy(self.request['feedback_batches'][0])
        batch.update(source_id='second-source',account_scope='second-account')
        batch['pages'][0]['messages'][0].update(source_id='second-source',account_scope='second-account')
        self.request['feedback_batches'].append(batch)
        self.request['run']['source_read_results'].append({'source_id':'second-source',
            'account_scope':'second-account','status':'complete'})
        self.save()
        self.commit()
        keys = self.read()['follow_up']['processed_message_ids']
        self.assertEqual(len(keys),2)
        self.assertNotEqual(keys[0],keys[1])

    def test_replayed_message_is_deduplicated_but_conflicting_content_is_rejected(self):
        self.commit()
        self.case = self.read()
        self.request['expected_revision'] = 1
        self.request['run']['id'] = 'run-2'
        batch = self.request['feedback_batches'][0]
        batch['from_cursor'] = 'cursor-1'
        batch['pages'][0]['cursor'] = 'cursor-1'
        batch['pages'][0]['next_cursor'] = 'cursor-2'
        self.save()
        self.commit()
        self.assertEqual(len(self.read()['follow_up']['processed_message_ids']),1)
        self.case = self.read()
        self.request['expected_revision'] = 2
        self.request['run']['id'] = 'run-3'
        batch['from_cursor'] = 'cursor-2'
        batch['pages'][0].update(cursor='cursor-2',next_cursor='cursor-3')
        batch['pages'][0]['messages'][0]['content_sha256'] = 'b'*64
        self.assert_rejected_unchanged()

    def test_unknown_send_history_cannot_be_deleted_rekeyed_or_silently_cleared(self):
        for state in ('prepared','in_flight','outcome_unknown'):
            attempt = {'id':'pending','action_id':'a1','dedupe_key':'original-key','status':state}
            self.case['outbound_attempts'] = [attempt]
            for variant in ([],[dict(attempt,dedupe_key='new-key')],
                            [dict(attempt,status='confirmed_not_sent')]):
                with self.subTest(state=state,variant=variant):
                    self.request['records'] = {'outbound_attempts':variant}
                    self.assert_rejected_unchanged()

    def test_pending_delivery_action_and_task_history_cannot_be_changed_or_removed(self):
        self.case['followup_tasks'] = [{'id':'task-1','action_id':'a1','kind':'remind','status':'in_progress'}]
        self.case['outbound_attempts'] = [{'id':'pending','action_id':'a1','task_id':'task-1',
                                         'dedupe_key':'original-key','status':'outcome_unknown'}]
        for records in ({'actions':[]},{'followup_tasks':[]},
                        {'actions':[{'id':'a1','status':'withdrawn'}]},
                        {'followup_tasks':[{'id':'task-1','action_id':'another','status':'closed'}]}):
            self.request['records'] = records
            self.assert_rejected_unchanged()

    def test_declared_action_and_deadline_effects_must_both_persist_before_cursor(self):
        action = {'id':'a1','status':'needs_information'}
        deadline = {'id':'deadline-1','action_id':'a1','due_at':'2026-11-17T04:00:00Z'}
        derived = self.request['feedback_batches'][0]['pages'][0]['messages'][0]['derived_state']
        derived.update(effect='records_updated',record_refs=[
            {'section':'actions','id':'a1','after_sha256':m.record_digest(action)},
            {'section':'deadlines','id':'deadline-1','after_sha256':m.record_digest(deadline)}])
        derived.pop('reason')
        derived.pop('review_at')
        for records in ({'actions':[action]}, {'deadlines':[deadline]}):
            self.request['records'] = records
            self.assert_rejected_unchanged()
        self.request['records'] = {'actions':[action],'deadlines':[deadline]}
        self.save()
        self.commit()
        self.assertEqual(self.read()['deadlines'],[deadline])
        self.assertEqual(self.read()['actions'],[action])

    def test_no_change_requires_a_reason_and_review_requires_an_open_item(self):
        derived = self.request['feedback_batches'][0]['pages'][0]['messages'][0]['derived_state']
        derived.update(effect='no_change')
        derived.pop('reason')
        self.assert_rejected_unchanged()
        derived['reason'] = 'Synthetic duplicate acknowledgement has no new business facts'
        self.save()
        self.commit()
        self.assertEqual(self.read()['follow_up']['feedback_events'][0]['derived_state']['effect'],'no_change')

    def test_terminal_reconciliation_requires_verified_preserved_evidence(self):
        attempt = {'id':'pending','action_id':'a1','dedupe_key':'original-key','status':'outcome_unknown'}
        self.case['outbound_attempts'] = [attempt]
        resolved = dict(attempt,status='confirmed_not_sent',reconciliation={
            'conclusion':'confirmed_not_sent','checked_at':'2026-11-16T03:00:00Z',
            'checked_by':'synthetic operator','execution_terminated':True,'evidence_ids':['receipt-evidence']})
        self.request['records'] = {'outbound_attempts':[resolved]}
        self.save()
        self.assertEqual(self.commit()['state'],'committed')
        self.assertEqual(self.read()['outbound_attempts'][0]['status'],'confirmed_not_sent')
        self.case['evidence'][0]['sha256'] = '0'*64
        self.assert_rejected_unchanged()

    def test_pause_resume_are_explicit_and_never_enable_follow_up(self):
        self.control('active','paused','pause')
        self.save()
        self.commit()
        self.case = self.read()
        self.assertEqual(self.case['follow_up']['host_adapter']['status'],'paused')
        self.assertEqual(m.health(self.case,NOW)['state'],'unverified')
        self.control('paused','active','resume')
        self.case['follow_up']['enabled'] = False
        self.save()
        self.commit()
        self.assertFalse(self.read()['follow_up']['enabled'])
        self.assertEqual(m.health(self.read(),NOW)['state'],'unverified')

    def test_paused_work_and_direct_control_field_patch_are_rejected(self):
        self.case['follow_up']['host_adapter']['status'] = 'paused'
        self.assert_rejected_unchanged()
        self.request = {'version':'1.0','expected_revision':0,'run':self.request['run'],
                        'records':{'follow_up':{'host_adapter':{'status':'active'}}}}
        self.assert_rejected_unchanged()

    def test_manual_future_missing_scheduler_and_overdue_are_not_healthy(self):
        self.request['run'].update(kind='manual_trial',scheduler_id=None)
        self.save()
        self.commit()
        self.assertEqual(m.health(self.read(),NOW)['state'],'unverified')
        self.case,self.request = fixture(self.root)
        self.request['run']['completed_at'] = '2099-01-01T00:00:00Z'
        self.assert_rejected_unchanged()
        self.case,self.request = fixture(self.root)
        self.save()
        self.commit()
        saved = self.read()
        self.assertEqual(m.health(saved,NOW + timedelta(hours=8))['state'],'overdue')
        saved['follow_up']['scheduler'] = {}
        self.assertEqual(m.health(saved,NOW)['state'],'unverified')

    def test_latest_failed_or_future_run_cannot_hide_behind_a_recent_success(self):
        self.commit()
        self.case = self.read()
        self.request['expected_revision'] = 1
        self.request['run'].update(id='failed-run',started_at='2026-11-16T05:00:00Z',
                                  completed_at='2026-11-16T05:03:00Z',errors=['synthetic failed read'])
        self.request.pop('feedback_batches')
        self.request['run']['source_read_results'][0]['status'] = 'failed'
        self.save()
        m.commit(self.root,self.case_path,self.request_path,NOW + timedelta(hours=2))
        report = m.health(self.read(),NOW + timedelta(hours=2))
        self.assertEqual(report['state'],'unverified')
        self.assertEqual(report['reason'],'latest_scheduled_run_was_not_successful')
        saved = self.read()
        saved['runs'][-1]['completed_at'] = '2099-01-01T00:00:00Z'
        self.assertEqual(m.health(saved,NOW + timedelta(hours=2))['state'],'unverified')

    def test_changed_authorized_thread_coverage_requires_a_new_complete_run(self):
        self.commit()
        saved = self.read()
        saved['follow_up']['feedback_sources'][0]['authorized_thread_or_ticket_ids'].append('new-thread')
        self.assertEqual(m.health(saved,NOW)['state'],'unverified')

    def age_feedback(self, days=10, include_run=True):
        batch = self.request['feedback_batches'][0]
        fields = [(batch,'read_at'),
                  (batch['pages'][0]['messages'][0]['derived_state'],'observed_at')]
        if include_run:
            fields.extend((self.request['run'],field) for field in ('started_at','completed_at'))
        for row,field in fields:
            row[field] = (m.timestamp(row[field],field) - timedelta(days=days)).isoformat()

    def test_backfilled_old_run_does_not_refresh_health(self):
        self.age_feedback()
        self.save()
        self.commit()
        result = m.health(self.read(),NOW)
        self.assertEqual(result['state'],'overdue')

    def test_recent_run_with_old_source_receipt_is_overdue(self):
        self.age_feedback(include_run=False)
        self.save()
        self.commit()
        self.assertEqual(m.health(self.read(),NOW)['state'],'overdue')

    def test_fresh_second_source_cannot_refresh_an_older_authorized_source(self):
        fresh = copy.deepcopy(self.request['feedback_batches'][0])
        self.age_feedback(include_run=False)
        source = copy.deepcopy(self.case['follow_up']['feedback_sources'][0])
        source.update(id='fresh-source',account_scope='fresh-account')
        self.case['follow_up']['feedback_sources'].append(source)
        fresh.update(source_id='fresh-source',account_scope='fresh-account')
        fresh['pages'][0]['messages'][0].update(source_id='fresh-source',account_scope='fresh-account')
        self.request['feedback_batches'].append(fresh)
        self.request['run']['source_read_results'].append({'source_id':'fresh-source',
            'account_scope':'fresh-account','status':'complete'})
        self.save()
        self.commit()
        self.assertEqual(m.health(self.read(),NOW)['state'],'overdue')

    def test_late_write_of_older_success_cannot_cover_a_more_recent_failure(self):
        original = copy.deepcopy(self.request)
        self.request['run']['errors'] = ['synthetic recent failure']
        self.request['run']['source_read_results'][0]['status'] = 'failed'
        self.request.pop('feedback_batches')
        self.save()
        self.commit()
        self.case = self.read()
        self.request = original
        self.request['expected_revision'] = 1
        self.request['run']['id'] = 'backfilled-old-success'
        self.age_feedback()
        self.save()
        m.commit(self.root,self.case_path,self.request_path,NOW + timedelta(hours=1))
        report = m.health(self.read(),NOW + timedelta(hours=1))
        self.assertEqual(report['state'],'unverified')
        self.assertEqual(report['reason'],'latest_scheduled_run_was_not_successful')

    def test_reconciled_sent_history_survives_next_commit_and_keeps_queue_dedupe(self):
        self.case['targets'] = [{'id':'target-1','status':'open'}]
        self.case['providers'] = [{'id':'provider-1'}]
        self.case['actions'] = [{'id':'a1','status':'acknowledged','target_ids':['target-1'],
                                 'provider_id':'provider-1','route':'email'}]
        task = {'id':'send-task','action_id':'a1','kind':'remind','status':'planned',
                'dedupe_key':'stable-sent-key','due_at':'2026-11-16T03:00:00Z'}
        self.case['followup_tasks'] = [task]
        attempt = {'id':'historical','action_id':'a1','task_id':'send-task',
                   'dedupe_key':'stable-sent-key','status':'outcome_unknown'}
        self.case['outbound_attempts'] = [attempt]
        resolved = dict(attempt,status='sent',reconciliation={
            'conclusion':'sent','checked_at':'2026-11-16T03:00:00Z','checked_by':'synthetic reviewer',
            'execution_terminated':True,'evidence_ids':['receipt-evidence']})
        self.request['records'] = {'outbound_attempts':[resolved]}
        self.save()
        self.commit()
        saved = self.read()
        first_queue = queue_base.module.build_queue(saved,NOW)
        self.assertEqual(first_queue['errors'],[])
        self.assertEqual(first_queue['due_tasks'],[])
        self.case = saved
        self.request['expected_revision'] = 1
        self.request['run']['id'] = 'next-run'
        batch = self.request['feedback_batches'][0]
        batch['from_cursor'] = 'cursor-1'
        batch['pages'][0].update(cursor='cursor-1',next_cursor='cursor-2')
        self.request['records'] = {'outbound_attempts':[]}
        self.save()
        before = self.case_path.read_bytes()
        rejected = False
        try:
            self.commit()
        except ValueError:
            rejected = True
        queue = queue_base.module.build_queue(self.read(),NOW)
        self.assertEqual(queue['errors'],[])
        self.assertNotIn('send-task',[row['id'] for row in queue['due_tasks']],
                         'Removing the reconciled sent attempt reopened the same dedupe key')
        self.assertTrue(rejected,'A later transaction must reject deletion of sent history')
        self.assertEqual(self.case_path.read_bytes(),before)

    def test_terminal_attempts_and_their_action_task_identity_remain_immutable(self):
        self.request.pop('feedback_batches')
        self.request['run'].update(kind='manual_control',scheduler_id=None,source_read_results=[])
        self.case['followup_tasks'] = [{'id':'history-task','action_id':'a1','kind':'remind',
                                       'dedupe_key':'history-key','status':'completed'}]
        for status in ('sent','confirmed_not_sent'):
            before = {'id':'history','action_id':'a1','task_id':'history-task',
                      'dedupe_key':'history-key','status':status}
            self.case['outbound_attempts'] = [before]
            for records in ({'outbound_attempts':[]},
                            {'outbound_attempts':[dict(before,dedupe_key='rekeyed')]},
                            {'outbound_attempts':[dict(before,status='prepared')]},
                            {'actions':[]}, {'followup_tasks':[]},
                            {'followup_tasks':[dict(self.case['followup_tasks'][0],dedupe_key='rekeyed')]}):
                with self.subTest(status=status,records=records):
                    self.request['records'] = records
                    self.assert_rejected_unchanged()

    def test_terminal_history_allows_status_detail_additions_without_rewriting_facts(self):
        before = {'id':'history','action_id':'a1','dedupe_key':'history-key','status':'sent'}
        self.case['outbound_attempts'] = [before]
        self.request['records'] = {'outbound_attempts':[dict(before,receipt_or_message_id='synthetic-receipt')],
                                  'actions':[dict(self.case['actions'][0],status='acknowledged')]}
        self.save()
        self.commit()
        self.assertEqual(self.read()['outbound_attempts'][0]['receipt_or_message_id'],'synthetic-receipt')

    def correction_history(self):
        self.case['targets'] = [{'id':'target-1','status':'open'}]
        self.case['providers'] = [{'id':'provider-1'}]
        base = {'status':'submitted','target_ids':['target-1'],'provider_id':'provider-1','route':'email'}
        self.case['actions'] = [dict(base,id='a1'),dict(base,id='mid',corrects_action_id='a1'),
                                dict(base,id='leaf',corrects_action_id='mid'),dict(base,id='other'),
                                dict(base,id='draft-root',status='draft'),
                                dict(base,id='draft-child',status='draft',corrects_action_id='draft-root')]
        self.case['followup_tasks'] = [
            {'id':'old-send','action_id':'a1','kind':'initial_notice','status':'completed',
             'dedupe_key':'old-key','due_at':'2026-11-15T03:00:00Z'},
            {'id':'leaf-send','action_id':'leaf','kind':'remind','status':'ready',
             'dedupe_key':'new-leaf','due_at':'2026-11-16T03:00:00Z'}]
        self.case['outbound_attempts'] = [{'id':'old-attempt','action_id':'a1','task_id':'old-send',
                                         'dedupe_key':'old-key','status':'outcome_unknown'}]

    def test_intermediate_correction_edge_cannot_disconnect_historical_send_family(self):
        self.correction_history()
        initial = queue_base.module.build_queue(self.case,NOW)
        self.assertEqual(initial['errors'],[])
        self.assertIn(('leaf-send','action_has_unresolved_send'),
                      [(row['id'],row['reason']) for row in initial['held_tasks']])
        changed = copy.deepcopy(self.case['actions'])
        changed[1]['corrects_action_id'] = 'other'
        self.request['records'] = {'actions':changed}
        self.save()
        before = self.case_path.read_bytes()
        rejected = False
        try:
            self.commit()
        except ValueError:
            rejected = True
        queue = queue_base.module.build_queue(self.read(),NOW)
        self.assertEqual(queue['errors'],[])
        self.assertNotIn('leaf-send',[row['id'] for row in queue['due_tasks']],
                         'Reparenting an intermediate correction bypassed the pending-send family')
        self.assertTrue(rejected)
        self.assertEqual(self.case_path.read_bytes(),before)

    def test_unrelated_draft_correction_edit_remains_allowed(self):
        self.correction_history()
        changed = copy.deepcopy(self.case['actions'])
        changed[-1]['corrects_action_id'] = 'other'
        self.request['records'] = {'actions':changed}
        self.save()
        self.assertEqual(self.commit()['state'],'committed')
        self.assertEqual(self.read()['actions'][-1]['corrects_action_id'],'other')
        queue = queue_base.module.build_queue(self.read(),NOW)
        self.assertEqual(queue['errors'],[])
        self.assertNotIn('leaf-send',[row['id'] for row in queue['due_tasks']])

    def test_bad_json_links_protected_paths_and_budget_are_rejected(self):
        self.request_path.write_text('{"version":"1.0","version":"1.0"}',encoding='utf-8')
        before = self.case_path.read_bytes()
        with self.assertRaises(ValueError):
            self.commit()
        self.assertEqual(self.case_path.read_bytes(),before)
        link = self.root/'linked.json'
        try:
            link.symlink_to(self.case_path)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                m.commit(self.root,link,self.request_path,NOW)
        with self.assertRaises(ValueError):
            m.commit(ROOT,ROOT/'README.md',self.request_path,NOW)
        with patch.object(m,'validate_case_budget',side_effect=ValueError('synthetic budget exceeded')):
            with self.assertRaises(ValueError):
                m.prepare(self.case,self.request,self.root,NOW)

    def test_cli_help_and_default_status_are_read_only(self):
        before = self.case_path.read_bytes()
        help_result = subprocess.run([sys.executable,'-B',str(SCRIPTS/'host_adapter.py'),'--help'],
                                     capture_output=True,text=True,timeout=10)
        self.assertEqual(help_result.returncode,0)
        result = subprocess.run([sys.executable,'-B',str(SCRIPTS/'host_adapter.py'),
            '--private-root',str(self.root),'--case',str(self.case_path)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0)
        report = json.loads(result.stdout)
        self.assertTrue(report['read_only'])
        self.assertFalse(report['authorizes_submission'])
        self.assertEqual(self.case_path.read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
