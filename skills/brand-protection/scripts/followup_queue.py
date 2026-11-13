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
    'initial_notice', 'check_feedback', 'check_target', 'verify_receipt', 'remind', 'resubmit',
    'send_supplement', 'send_escalation', 'send_correction', 'prepare_escalation',
    'verify_effect', 'check_recurrence', 'reconcile_send',
}
OUTBOUND = {'initial_notice', 'remind', 'resubmit', 'send_supplement', 'send_escalation', 'send_correction'}
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


def id_list(value, label, allow_empty=False):
    """Validate an explicit scope; missing and empty are never interchangeable."""
    if (not isinstance(value, list) or (not value and not allow_empty)
            or not all(identifier(item) for item in value)):
        raise ValueError(f'{label}: expected {"a" if allow_empty else "a non-empty"} list of ids')
    if len(set(value)) != len(value):
        raise ValueError(f'{label}: duplicate ids')
    return value


def correction_groups(actions, result):
    """Union related notices for delivery barriers, not for authority or freezing."""
    roots = {key: key for key in actions}

    def root(key):
        while roots[key] != key:
            roots[key] = roots[roots[key]]
            key = roots[key]
        return key

    for key, action in actions.items():
        original = action.get('corrects_action_id')
        if not identifier(original) or original not in actions or original == key:
            continue  # The action validator reports invalid references.
        left, right = root(key), root(original)
        if left == right:
            error(result, 'actions', 'cyclic corrects_action_id relationship', key)
        else:
            roots[left] = right
    return {key: root(key) for key in actions}


def delivery_barriers(tasks, attempts, groups):
    """Precompute barriers before selection so input ordering cannot grant a send."""
    claimed = {groups[t['action_id']] for t in tasks.values()
               if member(t.get('kind'), OUTBOUND) and t.get('status') == 'in_progress'
               and identifier(t.get('action_id')) and t['action_id'] in groups}
    pending = {groups[a['action_id']] for a in attempts if a['status'] in PENDING_SEND}
    used_keys = {a['dedupe_key'] for a in attempts if a['status'] in PENDING_SEND | {'sent'}}
    counts = Counter(t['dedupe_key'] for t in tasks.values()
                     if member(t.get('kind'), OUTBOUND)
                     and not member(t.get('status'), CLOSED_TASKS)
                     and identifier(t.get('dedupe_key')))
    collisions = {key for key, count in counts.items() if count > 1}
    return claimed, pending, used_keys, collisions


def linked_deadline(task, deadline):
    if 'deadline_ids' in task:
        return deadline['id'] in task['deadline_ids']
    # Legacy records lack explicit linkage. Keep conservative operational checks,
    # but post-result observation does not satisfy a pending legal response.
    return (task['kind'] not in {'check_recurrence', 'verify_effect'}
            and task.get('action_id') is not None
            and deadline.get('action_id') == task['action_id'])


def unique_object(pairs):
    """Reject ambiguous JSON before overwritten deadlines/permissions disappear."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON member: {key}')
        result[key] = value
    return result


def finite_number(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('non-finite JSON number')
    return parsed


def reject_constant(value):
    raise ValueError(f'non-standard JSON constant: {value}')


def coverage(targets, actions, tasks, result, now):
    """Report only recorded scope; never invent channels or declare global coverage."""
    gaps = result['coverage_gaps']
    available = {t['id'] for t in result['due_tasks'] + result['deferred_tasks']}
    held = {t['id']: t['reason'] for t in result['held_tasks']}
    for action_id, action in actions.items():
        if member(action.get('status'), TERMINAL_ACTIONS):
            continue
        target_ids = action.get('target_ids', [])
        if not isinstance(target_ids, list) or not target_ids or not all(identifier(i) for i in target_ids):
            gaps.append({'action_id': action_id, 'reason': 'target_scope_missing'})
            target_ids = [None]
        for target_id in target_ids:
            match = [t for t in tasks if t.get('action_id') == action_id
                     and ('target_ids' not in t or target_id in t['target_ids'])]
            covered = False
            for task in match:
                if task['id'] in available:
                    covered = True
                elif task['status'] == 'blocked' and held.get(task['id']) == 'blocked':
                    covered = True  # reason/review_at were validated before inclusion.
                elif task['status'] == 'in_progress' and held.get(task['id']) == 'in_progress':
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
    for field in ('requested', 'enabled'):
        if field in follow and type(follow[field]) is not bool:
            error(result, 'follow_up', f'{field}: expected a boolean')
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
    deadline_map = records(case, 'deadlines', result)
    for key, deadline in deadline_map.items():
        if member(deadline.get('status'), CLOSED_DEADLINES):
            continue
        if deadline.get('due_at') is None:
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
    targets = records(case, 'targets', result)
    unsafe |= len(result['errors']) > before
    invalid_actions = set()
    for key, action in actions.items():
        if not member(action.get('status'), ACTION_STATES):
            error(result, 'actions', 'unknown action status', key)
            unsafe = True
        correction = action.get('corrects_action_id')
        if correction is not None and (not identifier(correction) or correction not in actions or correction == key):
            error(result, 'actions', 'corrects_action_id must reference a different existing action', key)
            unsafe = True
        try:
            if 'claim_frozen' in action and type(action['claim_frozen']) is not bool:
                raise ValueError('claim_frozen must be a boolean when present')
            scope = id_list(action.get('target_ids'), 'target_ids')
            if any(target_id not in targets for target_id in scope):
                raise ValueError('target_ids contains an unknown or quarantined target')
            af = action.get('followup', {})
            if not isinstance(af, dict):
                raise ValueError('followup must be an object')
            for field in ('all_outbound_not_before', 'cooldown_until', 'expected_response_due_at'):
                if af.get(field) is not None:
                    timestamp(af[field], f'{key}.{field}')
        except ValueError as exc:
            error(result, 'actions', str(exc), key)
            invalid_actions.add(key)
    before = len(result['errors'])
    groups = correction_groups(actions, result)
    unsafe |= len(result['errors']) > before
    # Unclassifiable historical work may have sent. Do not let task order hide it.
    for key, task in task_map.items():
        if not member(task.get('kind'), KINDS) or not member(task.get('status'), TASK_STATES):
            unsafe = True
        if member(task.get('kind'), OUTBOUND) and not member(task.get('status'), CLOSED_TASKS):
            if (not identifier(task.get('action_id')) or task['action_id'] not in actions
                    or not identifier(task.get('dedupe_key'))):
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

    claimed, pending, used_keys, collisions = delivery_barriers(task_map, valid_attempts, groups)
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
            action = actions.get(action_id, {})
            if 'target_ids' in task:
                scope = id_list(task['target_ids'], 'target_ids')
                if any(target_id not in targets for target_id in scope):
                    raise ValueError('target_ids contains an unknown or quarantined target')
                parent_scope = action.get('target_ids')
                if action_id is not None and (not isinstance(parent_scope, list)
                        or any(target_id not in parent_scope for target_id in scope)):
                    raise ValueError('task target_ids must be within its action target_ids')
            if 'deadline_ids' in task:
                for deadline_id in id_list(task['deadline_ids'], 'deadline_ids', allow_empty=True):
                    if deadline_id not in deadline_map:
                        raise ValueError('unknown or quarantined deadline_id')
                    owner = deadline_map[deadline_id].get('action_id')
                    if owner is not None and owner != action_id:
                        raise ValueError('deadline_id belongs to another action')
            if kind == 'send_correction' and not action.get('corrects_action_id'):
                raise ValueError('send_correction requires an independent linked correction action')
            due = timestamp(task.get('due_at'), f'{key}.due_at')
            eligible = due
            constraints = []
            if task.get('not_before') is not None:
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
                    if af.get(field) is not None:
                        eligible = max(eligible, timestamp(af[field], f'{action_id}.{field}'))
                        constraints.append(reason)
                duplicate_key = task['dedupe_key'] in collisions
                if duplicate_key:
                    error(result, 'followup_tasks', 'duplicate active outbound dedupe_key; all candidates held', key)
                if not held:
                    if unsafe or action_id in invalid_actions:
                        held = 'record_integrity_requires_review'
                    elif duplicate_key:
                        held = 'duplicate_dedupe_key'
                    elif action.get('claim_frozen') is True:
                        held = 'claim_frozen'
                    elif member(action.get('status'), TERMINAL_ACTIONS):
                        held = 'action_terminal'
                    elif groups[action_id] in claimed:
                        held = 'action_has_unresolved_claim'
                    elif groups[action_id] in pending:
                        held = 'action_has_unresolved_send'
                    elif task['dedupe_key'] in used_keys:
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
                if linked_deadline(task, deadline):
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
                          'errors': [{'section': 'arguments', 'error': str(exc)}]}, ensure_ascii=True, indent=2))
        return 2
    report = {'checked_at': now.isoformat(), 'queue_only': True, 'cases': [], 'errors': []}
    for path in args.case:
        try:
            case = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique_object,
                              parse_constant=reject_constant, parse_float=finite_number)
            result = build_queue(case, now, args.horizon_hours)
            result['source_path'] = str(path)
            report['cases'].append(result)
            report['errors'].extend({'source_path': str(path), **e} for e in result['errors'])
        except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError) as exc:
            report['errors'].append({'source_path': str(path), 'error': str(exc)})
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
