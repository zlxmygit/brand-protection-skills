"""Queue lifecycle regressions use only synthetic records and never send."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/brand-protection/scripts'
sys.path.insert(0, str(SCRIPTS))
SCRIPT = Path(os.environ.get('FOLLOWUP_QUEUE_SCRIPT', SCRIPTS / 'followup_queue.py'))
spec = importlib.util.spec_from_file_location('queue_v184', SCRIPT)
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
NOW = datetime(2026, 11, 16, 4, tzinfo=timezone.utc)
NOW_TEXT = '2026-11-16T04:00:00Z'


def case_record():
    return {
        'schema_version': '1.3', 'case_id': 'synthetic-queue-lifecycle',
        'follow_up': {'requested': True, 'enabled': True},
        'targets': [{'id': 'target-1', 'status': 'closed'}],
        'actions': [{'id': 'notice-1', 'target_ids': ['target-1'],
                     'status': 'verified_effective'}],
        'followup_tasks': [], 'outbound_attempts': [], 'deadlines': [], 'runs': [],
    }


def observation(**changes):
    value = {'id': 'watch-1', 'target_ids': ['target-1'], 'status': 'enabled',
             'authorization_source': 'Synthetic explicit recurrence-watch authorization',
             'interval_hours': 168, 'next_review_at': '2026-11-17T04:00:00Z'}
    value.update(changes)
    return value


def task(task_id='watch-task', **changes):
    value = {'id': task_id, 'action_id': None, 'kind': 'check_recurrence',
             'recurrence_observation_id': 'watch-1', 'status': 'planned',
             'due_at': '2026-11-17T04:00:00Z'}
    value.update(changes)
    return value


def deadline():
    return {'id': 'urgent', 'action_id': 'notice-1', 'status': 'open',
            'due_at': '2026-11-16T05:00:00Z'}


def active_case():
    case = case_record()
    case['targets'][0].update(status='open', channel_plan=[{'route': 'email', 'provider_id': 'p-1'}])
    case['actions'][0].update(status='acknowledged', route='email', provider_id='p-1')
    case['followup_tasks'] = [{'id': 'send', 'action_id': 'notice-1', 'kind': 'remind',
                               'dedupe_key': 'synthetic-round-1', 'status': 'planned', 'due_at': NOW_TEXT}]
    case['deadlines'] = [deadline()]
    return case


class LifecycleQueue(unittest.TestCase):
    def build(self, case, **kwargs):
        before = copy.deepcopy(case)
        result = queue.build_queue(case, NOW, **kwargs)
        self.assertEqual(case, before)
        return result

    def test_terminal_action_and_target_do_not_hide_enabled_observation_gap(self):
        case = case_record()
        case['recurrence_observations'] = [observation()]
        result = self.build(case)
        self.assertTrue(any(item.get('observation_id') == 'watch-1'
                            and item.get('target_id') == 'target-1'
                            for item in result['coverage_gaps']), result)

    def test_registered_observation_has_independent_read_only_next_step(self):
        case = case_record()
        case['recurrence_observations'] = [observation()]
        case['followup_tasks'] = [task()]
        result = self.build(case)
        self.assertEqual(result['coverage_gaps'], [])
        self.assertEqual([row['id'] for row in result['deferred_tasks']], ['watch-task'])
        self.assertFalse(result['deferred_tasks'][0]['external_action_candidate'])

    def test_observation_partial_scope_and_late_task_preserve_gaps(self):
        case = case_record()
        case['targets'].append({'id': 'target-2', 'status': 'closed'})
        case['recurrence_observations'] = [observation(target_ids=['target-1', 'target-2'])]
        case['followup_tasks'] = [task(target_ids=['target-1'])]
        result = self.build(case)
        self.assertEqual([row['target_id'] for row in result['coverage_gaps']], ['target-2'])
        case['followup_tasks'][0]['due_at'] = '2026-11-18T04:00:00Z'
        result = self.build(case)
        self.assertEqual({row['target_id'] for row in result['coverage_gaps']}, {'target-1', 'target-2'})

    def test_inactive_observation_does_not_schedule_or_demand_a_task(self):
        for status in ('not_authorized', 'paused', 'ended'):
            with self.subTest(status=status):
                case = case_record()
                case['recurrence_observations'] = [observation(status=status)]
                self.assertEqual(self.build(case)['coverage_gaps'], [])
                case['followup_tasks'] = [task(due_at=NOW_TEXT)]
                result = self.build(case)
                self.assertEqual(result['due_tasks'] + result['deferred_tasks'], [])
                self.assertIn('watch-task', [row['id'] for row in result['held_tasks']])

    def test_unlinked_legacy_task_does_not_cover_an_explicit_registration(self):
        case = case_record()
        case['recurrence_observations'] = [observation()]
        legacy = task(action_id='notice-1')
        legacy.pop('recurrence_observation_id')
        case['followup_tasks'] = [legacy]
        result = self.build(case)
        self.assertEqual([row['observation_id'] for row in result['coverage_gaps']], ['watch-1'])
        self.assertEqual(result['due_tasks'] + result['deferred_tasks'], [])

    def test_unlinked_legacy_task_cannot_bypass_an_inactive_registration(self):
        for status in ('not_authorized', 'paused', 'ended'):
            with self.subTest(status=status):
                case = case_record()
                case['recurrence_observations'] = [observation(status=status)]
                legacy = task(action_id='notice-1', due_at=NOW_TEXT)
                legacy.pop('recurrence_observation_id')
                case['followup_tasks'] = [legacy]
                result = self.build(case)
                self.assertEqual(result['due_tasks'] + result['deferred_tasks'], [])
                self.assertIn('watch-task', [row['id'] for row in result['held_tasks']])

    def test_quarantined_or_malformed_registration_cannot_restore_legacy_candidates(self):
        paused = observation(status='paused')
        for rows in ([paused, copy.deepcopy(paused)], [None], None, {}, False):
            with self.subTest(rows=rows):
                case = case_record()
                case['recurrence_observations'] = copy.deepcopy(rows)
                legacy = task(due_at=NOW_TEXT, target_ids=['target-1'])
                legacy.pop('recurrence_observation_id')
                case['followup_tasks'] = [legacy]
                result = self.build(case)
                self.assertEqual(result['due_tasks'] + result['deferred_tasks'], [])
                self.assertTrue(any(row['section'] == 'recurrence_observations'
                                    for row in result['errors']), result)

    def test_unconfigured_inactive_observation_is_not_silently_enabled(self):
        case = case_record()
        case['recurrence_observations'] = [observation(status='not_authorized',
            authorization_source=None, interval_hours=None, next_review_at=None)]
        result = self.build(case)
        self.assertEqual(result['errors'] + result['coverage_gaps'] + result['due_tasks'], [])

    def test_enabled_observation_requires_its_own_authority_and_schedule(self):
        for changes in ({'authorization_source': None}, {'interval_hours': 0},
                        {'interval_hours': True}, {'next_review_at': None},
                        {'target_ids': ['unrecorded-target']}, {'status': True}):
            with self.subTest(changes=changes):
                case = case_record()
                case['recurrence_observations'] = [observation(**changes)]
                case['followup_tasks'] = [task(due_at=NOW_TEXT)]
                result = self.build(case)
                self.assertEqual(result['due_tasks'] + result['deferred_tasks'], [])
                self.assertTrue(any(row['section'] == 'recurrence_observations'
                                    for row in result['errors']), result)

    def test_observation_link_cannot_widen_legacy_action_scope(self):
        case = case_record()
        case['targets'].append({'id': 'target-2', 'status': 'closed'})
        case['recurrence_observations'] = [observation(target_ids=['target-1', 'target-2'])]
        case['followup_tasks'] = [task(action_id='notice-1')]
        result = self.build(case)
        self.assertEqual([row['target_id'] for row in result['coverage_gaps']], ['target-2'])

    def test_legacy_13_recurrence_task_remains_compatible_without_registration(self):
        case = case_record()
        legacy = task(action_id='notice-1', due_at=NOW_TEXT)
        legacy.pop('recurrence_observation_id')
        case['followup_tasks'] = [legacy]
        result = self.build(case)
        self.assertEqual([row['id'] for row in result['due_tasks']], ['watch-task'])
        self.assertEqual(result['errors'], [])
        case['recurrence_observations'] = []
        result = self.build(case)
        self.assertEqual([row['id'] for row in result['due_tasks']], ['watch-task'])
        self.assertEqual(result['errors'], [])

    def test_legacy_authority_without_plan_warns_after_terminal_case_without_enabling(self):
        for explicit_empty in (False, True):
            for state in ('preview', 'not_enabled', 'paused'):
                with self.subTest(explicit_empty=explicit_empty, state=state):
                    case = case_record()
                    case['follow_up']['authorizations'] = {
                        'recurrence_watch': 'Synthetic recorded legacy watch authorization'}
                    if explicit_empty:
                        case['recurrence_observations'] = []
                    if state == 'not_enabled':
                        case['follow_up']['enabled'] = False
                    if state == 'paused':
                        case['follow_up']['scheduler'] = {'status': 'paused'}
                    result = self.build(case)
                    self.assertEqual(result['state'], state)
                    self.assertEqual(result['due_tasks'] + result['deferred_tasks'] + result['recovery_tasks'], [])
                    self.assertTrue(any(warning.startswith('recurrence_authorization_requires_plan_review:')
                                        for warning in result['warnings']), result)

    def test_legacy_authority_warning_preserves_existing_read_only_work(self):
        case = case_record()
        case['follow_up']['authorizations'] = {
            'recurrence_watch': 'Synthetic recorded legacy watch authorization'}
        legacy = task(action_id='notice-1', due_at=NOW_TEXT)
        legacy.pop('recurrence_observation_id')
        case['followup_tasks'] = [legacy, {'id': 'other-read', 'kind': 'check_target',
            'action_id': None, 'target_ids': ['target-1'], 'status': 'planned', 'due_at': NOW_TEXT}]
        result = self.build(case)
        self.assertEqual({row['id'] for row in result['due_tasks']}, {'watch-task', 'other-read'})
        self.assertEqual(result['errors'], [])
        self.assertTrue(any(warning.startswith('recurrence_authorization_requires_plan_review:')
                            for warning in result['warnings']), result)

        active = active_case()
        active['follow_up']['authorizations'] = {'recurrence_watch': 'Synthetic recorded authorization'}
        result = self.build(active)
        self.assertEqual([row['id'] for row in result['due_tasks']], ['send'])
        self.assertEqual(result['errors'], [])

    def test_plan_review_warning_requires_recorded_authority_and_no_valid_explicit_plan(self):
        for authority in (None, '', False):
            with self.subTest(authority=authority):
                case = case_record()
                case['follow_up']['authorizations'] = {'recurrence_watch': authority}
                self.assertFalse(any(warning.startswith('recurrence_authorization_requires_plan_review:')
                                     for warning in self.build(case)['warnings']))
        for state in ('enabled', 'paused', 'ended', 'not_authorized'):
            with self.subTest(state=state):
                case = case_record()
                case['follow_up']['authorizations'] = {'recurrence_watch': 'Synthetic recorded authorization'}
                case['recurrence_observations'] = [observation(status=state)]
                self.assertFalse(any(warning.startswith('recurrence_authorization_requires_plan_review:')
                                     for warning in self.build(case)['warnings']))

    def test_investigation_uses_valid_target_task_without_complaint_action(self):
        case = case_record()
        case['targets'][0].update(status='open', phase='investigation')
        case['actions'] = []
        for state in ('planned', 'ready', 'blocked', 'in_progress'):
            with self.subTest(state=state):
                work = task('investigate', kind='check_target', due_at=NOW_TEXT,
                            target_ids=['target-1'], status=state)
                work.pop('recurrence_observation_id')
                if state == 'blocked':
                    work.update(reason='Synthetic evidence source unavailable', review_at=NOW_TEXT)
                if state == 'in_progress':
                    case['runs'] = [{'id': 'run-1', 'status': 'running'}]
                    work.update(run_id='run-1', claimed_at='2026-11-16T03:00:00Z',
                                lease_expires_at='2026-11-16T05:00:00Z')
                case['followup_tasks'] = [work]
                result = self.build(case)
                self.assertEqual(result['coverage_gaps'], [])
                if state in ('planned', 'ready'):
                    self.assertEqual([row['id'] for row in result['due_tasks']], ['investigate'])

    def test_investigation_requires_an_explicit_usable_target_scope(self):
        case = case_record()
        case['targets'][0].update(status='open', phase='investigation')
        case['actions'] = []
        for changes in ({}, {'target_ids': ['target-1'], 'status': 'completed'},
                        {'target_ids': ['target-1'], 'status': 'blocked', 'reason': 'Access unavailable'}):
            with self.subTest(changes=changes):
                work = {'id': 'investigate', 'action_id': None, 'kind': 'check_target',
                        'status': 'planned', 'due_at': NOW_TEXT, **changes}
                case['followup_tasks'] = [work]
                result = self.build(case)
                self.assertTrue(any(row.get('target_id') == 'target-1'
                                    for row in result['coverage_gaps']), result)

    def test_healthy_running_task_is_not_a_deadline_conflict(self):
        case = active_case()
        case['runs'] = [{'id': 'run-1', 'status': 'running', 'started_at': '2026-11-16T03:00:00Z'}]
        case['followup_tasks'][0].update(status='in_progress', run_id='run-1',
            claimed_at='2026-11-16T03:00:00Z', lease_expires_at='2026-11-16T06:00:00Z')
        result = self.build(case)
        self.assertEqual(result['deadline_conflicts'], [])
        self.assertEqual(result['due_tasks'], [])
        self.assertEqual(result['recovery_tasks'], [])

    def test_real_restrictions_and_elapsed_deadlines_still_conflict_with_running_task(self):
        for changes in ({'not_before': '2026-11-16T06:00:00Z'}, {}):
            with self.subTest(changes=changes):
                case = active_case()
                case['runs'] = [{'id': 'run-1', 'status': 'running', 'started_at': '2026-11-16T03:00:00Z'}]
                case['followup_tasks'][0].update(status='in_progress', run_id='run-1',
                    claimed_at='2026-11-16T03:00:00Z', lease_expires_at='2026-11-16T06:00:00Z', **changes)
                if not changes:
                    case['deadlines'][0]['due_at'] = '2026-11-16T03:30:00Z'
                result = self.build(case)
                self.assertEqual([row['deadline_id'] for row in result['deadline_conflicts']], ['urgent'])
                self.assertEqual([row['id'] for row in result['deadline_alerts']], ['urgent'])

    def test_record_and_dependency_budget_preserve_independent_deadlines(self):
        for section, value in (
                ('evidence', [{'id': 'item-' + str(i)} for i in range(10001)]),
                ('actions', [{**active_case()['actions'][0],
                              'claim_evidence_ids': ['item-' + str(i) for i in range(50001)]}])):
            with self.subTest(section=section):
                case = active_case()
                case[section] = value
                result = self.build(case)
                self.assertEqual(result['due_tasks'] + result['deferred_tasks'], [])
                self.assertEqual([row['id'] for row in result['deadline_alerts']], ['urgent'])
                self.assertTrue(any(row['section'] == 'record_budget' for row in result['errors']), result)


class ValidationOnly(unittest.TestCase):
    def run_cli(self, case, *extra):
        with tempfile.TemporaryDirectory(prefix='brand-queue-v184-') as directory:
            path = Path(directory) / 'synthetic.json'
            path.write_text(json.dumps(case), encoding='utf-8')
            before = path.read_bytes()
            proc = subprocess.run([sys.executable, '-B', str(SCRIPT), '--case', str(path),
                                   '--now', NOW_TEXT, *extra], capture_output=True, timeout=15)
            self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(b'Traceback', proc.stderr)
        return proc, json.loads(proc.stdout)

    def test_disabled_case_is_validated_without_executable_output(self):
        case = active_case()
        case['follow_up']['enabled'] = False
        case['followup_tasks'].append({'id': 'broken-history', 'kind': 'remind',
            'status': 'completed', 'action_id': 'unknown-action', 'dedupe_key': 'history'})
        proc, report = self.run_cli(case, '--validate-only')
        self.assertEqual(proc.returncode, 1)
        result = report['cases'][0]
        self.assertTrue(result['validation_only'])
        self.assertTrue(any(row['section'] == 'followup_tasks' for row in result['errors']))
        self.assertEqual(result['due_tasks'] + result['deferred_tasks'] + result['recovery_tasks'], [])
        self.assertEqual([row['id'] for row in result['deadline_alerts']], ['urgent'])

    def test_paused_scheduler_or_adapter_stays_paused_and_validates_on_request(self):
        for source in ('scheduler', 'host_adapter'):
            with self.subTest(source=source):
                case = active_case()
                case['follow_up'][source] = {'status': 'paused'}
                proc, report = self.run_cli(case)
                self.assertEqual(proc.returncode, 0, report)
                self.assertEqual(report['cases'][0]['state'], 'paused')
                self.assertEqual(report['cases'][0]['due_tasks'], [])
                case['outbound_attempts'] = [{'id': 'orphan', 'task_id': 'missing-task',
                    'action_id': 'notice-1', 'dedupe_key': 'unknown', 'status': 'sent'}]
                proc, report = self.run_cli(case, '--validate-only')
                self.assertEqual(proc.returncode, 1)
                result = report['cases'][0]
                self.assertTrue(any(row['section'] == 'outbound_attempts' for row in result['errors']))
                self.assertEqual(result['due_tasks'] + result['deferred_tasks'] + result['recovery_tasks'], [])

    def test_enabled_validation_only_never_returns_candidates_or_recovery_work(self):
        case = active_case()
        case['followup_tasks'].append({'id': 'blocked-read', 'action_id': 'notice-1',
            'kind': 'check_feedback', 'status': 'blocked', 'due_at': NOW_TEXT,
            'reason': 'Synthetic source unavailable', 'review_at': NOW_TEXT})
        proc, report = self.run_cli(case, '--validate-only')
        self.assertEqual(proc.returncode, 0, report)
        result = report['cases'][0]
        self.assertEqual(result['due_tasks'] + result['deferred_tasks'] + result['recovery_tasks'], [])
        self.assertEqual(result['coverage_gaps'], [])

    def test_closed_task_run_reference_is_checked_in_validation_mode(self):
        case = active_case()
        case['follow_up']['enabled'] = False
        case['followup_tasks'][0].update(status='completed', run_id='missing-run')
        proc, report = self.run_cli(case, '--validate-only')
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(any(row['section'] == 'followup_tasks' for row in report['cases'][0]['errors']))

    def test_cli_budget_failure_retains_deadline_diagnostics(self):
        case = active_case()
        case['evidence'] = [{'id': 'item-' + str(i)} for i in range(10001)]
        proc, report = self.run_cli(case)
        self.assertEqual(proc.returncode, 1)
        result = report['cases'][0]
        self.assertEqual([row['id'] for row in result['deadline_alerts']], ['urgent'])
        self.assertEqual(result['due_tasks'] + result['deferred_tasks'], [])

    def test_cli_rejects_oversized_file_without_claiming_deadline_extraction(self):
        case = active_case()
        case['synthetic_padding'] = 'x' * (8 * 1024 * 1024)
        proc, report = self.run_cli(case)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(report['cases'], [])
        self.assertTrue(report['errors'])


if __name__ == '__main__':
    unittest.main()
