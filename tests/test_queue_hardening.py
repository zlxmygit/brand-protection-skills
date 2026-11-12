"""Order, scope, recovery and strict-I/O contracts; no external services."""
import copy
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_audit_regressions as a


class QueueHardening(unittest.TestCase):
    def queue(self, case):
        before = copy.deepcopy(case)
        result = a.module.build_queue(case, a.NOW)
        self.assertEqual(case, before)
        return result

    def ids(self, result, name='due_tasks'):
        return {item['id'] for item in result[name]}

    def test_claim_barrier_is_order_independent_and_preserves_unrelated_work(self):
        for status in ('running', 'completed', 'interrupted'):
            case = a.unresolved_claim(status)
            case['actions'].append({**case['actions'][0], 'id': 'independent'})
            case['followup_tasks'] += [a.task('read', kind='check_feedback'),
                                      a.task('other', action_id='independent')]
            for tasks in itertools.permutations(case['followup_tasks']):
                with self.subTest(status=status, order=[t['id'] for t in tasks]):
                    result = self.queue({**case, 'followup_tasks': list(tasks)})
                    self.assertEqual(self.ids(result), {'read', 'other'})
                    self.assertIn('urgent', self.ids(result, 'deadline_alerts'))

    def test_invalid_due_or_missing_run_does_not_remove_claim_barrier(self):
        for change in ({'due_at': None}, {'run_id': 'missing'}, {'lease_expires_at': None}):
            case = a.unresolved_claim('interrupted')
            case['followup_tasks'][0].update(change)
            result = self.queue(case)
            self.assertNotIn('new', self.ids(result))
            self.assertIn('urgent', self.ids(result, 'deadline_alerts'))

    def test_unclassifiable_work_cannot_be_hidden_after_candidate(self):
        for change in ({'kind': []}, {'status': {}}, {'action_id': None}, {'dedupe_key': None}):
            case = a.unresolved_claim('running')
            case['followup_tasks'][0].update(change)
            for reverse in (False, True):
                with self.subTest(change=change, reverse=reverse):
                    current = copy.deepcopy(case)
                    if reverse:
                        current['followup_tasks'].reverse()
                    result = self.queue(current)
                    self.assertNotIn('new', self.ids(result))
                    self.assertTrue(result['errors'])

    def test_barrier_propagates_across_correction_family_only(self):
        case = a.case_record()
        for key, parent in [('fix-1', 'action-1'), ('fix-2', 'fix-1'), ('sibling', 'action-1')]:
            case['actions'].append({**case['actions'][0], 'id': key, 'corrects_action_id': parent})
        old = a.task('old', action_id='fix-2', status='completed', kind='send_correction')
        case['followup_tasks'] = [old, a.task('original'),
            a.task('sibling', action_id='sibling', kind='send_correction')]
        case['outbound_attempts'] = [{'id': 'attempt-1', 'task_id': 'old', 'action_id': 'fix-2',
            'dedupe_key': old['dedupe_key'], 'status': 'prepared'}]
        result = self.queue(case)
        self.assertEqual(self.ids(result), set())
        self.assertEqual(self.ids(result, 'held_tasks'), {'original', 'sibling'})

    def test_correction_cycle_is_reported_without_hiding_read_or_deadline(self):
        case = a.case_record()
        case['actions'][0]['corrects_action_id'] = 'fix-1'
        case['actions'].append({**case['actions'][0], 'id': 'fix-1', 'corrects_action_id': 'action-1'})
        case['followup_tasks'] = [a.task(), a.task('read', kind='check_feedback')]
        case['deadlines'] = [a.deadline()]
        result = self.queue(case)
        self.assertEqual(self.ids(result), {'read'})
        self.assertTrue(any('cyclic' in e['error'] for e in result['errors']))
        self.assertIn('urgent', self.ids(result, 'deadline_alerts'))

    def test_settled_history_allows_distinct_new_round(self):
        case = a.unresolved_claim('completed')
        old = case['followup_tasks'][0]
        old['status'] = 'completed'
        case['outbound_attempts'] = [{'id': 'attempt-1', 'task_id': 'old', 'action_id': 'action-1',
            'dedupe_key': old['dedupe_key'], 'status': 'sent', 'run_id': 'run-1'}]
        result = self.queue(case)
        self.assertEqual(self.ids(result), {'new'})

    def test_only_boolean_freeze_values_are_accepted(self):
        for value in (None, 0, 1, '', 'false', 'true', [], {}, ['x']):
            with self.subTest(value=value):
                case = a.case_record()
                case['actions'][0]['claim_frozen'] = value
                case['followup_tasks'] = [a.task(), a.task('read', kind='check_feedback')]
                result = self.queue(case)
                self.assertEqual(self.ids(result), {'read'})
                self.assertTrue(result['errors'])
        for value, expected in ((False, {'task-1'}), (True, set())):
            case = a.case_record()
            case['actions'][0]['claim_frozen'] = value
            case['followup_tasks'] = [a.task()]
            self.assertEqual(self.ids(self.queue(case)), expected)

    def test_constraint_types_fail_closed_but_null_means_unconfigured(self):
        for field in ('all_outbound_not_before', 'cooldown_until', 'expected_response_due_at'):
            for value in (0, False, '', [], {}, 'not-a-date'):
                with self.subTest(field=field, value=value):
                    case = a.case_record()
                    case['actions'][0]['followup'] = {field: value}
                    case['followup_tasks'] = [a.task(), a.task('read', kind='check_feedback')]
                    result = self.queue(case)
                    self.assertEqual(self.ids(result), {'read'})
                    self.assertTrue(result['errors'])
            case = a.case_record()
            case['actions'][0]['followup'] = {field: None}
            case['followup_tasks'] = [a.task()]
            self.assertEqual(self.ids(self.queue(case)), {'task-1'})

    def test_invalid_not_before_never_bypasses_delay(self):
        for value in (False, 0, '', [], {}):
            case = a.case_record()
            case['followup_tasks'] = [a.task(not_before=value)]
            result = self.queue(case)
            self.assertEqual(self.ids(result), set())
            self.assertTrue(result['errors'])

    def test_all_duplicate_active_keys_are_quarantined_in_both_orders(self):
        for second_state in ('planned', 'ready', 'blocked'):
            case = a.case_record()
            case['followup_tasks'] = [a.task('a', dedupe_key='same'),
                a.task('b', dedupe_key='same', status=second_state,
                       reason='synthetic blocker', review_at='2026-11-13T04:00:00Z'),
                a.task('read', kind='check_feedback')]
            for reverse in (False, True):
                if reverse:
                    case['followup_tasks'].reverse()
                result = self.queue(case)
                self.assertEqual(self.ids(result), {'read'})
                self.assertEqual(self.ids(result, 'held_tasks'), {'a', 'b'})
                self.assertTrue(result['errors'])

    def test_held_task_needs_real_next_step_not_just_planned_status(self):
        case = a.case_record()
        case['actions'][0]['claim_frozen'] = True
        case['followup_tasks'] = [a.task()]
        self.assertTrue(self.queue(case)['coverage_gaps'])
        case['followup_tasks'].append(a.task('review', kind='check_feedback', status='blocked',
            reason='awaiting corrected rights evidence', review_at='2026-11-13T04:00:00Z'))
        self.assertEqual(self.queue(case)['coverage_gaps'], [])

    def test_unknown_duplicate_and_expanded_scopes_are_rejected(self):
        for scope in ([], None, ['missing'], ['target-1', 'target-1'], ['target-2']):
            with self.subTest(scope=scope):
                case = a.case_record()
                case['targets'].append({**copy.deepcopy(case['targets'][0]), 'id': 'target-2'})
                case['followup_tasks'] = [a.task(target_ids=scope)]
                result = self.queue(case)
                self.assertEqual(self.ids(result), set())
                self.assertTrue(result['errors'])

    def test_duplicate_target_records_block_outbound_not_reads(self):
        case = a.case_record()
        case['targets'].append(copy.deepcopy(case['targets'][0]))
        case['followup_tasks'] = [a.task(), a.task('read', kind='check_feedback')]
        result = self.queue(case)
        self.assertEqual(self.ids(result), {'read'})
        self.assertTrue(result['errors'])

    def test_missing_task_scope_inherits_but_subset_covers_only_subset(self):
        case = a.case_record()
        case['targets'].append({**copy.deepcopy(case['targets'][0]), 'id': 'target-2'})
        case['actions'][0]['target_ids'].append('target-2')
        case['followup_tasks'] = [a.task()]
        self.assertEqual(self.queue(case)['coverage_gaps'], [])
        case['followup_tasks'][0]['target_ids'] = ['target-1']
        gaps = self.queue(case)['coverage_gaps']
        self.assertTrue(any(g.get('target_id') == 'target-2' for g in gaps))
        self.assertFalse(any(g.get('target_id') == 'target-1' for g in gaps))

    def test_explicit_deadline_links_override_kind_fallback(self):
        case = a.case_record()
        case['deadlines'] = [a.deadline()]
        case['followup_tasks'] = [a.task('watch', kind='check_recurrence',
            due_at='2026-11-19T04:00:00Z', deadline_ids=['urgent'])]
        result = self.queue(case)
        self.assertEqual([c['task_id'] for c in result['deadline_conflicts']], ['watch'])
        case['followup_tasks'][0]['deadline_ids'] = []
        self.assertEqual(self.queue(case)['deadline_conflicts'], [])
        self.assertIn('urgent', self.ids(self.queue(case), 'deadline_alerts'))

    def test_invalid_deadline_links_are_diagnosed_not_silently_dropped(self):
        for value in (None, 'urgent', ['unknown'], ['urgent', 'urgent'], [None]):
            with self.subTest(value=value):
                case = a.case_record()
                case['deadlines'] = [a.deadline()]
                case['followup_tasks'] = [a.task(deadline_ids=value)]
                result = self.queue(case)
                self.assertTrue(result['errors'])
                self.assertEqual(self.ids(result), set())
                self.assertIn('urgent', self.ids(result, 'deadline_alerts'))

    def test_cross_action_deadline_link_is_rejected(self):
        case = a.case_record()
        case['deadlines'] = [{**a.deadline(), 'action_id': 'other'}]
        case['followup_tasks'] = [a.task(deadline_ids=['urgent'])]
        result = self.queue(case)
        self.assertTrue(result['errors'])
        self.assertEqual(self.ids(result), set())
        self.assertIn('urgent', self.ids(result, 'deadline_alerts'))


class StrictInputOutput(unittest.TestCase):
    def cli(self, raw, env=None):
        with tempfile.TemporaryDirectory(prefix='brand-queue-test-') as directory:
            paths = [Path(directory) / 'bad.json', Path(directory) / 'good.json']
            good = a.case_record()
            good['case_id'] = '\u54c1\u724c\u6848\u4ef6'
            good['deadlines'] = [a.deadline()]
            paths[0].write_bytes(raw if isinstance(raw, bytes) else raw.encode('utf-8'))
            paths[1].write_text(json.dumps(good, ensure_ascii=False), encoding='utf-8')
            before = [p.read_bytes() for p in paths]
            proc = subprocess.run([sys.executable, '-B', str(a.SCRIPT), '--now', a.NOW_TEXT,
                '--case', str(paths[0]), '--case', str(paths[1])], capture_output=True,
                timeout=15, env={**os.environ, **(env or {})})
            self.assertEqual(before, [p.read_bytes() for p in paths])
        self.assertNotIn(b'Traceback', proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report['cases'][-1]['case_id'], good['case_id'])
        self.assertEqual(report['cases'][-1]['deadline_alerts'][0]['id'], 'urgent')
        return proc, report

    def test_duplicate_members_at_any_level_do_not_hide_good_case(self):
        for raw in ('{"deadlines": [], "deadlines": []}',
                    '{"follow_up":{"enabled":false,"enabled":true}}',
                    '{"actions":[{"claim_frozen":true,"claim_frozen":false}]}'):
            proc, report = self.cli(raw)
            self.assertEqual(proc.returncode, 1)
            self.assertIn('duplicate JSON member', report['errors'][0]['error'])
            self.assertEqual(len(report['cases']), 1)

    def test_nonstandard_and_overflowing_numbers_are_rejected(self):
        for literal in ('NaN', 'Infinity', '-Infinity', '1e400'):
            proc, report = self.cli('{"value":' + literal + '}')
            self.assertEqual(proc.returncode, 1)
            self.assertTrue(report['errors'])

    def test_invalid_utf8_and_deep_json_are_isolated(self):
        for raw in (b'\xff', '[' * 1500 + '0' + ']' * 1500):
            proc, report = self.cli(raw)
            self.assertEqual(proc.returncode, 1)
            self.assertTrue(report['errors'])

    def test_utf8_bom_and_ascii_environment_are_supported(self):
        raw = b'\xef\xbb\xbf' + json.dumps(a.case_record()).encode('utf-8')
        proc, report = self.cli(raw, {'LC_ALL': 'C', 'LANG': 'C', 'PYTHONUTF8': '0',
            'PYTHONCOERCECLOCALE': '0', 'PYTHONIOENCODING': 'ascii'})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(report['cases']), 2)
        proc.stdout.decode('ascii')


if __name__ == '__main__':
    unittest.main()
