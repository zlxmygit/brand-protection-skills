#!/usr/bin/env python3
"""Read-only follow-up queue and record checks; never sends, schedules or writes cases."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import load_json
from record_integrity import MAX_RECORDS, validate_case_budget


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
OBSERVATION_STATES = {'enabled', 'not_authorized', 'paused', 'ended'}
INVESTIGATION_ACTIONS = {'researching', 'draft', 'awaiting_evidence'}
RECURRENCE_PLAN_WARNING = (
    'recurrence_authorization_requires_plan_review: recorded legacy recurrence_watch authority lacks a valid '
    'explicit observation plan; review current scope, status and schedule. Authority alone does not enable '
    'observation, create tasks or block otherwise valid work.'
)


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


def recurrence_observations(rows, targets, result):
    """An explicit local registration never enables itself or grants authority."""
    valid = {}
    for key, row in rows.items():
        try:
            if not member(row.get('status'), OBSERVATION_STATES):
                raise ValueError('unknown observation status')
            scope = id_list(row.get('target_ids'), 'target_ids')
            if any(target_id not in targets for target_id in scope):
                raise ValueError('target_ids contains an unknown or quarantined target')
            enabled = row['status'] == 'enabled'
            authority = row.get('authorization_source')
            if (authority is not None and not identifier(authority)) or (enabled and not identifier(authority)):
                raise ValueError('enabled observation requires its own authorization_source')
            interval = row.get('interval_hours')
            if interval is not None or enabled:
                if (type(interval) not in (int, float) or not math.isfinite(interval)
                        or interval <= 0):
                    raise ValueError('interval_hours must be a finite positive number')
            review = row.get('next_review_at')
            if review is not None or enabled:
                timestamp(review, f'{key}.next_review_at')
            valid[key] = row
        except (ValueError, TypeError, OverflowError) as exc:
            error(result, 'recurrence_observations', str(exc), key)
    return valid


def task_scope(task, actions, observations):
    if 'target_ids' in task:
        return task['target_ids']
    action = actions.get(task.get('action_id'), {})
    if action:
        return action.get('target_ids', [])
    observation = observations.get(task.get('recurrence_observation_id'), {})
    return observation.get('target_ids', [])


def task_links(task, actions, targets, deadlines, observations, runs):
    """Check closed history too; scheduling fields are checked only for live work."""
    kind, state = task.get('kind'), task.get('status')
    if not member(kind, KINDS) or not member(state, TASK_STATES):
        raise ValueError('unknown kind or status')
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
            if deadline_id not in deadlines:
                raise ValueError('unknown or quarantined deadline_id')
            owner = deadlines[deadline_id].get('action_id')
            if owner is not None and owner != action_id:
                raise ValueError('deadline_id belongs to another action')
    if kind == 'send_correction' and not action.get('corrects_action_id'):
        raise ValueError('send_correction requires an independent linked correction action')
    if 'recurrence_observation_id' in task:
        observation_id = task['recurrence_observation_id']
        if not identifier(observation_id) or observation_id not in observations:
            raise ValueError('unknown or quarantined recurrence_observation_id')
        if kind != 'check_recurrence':
            raise ValueError('recurrence_observation_id requires a check_recurrence task')
        if any(target_id not in observations[observation_id]['target_ids']
               for target_id in task_scope(task, actions, observations)):
            raise ValueError('task target scope is outside its recurrence observation')
    if task.get('run_id') is not None and (not identifier(task['run_id']) or task['run_id'] not in runs):
        raise ValueError('unknown or quarantined run_id')


def coverage(targets, actions, tasks, observations, result):
    """Report only recorded scope; never invent channels or declare global coverage."""
    gaps = result['coverage_gaps']
    available = {t['id']: t for t in result['due_tasks'] + result['deferred_tasks']}
    held = {t['id']: t['reason'] for t in result['held_tasks']}
    usable = {t['id'] for t in tasks if t['id'] in available
              or (t['status'] == 'blocked' and held.get(t['id']) == 'blocked')
              or (t['status'] == 'in_progress' and held.get(t['id']) == 'in_progress')}
    investigation_targets = {target_id for task in tasks
                             if task['id'] in usable and task['kind'] not in OUTBOUND | {'check_recurrence'}
                             and task.get('action_id') is None and 'target_ids' in task
                             for target_id in task['target_ids']}
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
            covered = any(task['id'] in usable for task in match)
            if (member(action.get('status'), INVESTIGATION_ACTIONS)
                    and targets.get(target_id, {}).get('phase') == 'investigation'
                    and target_id in investigation_targets):
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
        if target.get('phase') == 'investigation':
            if target_id not in investigation_targets:
                gaps.append({'target_id': target_id, 'reason': 'investigation_next_step_or_blocker_review_missing'})
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
    # Index observation coverage once; a large registration list must not multiply task scans.
    observed_targets = {}
    for task in tasks:
        observation_id = task.get('recurrence_observation_id')
        observation = observations.get(observation_id)
        if task['id'] not in usable or not observation or observation['status'] != 'enabled':
            continue
        when = (task['review_at'] if task['status'] == 'blocked'
                else available.get(task['id'], {}).get('eligible_at', task['due_at']))
        review = timestamp(observation['next_review_at'], f'{observation_id}.next_review_at')
        if timestamp(when, 'observation task review') <= review:
            observed_targets.setdefault(observation_id, set()).update(task_scope(task, actions, observations))
    # Observation has an independent lifecycle, including after the notice/target closes.
    for observation_id, observation in observations.items():
        if observation['status'] != 'enabled':
            continue
        for target_id in observation['target_ids']:
            if target_id not in observed_targets.get(observation_id, set()):
                gaps.append({'observation_id': observation_id, 'target_id': target_id,
                             'next_review_at': observation['next_review_at'],
                             'reason': 'recurrence_next_step_or_blocker_review_missing'})


def build_queue(case, now, horizon_hours=48, validate_only=False):
    """Return candidates, per-record errors and recovery gaps without mutating input."""
    if not isinstance(case, dict):
        raise ValueError('case must be an object')
    if type(validate_only) is not bool:
        raise ValueError('validate_only must be a boolean')
    result = {
        'case_id': case.get('case_id'), 'queue_only': True, 'requires_live_verification': True,
        'due_tasks': [], 'held_tasks': [], 'deferred_tasks': [], 'deadline_alerts': [],
        'unresolved_deadlines': [], 'deadline_conflicts': [], 'recovery_tasks': [],
        'coverage_gaps': [], 'errors': [], 'warnings': [],
        'validation_only': validate_only,
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
    authorizations = follow.get('authorizations', {})
    recorded_recurrence_authority = (isinstance(authorizations, dict)
                                     and identifier(authorizations.get('recurrence_watch')))
    if recorded_recurrence_authority and case.get('recurrence_observations', []) == []:
        result['warnings'].append(RECURRENCE_PLAN_WARNING)
    follow_state = 'preview' if follow.get('requested') is True and follow.get('enabled') is True else 'not_enabled'
    for source in ('scheduler', 'host_adapter'):
        settings = follow.get(source, {})
        if not isinstance(settings, dict):
            error(result, 'follow_up', f'{source}: expected an object')
        elif settings.get('status') == 'paused' and follow_state == 'preview':
            follow_state = 'paused'
    result['follow_up_state'] = follow_state
    if follow_state != 'preview' and not validate_only:
        result['state'] = follow_state
        return result
    result['state'] = 'validation_only' if validate_only else 'preview'
    unsafe = bool(result['errors'])
    if not identifier(case.get('case_id')):
        error(result, 'case_id', 'validated case requires a non-empty id')
        unsafe = True
    version = case.get('schema_version')
    if version not in (None, '1.1', '1.2', '1.3'):
        error(result, 'schema_version', 'unsupported schema version')
        unsafe = True
    elif version != '1.3':
        result['warnings'].append('legacy_record: review the documented 1.3 migration; no fields were inferred or written')

    # Check budgets before graph/index work, while retaining bounded independent deadlines.
    budget_error = None
    try:
        validate_case_budget(case)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        budget_error = str(exc)
        error(result, 'record_budget', budget_error)
    deadlines = []
    rows = case.get('deadlines', [])
    if budget_error and isinstance(rows, list) and len(rows) > MAX_RECORDS:
        error(result, 'deadlines', 'deadline collection exceeds independent scan limit')
        deadline_map = {}
    else:
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
    if budget_error:
        result['state'] = 'record_budget_exceeded'
        result['warnings'].append('coverage_not_checked: record budget exceeded; no task candidates were produced')
        result['deadline_alerts'].sort(key=lambda d: d['due_at_utc'])
        return result

    before = len(result['errors'])
    actions = records(case, 'actions', result)
    task_map = records(case, 'followup_tasks', result)
    runs = records(case, 'runs', result)
    attempts = records(case, 'outbound_attempts', result)
    targets = records(case, 'targets', result)
    observation_rows = records(case, 'recurrence_observations', result)
    requires_observation_link = ('recurrence_observations' in case and case['recurrence_observations'] != [])
    observations = recurrence_observations(observation_rows, targets, result)
    if (recorded_recurrence_authority and not observations
            and RECURRENCE_PLAN_WARNING not in result['warnings']):
        result['warnings'].append(RECURRENCE_PLAN_WARNING)
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
    for key, deadline in deadline_map.items():
        owner = deadline.get('action_id')
        if owner is not None and (not identifier(owner) or owner not in actions):
            error(result, 'deadlines', 'unknown or quarantined action_id', key)
            unsafe = True
    before = len(result['errors'])
    groups = correction_groups(actions, result)
    unsafe |= len(result['errors']) > before
    for key, run in runs.items():
        if not member(run.get('status'), {'running', 'completed', 'interrupted'}):
            error(result, 'runs', 'run status needs verification (running/completed/interrupted)', key)
            unsafe = True
    # Closed history participates in integrity checks before any candidate is chosen.
    linked_tasks = {}
    for key, task in task_map.items():
        try:
            task_links(task, actions, targets, deadline_map, observations, runs)
            linked_tasks[key] = task
        except (ValueError, TypeError) as exc:
            error(result, 'followup_tasks', str(exc), key)
            result['held_tasks'].append({'id': key, 'reason': 'invalid_task'})
            unsafe = True
            if task.get('status') == 'in_progress':
                add_recovery(result, key, 'review_run', 'invalid_claim_or_task_reference', run_id=task.get('run_id'))
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
            if (attempt.get('run_id') is not None and task.get('run_id') is not None
                    and attempt['run_id'] != task['run_id']):
                raise ValueError('attempt run_id differs from its task executor')
            valid_attempts.append(attempt)
            if attempt['status'] in PENDING_SEND:
                add_recovery(result, attempt['task_id'], 'reconcile_send', 'pending_delivery_record', attempt_id=key)
        except (ValueError, TypeError) as exc:
            error(result, 'outbound_attempts', str(exc), key)
            unsafe = True
            add_recovery(result, attempt.get('task_id'), 'reconcile_send', 'invalid_delivery_record', attempt_id=key)

    claimed, pending, used_keys, collisions = delivery_barriers(task_map, valid_attempts, groups)
    valid_tasks = []
    for key, task in linked_tasks.items():
        try:
            kind, state = task.get('kind'), task.get('status')
            if state in CLOSED_TASKS:
                continue
            action_id = task.get('action_id')
            action = actions.get(action_id, {})
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
            observation = observations.get(task.get('recurrence_observation_id'))
            if observation and observation['status'] != 'enabled':
                held = 'recurrence_observation_not_enabled'
            elif (kind == 'check_recurrence' and requires_observation_link
                    and 'recurrence_observation_id' not in task):
                held = 'recurrence_observation_link_required'
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
            if 'recurrence_observation_id' in task:
                item['recurrence_observation_id'] = task['recurrence_observation_id']
                item['target_ids'] = task_scope(task, actions, observations)
            if held:
                result['held_tasks'].append({'id': key, 'reason': held})
            elif eligible > now:
                result['deferred_tasks'].append({**item, 'reasons': constraints or ['not_due']})
            else:
                result['due_tasks'].append(item)
            for deadline, when in deadlines:
                if linked_deadline(task, deadline):
                    conflict = None
                    if held and held != 'in_progress' and when <= horizon:
                        conflict = held
                    elif eligible >= when:
                        conflict = 'eligibility_at_or_after_deadline'
                    elif held == 'in_progress' and when <= now:
                        conflict = 'deadline_passed_while_in_progress'
                    if conflict:
                        result['deadline_conflicts'].append({
                            'task_id': key, 'deadline_id': deadline['id'], 'action_id': action_id,
                            'due_at': deadline['due_at'], 'eligible_at': eligible.isoformat(),
                            'reason': conflict,
                        })
            valid_tasks.append(task)
        except (ValueError, TypeError) as exc:
            error(result, 'followup_tasks', str(exc), key)
            result['held_tasks'].append({'id': key, 'reason': 'invalid_task'})
    coverage(targets, actions, valid_tasks, observations, result)
    result['due_tasks'].sort(key=lambda t: (t['eligible_at'], t['id']))
    result['deferred_tasks'].sort(key=lambda t: (t['eligible_at'], t['id']))
    result['deadline_alerts'].sort(key=lambda d: d['due_at_utc'])
    if validate_only:
        result['due_tasks'] = []
        result['deferred_tasks'] = []
        result['recovery_tasks'] = []
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', required=True, type=Path)
    parser.add_argument('--now', help='ISO-8601 timestamp with timezone; defaults to current UTC')
    parser.add_argument('--horizon-hours', type=float, default=48)
    parser.add_argument('--validate-only', action='store_true',
                        help='validate disabled/paused cases too; never return task or recovery candidates')
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
            case = load_json(path)
            result = build_queue(case, now, args.horizon_hours, validate_only=args.validate_only)
            result['source_path'] = str(path)
            report['cases'].append(result)
            report['errors'].extend({'source_path': str(path), **e} for e in result['errors'])
        except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError) as exc:
            report['errors'].append({'source_path': str(path), 'error': str(exc)})
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
