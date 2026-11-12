#!/usr/bin/env python3
"""Read-only follow-up queue and record checks; never sends, schedules or writes cases."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys


KINDS = {
    'check_feedback', 'check_target', 'verify_receipt', 'remind', 'resubmit',
    'send_supplement', 'send_escalation', 'send_correction', 'prepare_escalation',
    'verify_effect', 'check_recurrence', 'reconcile_send',
}
OUTBOUND = {'remind', 'resubmit', 'send_supplement', 'send_escalation', 'send_correction'}
TASK_STATES = {'planned', 'ready', 'in_progress', 'blocked', 'completed', 'cancelled'}
TERMINAL_ACTIONS = {'withdrawn', 'closed_no_action', 'verified_effective'}
ACTION_STATES = TERMINAL_ACTIONS | {
    'researching', 'draft', 'awaiting_evidence', 'ready_for_review', 'authorized',
    'submitted', 'acknowledged', 'needs_information', 'rejected', 'appealed',
    'counter_notified', 'action_reported', 'verified_partial', 'not_verified',
    'correction_pending',
}
PENDING_SEND = {'prepared', 'in_flight', 'outcome_unknown'}
ATTEMPT_STATES = PENDING_SEND | {'sent', 'confirmed_not_sent'}
CLOSED_TASKS = {'completed', 'cancelled'}
CLOSED_DEADLINES = {'completed', 'resolved', 'cancelled', 'superseded'}


def identifier(value):
    return isinstance(value, str) and bool(value.strip())


def member(value, choices):
    return isinstance(value, str) and value in choices


def timestamp(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label}: expected an ISO-8601 timestamp with timezone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('timezone is required')
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{label}: invalid or out-of-range timestamp with timezone ({exc})') from exc


def window(now, hours):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now: timezone is required')
    try:
        hours = float(hours)
        if not math.isfinite(hours) or hours < 0:
            raise ValueError('horizon_hours must be finite and non-negative')
        now = now.astimezone(timezone.utc)
        return now, now + timedelta(hours=hours)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f'invalid time window: {exc}') from exc


def error(result, section, message, item_id=None):
    result['errors'].append({'section': section, 'id': item_id, 'error': message})


def records(case, section, result):
    """Quarantine malformed/duplicate rows instead of losing unrelated results."""
    rows = case.get(section, [])
    if not isinstance(rows, list):
        error(result, section, 'expected a list')
        return {}
    counts = Counter(r['id'] for r in rows if isinstance(r, dict) and identifier(r.get('id')))
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not identifier(row.get('id')):
            error(result, section, 'record requires a non-empty string id')
        elif counts[row['id']] > 1:
            error(result, section, 'duplicate id; all conflicting rows quarantined', row['id'])
        else:
            indexed[row['id']] = row
    return indexed


def add_recovery(result, task_id, kind, reason, **extra):
    item = {'task_id': task_id, 'kind': kind, 'reason': reason, **extra}
    if item not in result['recovery_tasks']:
        result['recovery_tasks'].append(item)


def recover_claim(task, runs, now, result):
    """Expiry is a reason to investigate, never proof that an executor is dead."""
    run = runs.get(task.get('run_id')) if identifier(task.get('run_id')) else None
    interrupted = (run and run.get('status') == 'interrupted'
                   and run.get('interruption_evidence') and run.get('interrupted_at'))
    if interrupted:
        timestamp(run['interrupted_at'], f"run {run['id']}.interrupted_at")
        kind = 'reconcile_send' if task['kind'] in OUTBOUND else 'requeue_read_only'
        add_recovery(result, task['id'], kind, 'confirmed_interrupted_run', run_id=run['id'])
        return 'delivery_reconciliation_required' if task['kind'] in OUTBOUND else 'recovery_required'
    complete_claim = all(task.get(field) for field in ('run_id', 'claimed_at', 'lease_expires_at'))
    if complete_claim:
        claimed = timestamp(task['claimed_at'], f"{task['id']}.claimed_at")
        expires = timestamp(task['lease_expires_at'], f"{task['id']}.lease_expires_at")
        if expires < claimed:
            raise ValueError('lease_expires_at precedes claimed_at')
        if run and run.get('status') == 'running' and claimed <= now < expires:
            return 'in_progress'
    add_recovery(result, task['id'], 'review_run', 'claim_missing_expired_or_run_unconfirmed', run_id=task.get('run_id'))
    return 'run_review_required'


def coverage(targets, actions, tasks, result, now):
    """Report only recorded scope; never invent channels or declare global coverage."""
    gaps = result['coverage_gaps']
    for action_id, action in actions.items():
        if member(action.get('status'), TERMINAL_ACTIONS):
            continue
        target_ids = action.get('target_ids', [])
        if not isinstance(target_ids, list) or not target_ids or not all(identifier(i) for i in target_ids):
            gaps.append({'action_id': action_id, 'reason': 'target_scope_missing'})
            target_ids = [None]
        for target_id in target_ids:
            match = [t for t in tasks if t.get('action_id') == action_id
                     and (not t.get('target_ids') or target_id in t['target_ids'])]
            covered = False
            for task in match:
                if task['status'] in {'planned', 'ready'}:
                    covered = True
                elif task['status'] == 'blocked' and task.get('reason') and task.get('review_at'):
                    covered = True
                elif task['status'] == 'in_progress' and any(
                        h['id'] == task['id'] and h['reason'] == 'in_progress' for h in result['held_tasks']):
                    covered = True
            if not covered:
                gaps.append({'action_id': action_id, 'target_id': target_id,
                             'provider_id': action.get('provider_id'), 'route': action.get('route'),
                             'reason': 'next_step_or_blocker_review_missing'})
            if target_id is not None and target_id not in targets:
                gaps.append({'action_id': action_id, 'target_id': target_id, 'reason': 'target_reference_unknown'})
    for target_id, target in targets.items():
        if member(target.get('status'), {'closed', 'withdrawn', 'verified_effective', 'closed_no_action'}):
            continue
        linked = [a for a in actions.values() if isinstance(a.get('target_ids'), list) and target_id in a['target_ids']]
        plan = target.get('channel_plan', [])
        if not isinstance(plan, list):
            error(result, 'targets', 'channel_plan must be a list', target_id)
            plan = []
        if not plan:
            gaps.append({'target_id': target_id, 'reason': 'channel_plan_missing'})
        if not linked:
            gaps.append({'target_id': target_id, 'reason': 'action_missing'})
        for channel in plan:
            if not isinstance(channel, dict) or not identifier(channel.get('route')):
                error(result, 'targets', 'channel_plan requires a route', target_id)
                continue
            channel_actions = [a for a in linked if a.get('provider_id') == channel.get('provider_id')
                               and a.get('route') == channel['route']]
            if not channel_actions:
                gaps.append({'target_id': target_id, 'provider_id': channel.get('provider_id'),
                             'route': channel['route'], 'reason': 'channel_action_missing'})
        if linked and all(member(a.get('status'), TERMINAL_ACTIONS) for a in linked):
            gaps.append({'target_id': target_id, 'reason': 'target_result_or_next_route_review_required'})


def build_queue(case, now, horizon_hours=48):
    """Return candidates, per-record errors and recovery gaps without mutating input."""
    if not isinstance(case, dict):
        raise ValueError('case must be an object')
    result = {
        'case_id': case.get('case_id'), 'queue_only': True, 'requires_live_verification': True,
        'due_tasks': [], 'held_tasks': [], 'deferred_tasks': [], 'deadline_alerts': [],
        'unresolved_deadlines': [], 'deadline_conflicts': [], 'recovery_tasks': [],
        'coverage_gaps': [], 'errors': [], 'warnings': [],
    }
    try:
        now, horizon = window(now, horizon_hours)
    except ValueError as exc:
        error(result, 'time_window', str(exc))
        result['state'] = 'invalid_input'
        return result
    follow = case.get('follow_up', {})
    if not isinstance(follow, dict):
        error(result, 'follow_up', 'expected an object')
        follow = {}
    if follow.get('requested') is not True or follow.get('enabled') is not True:
        result['state'] = 'not_enabled'
        return result
    result['state'] = 'preview'
    unsafe = not identifier(case.get('case_id'))
    if unsafe:
        error(result, 'case_id', 'enabled case requires a non-empty id')
    version = case.get('schema_version')
    if version not in (None, '1.1', '1.2', '1.3'):
        error(result, 'schema_version', 'unsupported schema version')
        unsafe = True
    elif version != '1.3':
        result['warnings'].append('legacy_record: review the documented 1.3 migration; no fields were inferred or written')

    # Legal deadlines survive all independent task/attempt validation errors.
    deadlines = []
    for key, deadline in records(case, 'deadlines', result).items():
        if member(deadline.get('status'), CLOSED_DEADLINES):
            continue
        if not deadline.get('due_at'):
            result['unresolved_deadlines'].append({'id': key, 'reason': 'due_at_unknown'})
            continue
        try:
            due = timestamp(deadline['due_at'], f'deadline {key}')
            deadlines.append((deadline, due))
            if due <= horizon:
                result['deadline_alerts'].append({
                    'id': key, 'action_id': deadline.get('action_id'), 'due_at': deadline['due_at'],
                    'due_at_utc': due.isoformat(), 'state': 'overdue' if due < now else 'due_soon',
                })
        except ValueError as exc:
            error(result, 'deadlines', str(exc), key)
            result['unresolved_deadlines'].append({'id': key, 'reason': 'invalid_due_at'})

    before = len(result['errors'])
    actions = records(case, 'actions', result)
    task_map = records(case, 'followup_tasks', result)
    runs = records(case, 'runs', result)
    attempts = records(case, 'outbound_attempts', result)
    unsafe |= len(result['errors']) > before
    targets = records(case, 'targets', result)
    for key, action in actions.items():
        if not member(action.get('status'), ACTION_STATES):
            error(result, 'actions', 'unknown action status', key)
            unsafe = True
        correction = action.get('corrects_action_id')
        if correction is not None and (not identifier(correction) or correction not in actions or correction == key):
            error(result, 'actions', 'corrects_action_id must reference a different existing action', key)
            unsafe = True
    for key, run in runs.items():
        if not member(run.get('status'), {'running', 'completed', 'interrupted'}):
            error(result, 'runs', 'run status needs verification (running/completed/interrupted)', key)
            unsafe = True
    valid_attempts = []
    for key, attempt in attempts.items():
        try:
            if not all(identifier(attempt.get(field)) for field in ('task_id', 'action_id', 'dedupe_key')):
                raise ValueError('attempt requires task_id, action_id and dedupe_key')
            if attempt.get('status') not in ATTEMPT_STATES:
                raise ValueError('unknown delivery status')
            task = task_map.get(attempt['task_id'])
            if attempt['action_id'] not in actions or task is None:
                raise ValueError('attempt has an unknown task/action reference')
            if (task.get('kind') not in OUTBOUND or task.get('action_id') != attempt['action_id']
                    or task.get('dedupe_key') != attempt['dedupe_key']):
                raise ValueError('attempt task/action/dedupe relationship is inconsistent')
            if attempt.get('run_id') is not None and attempt['run_id'] not in runs:
                raise ValueError('attempt has an unknown run reference')
            valid_attempts.append(attempt)
            if attempt['status'] in PENDING_SEND:
                add_recovery(result, attempt['task_id'], 'reconcile_send', 'pending_delivery_record', attempt_id=key)
        except (ValueError, TypeError) as exc:
            error(result, 'outbound_attempts', str(exc), key)
            unsafe = True
            add_recovery(result, attempt.get('task_id'), 'reconcile_send', 'invalid_delivery_record', attempt_id=key)

    valid_tasks = []
    for key, task in task_map.items():
        try:
            kind, state = task.get('kind'), task.get('status')
            if kind not in KINDS or state not in TASK_STATES:
                raise ValueError('unknown kind or status')
            if state in CLOSED_TASKS:
                continue
            action_id = task.get('action_id')
            if action_id is not None and (not identifier(action_id) or action_id not in actions):
                raise ValueError('unknown action_id')
            if kind in OUTBOUND and (action_id is None or not identifier(task.get('dedupe_key'))):
                raise ValueError('outbound task requires action_id and dedupe_key')
            if 'target_ids' in task and (not isinstance(task['target_ids'], list)
                                       or not all(identifier(t) for t in task['target_ids'])):
                raise ValueError('target_ids must be a list of ids')
            action = actions.get(action_id, {})
            if kind == 'send_correction' and not action.get('corrects_action_id'):
                raise ValueError('send_correction requires an independent linked correction action')
            due = timestamp(task.get('due_at'), f'{key}.due_at')
            eligible = due
            constraints = []
            if task.get('not_before'):
                eligible = max(eligible, timestamp(task['not_before'], f'{key}.not_before'))
                constraints.append('task_not_before')
            held = None
            if state == 'blocked':
                if not identifier(task.get('reason')) or not task.get('review_at'):
                    raise ValueError('blocked task requires reason and review_at')
                review = timestamp(task['review_at'], f'{key}.review_at')
                held = 'blocked'
                if review <= now:
                    add_recovery(result, key, 'review_blocker', 'blocker_review_due')
            elif state == 'in_progress':
                held = recover_claim(task, runs, now, result)
            if kind in OUTBOUND:
                af = action.get('followup', {})
                if not isinstance(af, dict):
                    raise ValueError('action.followup must be an object')
                fields = [('all_outbound_not_before', 'provider_all_outbound_restriction')]
                if kind in {'remind', 'resubmit'}:
                    fields.append(('cooldown_until', 'reminder_cooldown'))
                if kind == 'remind':
                    fields.append(('expected_response_due_at', 'provider_response_window'))
                for field, reason in fields:
                    if af.get(field):
                        eligible = max(eligible, timestamp(af[field], f'{action_id}.{field}'))
                        constraints.append(reason)
                if not held:
                    if unsafe:
                        held = 'record_integrity_requires_review'
                    elif action.get('claim_frozen') is True:
                        held = 'claim_frozen'
                    elif member(action.get('status'), TERMINAL_ACTIONS):
                        held = 'action_terminal'
                    elif any(a['action_id'] in {action_id, action.get('corrects_action_id')}
                             and a['status'] in PENDING_SEND for a in valid_attempts):
                        held = 'action_has_unresolved_send'
                    elif any(a['dedupe_key'] == task['dedupe_key'] and a['status'] in (PENDING_SEND | {'sent'})
                             for a in valid_attempts):
                        held = 'matching_sent_or_unresolved_attempt'
            item = {'id': key, 'action_id': action_id, 'kind': kind, 'due_at': task['due_at'],
                    'eligible_at': eligible.isoformat(), 'external_action_candidate': kind in OUTBOUND}
            if held:
                result['held_tasks'].append({'id': key, 'reason': held})
            elif eligible > now:
                result['deferred_tasks'].append({**item, 'reasons': constraints or ['not_due']})
            else:
                result['due_tasks'].append(item)
            for deadline, when in deadlines:
                if deadline.get('action_id') == action_id and action_id is not None:
                    if (held and when <= horizon) or eligible >= when:
                        result['deadline_conflicts'].append({
                            'task_id': key, 'deadline_id': deadline['id'], 'action_id': action_id,
                            'due_at': deadline['due_at'], 'eligible_at': eligible.isoformat(),
                            'reason': held or 'eligibility_at_or_after_deadline',
                        })
            valid_tasks.append(task)
        except (ValueError, TypeError) as exc:
            error(result, 'followup_tasks', str(exc), key)
            result['held_tasks'].append({'id': key, 'reason': 'invalid_task'})
    coverage(targets, actions, valid_tasks, result, now)
    result['due_tasks'].sort(key=lambda t: (t['eligible_at'], t['id']))
    result['deferred_tasks'].sort(key=lambda t: (t['eligible_at'], t['id']))
    result['deadline_alerts'].sort(key=lambda d: d['due_at_utc'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', required=True, type=Path)
    parser.add_argument('--now', help='ISO-8601 timestamp with timezone; defaults to current UTC')
    parser.add_argument('--horizon-hours', type=float, default=48)
    args = parser.parse_args()
    try:
        now = timestamp(args.now, 'now') if args.now else datetime.now(timezone.utc)
        now, _ = window(now, args.horizon_hours)
    except ValueError as exc:
        print(json.dumps({'checked_at': None, 'queue_only': True, 'cases': [],
                          'errors': [{'section': 'arguments', 'error': str(exc)}]}, ensure_ascii=False, indent=2))
        return 2
    report = {'checked_at': now.isoformat(), 'queue_only': True, 'cases': [], 'errors': []}
    for path in args.case:
        try:
            case = json.loads(path.read_text())
            result = build_queue(case, now, args.horizon_hours)
            result['source_path'] = str(path)
            report['cases'].append(result)
            report['errors'].extend({'source_path': str(path), **e} for e in result['errors'])
        except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
            report['errors'].append({'source_path': str(path), 'error': str(exc)})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
