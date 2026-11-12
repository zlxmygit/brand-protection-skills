"""1,120 deterministic field mutations; synthetic inputs, no external actions."""
import copy
import unittest
import test_audit_regressions as a

def seed():
    case = a.case_record()
    case['actions'][0].update(corrects_action_id=None, claim_frozen=False, followup={
        'all_outbound_not_before': None, 'cooldown_until': None, 'expected_response_due_at': None})
    case['runs'] = [{'id': 'run-1', 'status': 'running', 'started_at': '2026-11-12T03:00:00Z',
                    'interrupted_at': None, 'interruption_evidence': None}]
    case['followup_tasks'] = [a.task('old', status='completed', target_ids=['target-1'],
        not_before=None, reason=None, review_at=None, run_id='run-1',
        claimed_at='2026-11-12T03:00:00Z', lease_expires_at='2026-11-12T05:00:00Z'),
        a.task('read', kind='check_feedback', run_id=None, claimed_at=None)]
    case['outbound_attempts'] = [{'id': 'attempt-1', 'task_id': 'old', 'action_id': 'action-1',
        'dedupe_key': 'old-round-1', 'status': 'sent', 'run_id': 'run-1'}]
    case['deadlines'] = [a.deadline()]
    return case


def paths():
    result = [(k,) for k in ('case_id','schema_version','follow_up','targets','actions',
                              'followup_tasks','runs','outbound_attempts','deadlines')]
    def add(prefix, keys):
        result.extend(prefix + (key,) for key in keys)
    add(('follow_up',), ('requested','enabled'))
    add(('targets',0), ('id','status','channel_plan'))
    add(('targets',0,'channel_plan',0), ('provider_id','route'))
    add(('actions',0), ('id','status','target_ids','provider_id','route','corrects_action_id','claim_frozen','followup'))
    add(('actions',0,'followup'), ('all_outbound_not_before','cooldown_until','expected_response_due_at'))
    add(('followup_tasks',0), ('id','action_id','kind','status','due_at','dedupe_key','target_ids',
                              'not_before','reason','review_at','run_id','claimed_at','lease_expires_at'))
    add(('runs',0), ('id','status','started_at','interrupted_at','interruption_evidence'))
    add(('outbound_attempts',0), ('id','task_id','action_id','dedupe_key','status','run_id'))
    add(('deadlines',0), ('id','action_id','status','due_at'))
    result.extend((section,0) for section in ('targets','actions','followup_tasks','runs','outbound_attempts','deadlines'))
    result.append(('targets',0,'channel_plan',0))
    add(('followup_tasks',1), ('id','action_id','kind','status','due_at','dedupe_key','run_id','claimed_at'))
    assert len(result) == 70
    return result


class MutationContracts(unittest.TestCase):
    def test_1120_type_mutations_preserve_independent_deadlines_and_input(self):
        values = [None, False, True, 0, 1, -1, 1.5, '', ' ', 'bad', [], {},
                  [{}], [None], ['x'], {'id': 'x'}]
        count = 0
        for path in paths():
            for value in values:
                with self.subTest(path=path, value=value):
                    case = seed()
                    cursor = case
                    for part in path[:-1]:
                        cursor = cursor[part]
                    cursor[path[-1]] = copy.deepcopy(value)
                    before = copy.deepcopy(case)
                    output = a.module.build_queue(case, a.NOW)
                    self.assertEqual(case, before)
                    if path[0] not in ('deadlines', 'follow_up'):
                        self.assertIn('urgent', [d['id'] for d in output['deadline_alerts']])
                    count += 1
        self.assertEqual(count, 1120)


if __name__ == '__main__':
    unittest.main()
