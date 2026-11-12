"""Regression contracts for audit findings R01-R08; synthetic records, no sends.

The original 16 assertions are retained. Run with unittest discovery, or set
FOLLOWUP_QUEUE_SCRIPT to test an immutable historical script.
"""
from __future__ import annotations

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

SCRIPT = Path(os.environ.get('FOLLOWUP_QUEUE_SCRIPT',
    Path(__file__).resolve().parents[1] / 'skills/brand-protection/scripts/followup_queue.py')).resolve()
if not SCRIPT.is_file():
    raise SystemExit('Set FOLLOWUP_QUEUE_SCRIPT to an existing followup_queue.py')
spec = importlib.util.spec_from_file_location('audited_queue', SCRIPT)
if spec is None or spec.loader is None:
    raise SystemExit('Cannot load the supplied Python module')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime(2026, 11, 12, 4, tzinfo=timezone.utc)
NOW_TEXT = '2026-11-12T04:00:00Z'


def case_record():
    return {
        'schema_version': '1.3', 'case_id': 'audit-synthetic',
        'follow_up': {'requested': True, 'enabled': True},
        'targets': [{'id': 'target-1', 'status': 'open', 'channel_plan': [
            {'provider_id': 'provider-1', 'route': 'email'}]}],
        'providers': [{'id': 'provider-1'}],
        'actions': [{'id': 'action-1', 'status': 'acknowledged',
                     'target_ids': ['target-1'], 'provider_id': 'provider-1', 'route': 'email'}],
        'followup_tasks': [], 'outbound_attempts': [], 'deadlines': [], 'runs': [],
    }


def task(task_id='task-1', **changes):
    result = {'id': task_id, 'action_id': 'action-1', 'kind': 'remind',
              'status': 'planned', 'due_at': NOW_TEXT, 'dedupe_key': task_id + '-round-1'}
    result.update(changes)
    return result


def deadline():
    return {'id': 'urgent', 'action_id': 'action-1', 'status': 'open',
            'due_at': '2026-11-12T05:00:00Z'}


def unresolved_claim(run_status):
    case = case_record()
    run = {'id': 'run-1', 'status': run_status, 'started_at': '2026-11-12T03:00:00Z'}
    if run_status == 'interrupted':
        run.update(interrupted_at='2026-11-12T03:30:00Z',
                   interruption_evidence='Synthetic supervisor record confirms termination')
    elif run_status == 'completed':
        run['completed_at'] = '2026-11-12T03:30:00Z'
    case['runs'] = [run]
    case['followup_tasks'] = [task('old', status='in_progress', run_id='run-1',
        claimed_at='2026-11-12T03:00:00Z', lease_expires_at='2026-11-12T05:00:00Z'), task('new')]
    case['deadlines'] = [deadline()]
    return case


class AuditRegressions(unittest.TestCase):
    def queue(self, case):
        original = copy.deepcopy(case)
        result = module.build_queue(case, NOW)
        self.assertEqual(case, original, 'Queue must not mutate the case')
        return result

    def assert_no_candidate(self, result, task_id):
        self.assertNotIn(task_id, [item['id'] for item in result['due_tasks']],
                         'An unresolved safety condition must not yield an outbound candidate')

    def check_claim_barrier(self, status):
        result = self.queue(unresolved_claim(status))
        self.assertIn('urgent', [item['id'] for item in result['deadline_alerts']])
        self.assert_no_candidate(result, 'new')

    def test_R01_running_claim_blocks_new_round(self):
        self.check_claim_barrier('running')

    def test_R01_interrupted_claim_blocks_new_round(self):
        self.check_claim_barrier('interrupted')

    def test_R01_completed_run_with_unresolved_task_blocks_new_round(self):
        self.check_claim_barrier('completed')

    def test_R01_original_unresolved_claim_blocks_correction(self):
        case = unresolved_claim('interrupted')
        case['actions'].append({**case['actions'][0], 'id': 'correction-1',
                                'status': 'authorized', 'corrects_action_id': 'action-1'})
        case['followup_tasks'][1] = task('correction', action_id='correction-1', kind='send_correction')
        result = self.queue(case)
        self.assert_no_candidate(result, 'correction')

    def test_R02_nonboolean_freeze_is_not_silently_ignored(self):
        case = case_record()
        case['actions'][0]['claim_frozen'] = 'true'
        case['followup_tasks'] = [task()]
        result = self.queue(case)
        self.assert_no_candidate(result, 'task-1')
        self.assertTrue(result['errors'], 'Malformed safety field needs a diagnostic')

    def test_R02_invalid_falsey_restriction_is_not_silently_ignored(self):
        case = case_record()
        case['actions'][0]['followup'] = {'all_outbound_not_before': 0}
        case['followup_tasks'] = [task()]
        result = self.queue(case)
        self.assert_no_candidate(result, 'task-1')
        self.assertTrue(result['errors'])

    def test_R03_same_batch_duplicate_dedupe_key_is_detected(self):
        case = case_record()
        case['followup_tasks'] = [task('a', dedupe_key='same'), task('b', dedupe_key='same')]
        result = self.queue(case)
        self.assertLessEqual(len([t for t in result['due_tasks'] if t['external_action_candidate']]), 1,
                             'Two same-key outbound candidates must not silently coexist')

    def test_R04_effectively_held_task_does_not_hide_next_step_gap(self):
        case = case_record()
        case['actions'][0]['claim_frozen'] = True
        case['followup_tasks'] = [task()]
        result = self.queue(case)
        self.assertEqual(result['due_tasks'], [])
        self.assertTrue(result['coverage_gaps'] or result['recovery_tasks'],
                        'A held task with no review plan must surface a next-step gap')

    def test_R05_explicit_empty_targets_does_not_inherit_all_targets(self):
        case = case_record()
        case['targets'].append({**copy.deepcopy(case['targets'][0]), 'id': 'target-2'})
        case['actions'][0]['target_ids'] = ['target-1', 'target-2']
        case['followup_tasks'] = [task(target_ids=[])]
        result = self.queue(case)
        self.assertTrue(result['errors'] or result['coverage_gaps'],
                        'Explicit empty scope must not silently cover both targets')

    def test_R05_unknown_action_target_blocks_outbound_candidate(self):
        case = case_record()
        case['actions'][0]['target_ids'] = ['does-not-exist']
        case['followup_tasks'] = [task()]
        result = self.queue(case)
        self.assert_no_candidate(result, 'task-1')

    def run_cli(self, raw_text, env_changes=None):
        with tempfile.TemporaryDirectory(prefix='brand-audit-synthetic-') as directory:
            path = Path(directory) / 'case.json'
            path.write_text(raw_text, encoding='utf-8')
            original = path.read_bytes()
            completed = subprocess.run(
                [sys.executable, '-B', str(SCRIPT), '--case', str(path), '--now', NOW_TEXT],
                capture_output=True, timeout=15,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', **(env_changes or {})})
            self.assertEqual(path.read_bytes(), original)
        return completed

    def test_R06_duplicate_json_member_is_not_silent_data_loss(self):
        case = case_record()
        case['deadlines'] = [deadline()]
        raw = json.dumps(case)[:-1] + ', "deadlines": []}'
        completed = self.run_cli(raw)
        self.assertNotEqual(completed.returncode, 0, 'Duplicate deadlines member needs an input error')
        report = json.loads(completed.stdout.decode('utf-8'))
        self.assertTrue(report['errors'])

    def ascii_environment(self):
        return {'LC_ALL': 'C', 'LANG': 'C', 'PYTHONUTF8': '0',
                'PYTHONCOERCECLOCALE': '0', 'PYTHONIOENCODING': 'ascii'}

    def test_R07_unicode_report_survives_ascii_stdout(self):
        case = case_record()
        case['case_id'] = '\u54c1\u724c\u6848\u4ef6'
        case['deadlines'] = [deadline()]
        completed = self.run_cli(json.dumps(case, ensure_ascii=True), self.ascii_environment())
        self.assertTrue(completed.stdout, 'Valid Unicode case must still produce a JSON report')
        self.assertNotIn(b'Traceback', completed.stderr)
        report = json.loads(completed.stdout.decode('utf-8'))
        self.assertEqual(report['cases'][0]['case_id'], case['case_id'])

    def test_R07_utf8_case_is_read_independently_of_locale(self):
        case = case_record()
        case['case_id'] = '\u54c1\u724c\u6848\u4ef6'
        case['deadlines'] = [deadline()]
        completed = self.run_cli(json.dumps(case, ensure_ascii=False), self.ascii_environment())
        self.assertTrue(completed.stdout, 'CLI must retain a report')
        report = json.loads(completed.stdout.decode('utf-8'))
        self.assertEqual([c['case_id'] for c in report['cases']], [case['case_id']],
                         'A UTF-8 case must not be discarded due to the process locale')

    def test_R08_unrelated_recurrence_is_not_a_legal_deadline_conflict(self):
        case = case_record()
        case['deadlines'] = [deadline()]
        case['followup_tasks'] = [task('reply', kind='send_supplement'),
            task('watch', kind='check_recurrence', due_at='2026-11-19T04:00:00Z')]
        result = self.queue(case)
        self.assertIn('urgent', [d['id'] for d in result['deadline_alerts']])
        self.assertFalse(any(c['task_id'] == 'watch' for c in result['deadline_conflicts']),
                         'Future recurrence is not a task to satisfy the current reply deadline')

    def test_control_readonly_and_unrelated_deadline_survive_bad_task(self):
        case = case_record()
        case['deadlines'] = [deadline()]
        case['followup_tasks'] = [task('bad', due_at=None), task('read', kind='check_feedback')]
        result = self.queue(case)
        self.assertIn('urgent', [d['id'] for d in result['deadline_alerts']])
        self.assertIn('read', [t['id'] for t in result['due_tasks']])
        self.assertTrue(result['errors'])

    def test_control_known_pending_attempt_blocks_new_round(self):
        case = case_record()
        case['followup_tasks'] = [task('old', status='completed'), task('new')]
        case['outbound_attempts'] = [{'id': 'attempt-1', 'task_id': 'old', 'action_id': 'action-1',
                                     'dedupe_key': 'old-round-1', 'status': 'outcome_unknown'}]
        result = self.queue(case)
        self.assert_no_candidate(result, 'new')


if __name__ == '__main__':
    unittest.main()
