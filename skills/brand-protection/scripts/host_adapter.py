#!/usr/bin/env python3
"""Validate or atomically persist local host receipts. Never connects or sends."""
import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_web import private_root
from evidence_export import digest, read_evidence
from filesystem_paths import checked_existing_path
from investigation import MAX_FILE_BYTES, read_json, text, timestamp
from record_integrity import check_identity, identifier, references, validate_case_budget

VERSION = '1.0'
PENDING = {'prepared', 'in_flight', 'outcome_unknown'}
SECTIONS = {'actions', 'deadlines', 'followup_tasks', 'outbound_attempts'}
STATUSES = {'disabled', 'active', 'paused'}
MAX_PAGES = 100


class LockBusy(ValueError):
    pass


class CommitUncertain(ValueError):
    """Replacement succeeded, but verification failed; never automatically retry."""


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + ': expected an object')
    return value


def _fields(value, required, optional=()):
    _object(value, 'protocol record')
    if not set(required).issubset(value) or set(value) - set(required) - set(optional):
        raise ValueError('Missing or unknown protocol fields')


def _now(value):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('now requires timezone')
    return value.astimezone(timezone.utc)


def _time(value, label, now):
    result = timestamp(value, label)
    if result > now:
        raise ValueError(label + ': future evidence is not accepted')
    return result


def _revision(value):
    if type(value) is not int or value < 0:
        raise ValueError('revision must be a nonnegative integer')
    return value


def _rows(value, label):
    if not isinstance(value, list):
        raise ValueError(label + ': expected a record list')
    result = {}
    for row in value:
        _object(row, label)
        key = identifier(row.get('id'), label + '.id')
        if key in result:
            raise ValueError(label + ': duplicate id')
        result[key] = row
    return result


def _file(root, value):
    supplied = Path(value)
    path = checked_existing_path(supplied if supplied.is_absolute() else root/supplied)
    if not path.is_file() or root not in path.parents:
        raise ValueError('Input must be an existing file inside the explicit private root')
    return path


def _load(path):
    value = read_json(path)
    validate_case_budget(value)
    return _object(value, 'JSON input')


def _config(case):
    follow = _object(case.get('follow_up', {}), 'follow_up')
    config = follow.get('host_adapter', {'version':VERSION,'status':'disabled'})
    _object(config, 'host_adapter')
    if config.get('version') != VERSION or config.get('status') not in STATUSES:
        raise ValueError('Unsupported host_adapter configuration')
    if config['status'] == 'active':
        text(config.get('authorization_source'), 'host authorization_source')
        _interval(config)
    return config


@contextmanager
def case_lock(case_path):
    """Lock a stable inode. Keep the file after release; the OS recovers dead locks."""
    case_path = _file(private_root(Path(case_path).parent), case_path)
    lock_path = case_path.with_name('.' + case_path.name + '.host-adapter.lock')
    if lock_path.exists() or lock_path.is_symlink():
        checked_existing_path(lock_path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(lock_path, flags, 0o600)
    acquired = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError('Lock must be a regular, unshared local file')
        if os.name == 'nt':
            import msvcrt
            if metadata.st_size == 0:
                os.write(descriptor, b'\0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LockBusy('Another host adapter holds the case lock') from exc
        else:
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockBusy('Another host adapter holds the case lock') from exc
        acquired = True
        yield
    finally:
        try:
            if acquired:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _sources(case):
    result = {}
    for source_id, row in _rows(case.get('follow_up', {}).get('feedback_sources', []), 'feedback_sources').items():
        check_identity(case, row, 'feedback source')
        account = identifier(row.get('account_scope'), 'account_scope')
        threads = references(row, 'authorized_thread_or_ticket_ids')
        if not threads:
            raise ValueError('A feedback source requires explicit authorized threads')
        text(row.get('authorization_source'), 'source authorization_source')
        result[(source_id, account)] = (row, set(threads))
    return result


def _cursor(value):
    return None if value is None else identifier(value, 'cursor')


def _message_key(row):
    return json.dumps([row[field] for field in ('source_id','account_scope','thread_or_ticket_id','message_id')],
                      ensure_ascii=True, separators=(',', ':'))


def _reconciliation(case, row, root, now):
    value = row.get('reconciliation')
    _fields(value, {'conclusion','checked_at','checked_by','execution_terminated','evidence_ids'})
    if value['conclusion'] != row['status'] or value['execution_terminated'] is not True:
        raise ValueError('Reconciliation must establish the outcome and terminated execution')
    _time(value['checked_at'], 'reconciliation.checked_at', now)
    text(value['checked_by'], 'reconciliation.checked_by')
    evidence = _rows(case.get('evidence', []), 'evidence')
    keys = references(value, 'evidence_ids')
    if not keys:
        raise ValueError('Reconciliation requires preserved evidence')
    for key in keys:
        if key not in evidence:
            raise ValueError('Unknown reconciliation evidence')
        record = evidence[key]
        check_identity(case, record, 'reconciliation evidence')
        read_evidence(root, record.get('original_path'), digest(record.get('sha256')))


def _history_families(original, retained, attempts):
    """One multi-source O(V+E) walk preserves every historical correction edge."""
    graph = {key:[] for key in original}
    for key, row in original.items():
        parent = row.get('corrects_action_id')
        if isinstance(parent, str) and parent in graph:
            graph[key].append(parent)
            graph[parent].append(key)
    pending = []
    for row in attempts.values():
        action_id = identifier(row.get('action_id'), 'outbound.action_id')
        if action_id not in original:
            raise ValueError('Historical outbound action must exist')
        pending.append(action_id)
    selected = set()
    while pending:
        key = pending.pop()
        if key in selected:
            continue
        selected.add(key)
        pending.extend(graph[key])
    for key in selected:
        parent = original[key].get('corrects_action_id')
        if parent is not None:
            identifier(parent, 'historical corrects_action_id')
            if parent not in original:
                raise ValueError('Historical correction parent must exist')
        if key not in retained or retained[key].get('corrects_action_id') != parent:
            raise ValueError('Historical outbound correction family IDs and edges must be retained')


def _records(case, updated, replacements, root, now):
    _object(replacements, 'records')
    if set(replacements) - SECTIONS:
        raise ValueError('Unsupported record replacement section')
    for section, rows in replacements.items():
        for row in _rows(rows, section).values():
            check_identity(case, row, section)
        updated[section] = copy.deepcopy(rows)
    old = _rows(case.get('outbound_attempts', []), 'outbound_attempts')
    new = _rows(updated.get('outbound_attempts', []), 'outbound_attempts')
    original_sections = {section:_rows(case.get(section, []), section) for section in ('actions','followup_tasks')}
    retained_sections = {section:_rows(updated.get(section, []), section) for section in ('actions','followup_tasks')}
    _history_families(original_sections['actions'], retained_sections['actions'], old)
    mutable = {'status','reconciliation','sent_at','receipt_or_message_id','completed_at'}
    for key, before in old.items():
        if key not in new:
            raise ValueError('Existing outbound history may not be removed')
        after = new[key]
        current, following = before['status'], after.get('status')
        if current in {'sent','confirmed_not_sent'}:
            if any(after.get(field) != value for field,value in before.items()):
                raise ValueError('Terminal outbound facts may only receive additional details')
        elif current in PENDING:
            if any(after.get(field) != value for field,value in before.items() if field not in mutable):
                raise ValueError('Existing outbound identity or payload may not be changed')
            if following in {'sent','confirmed_not_sent'}:
                _reconciliation(updated, after, root, now)
            elif following != current and (current, following) not in {
                    ('prepared','in_flight'),('prepared','outcome_unknown'),('in_flight','outcome_unknown')}:
                raise ValueError('Unresolved outbound outcome requires reconciliation')
        else:
            raise ValueError('Unknown persisted outbound status')
        for field, section in (('action_id','actions'),('task_id','followup_tasks')):
            if field not in before and field == 'task_id':
                continue
            reference = identifier(before.get(field), 'outbound.' + field)
            original, retained = original_sections[section], retained_sections[section]
            if reference not in original or reference not in retained:
                raise ValueError('Existing outbound action/task history must be retained')
            if following in PENDING and retained[reference] != original[reference]:
                raise ValueError('Reconcile pending delivery before changing its action/task')
            for binding in ('id','action_id','target_id','target_ids','provider_id','route','kind','dedupe_key','run_id'):
                if original[reference].get(binding) != retained[reference].get(binding):
                    raise ValueError('Outbound action/task identity and references must be retained')


def _transition(case, updated, value, now):
    config = copy.deepcopy(_config(case))
    if value is None:
        return config
    _fields(value, {'from','to','authorization_source','recorded_at'},
            {'expected_interval_hours','grace_hours'})
    if value['from'] != config['status'] or (value['from'],value['to']) not in {
            ('disabled','active'),('active','paused'),('paused','active'),
            ('active','disabled'),('paused','disabled')}:
        raise ValueError('Invalid explicit host state transition')
    text(value['authorization_source'], 'transition.authorization_source')
    _time(value['recorded_at'], 'transition.recorded_at', now)
    config.update({key:value[key] for key in ('expected_interval_hours','grace_hours') if key in value})
    config.update(status=value['to'], authorization_source=value['authorization_source'],
                  last_transition=copy.deepcopy(value))
    if config['status'] == 'active':
        _interval(config)
    updated.setdefault('follow_up', {})['host_adapter'] = config
    return config


def _interval(config):
    interval, grace = config.get('expected_interval_hours'), config.get('grace_hours')
    for value in (interval, grace):
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError('Host interval and grace must be finite numbers')
    if not 0 < interval <= 8760 or not 0 <= grace <= 8760:
        raise ValueError('Host interval or grace is outside the supported range')
    return interval + grace


def record_digest(row):
    encoded = json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('ascii')).hexdigest()


def _derived(case, updated, derived, replacements, read_at):
    _fields(derived, {'category','summary','observed_at','effect'},
            {'action_id','record_refs','reason','review_at'})
    identifier(derived['category'], 'derived category')
    text(derived['summary'], 'derived summary', 4000)
    _time(derived['observed_at'], 'derived observed_at', read_at)
    if 'action_id' in derived and derived['action_id'] not in _rows(updated.get('actions', []), 'actions'):
        raise ValueError('Derived state references an unknown action')
    effect = derived['effect']
    if effect == 'records_updated':
        refs = derived.get('record_refs')
        if not isinstance(refs, list) or not refs:
            raise ValueError('records_updated requires changed record references')
        selected = set()
        for reference in refs:
            _fields(reference, {'section','id','after_sha256'})
            section, key = reference['section'], identifier(reference['id'], 'record reference')
            if section not in SECTIONS or section not in replacements or (section,key) in selected:
                raise ValueError('Message effects must reference same-commit unique replacements')
            selected.add((section,key))
            before = _rows(case.get(section, []), section)
            after = _rows(updated.get(section, []), section)
            if (key not in after or before.get(key) == after[key]
                    or digest(reference['after_sha256']) != record_digest(after[key])):
                raise ValueError('Declared message effect was not persisted in this transaction')
    elif effect in {'no_change','requires_review'}:
        text(derived.get('reason'), 'derived reason', 4000)
        if 'record_refs' in derived:
            raise ValueError('Record references require records_updated')
        if effect == 'requires_review':
            if timestamp(derived.get('review_at'), 'review_at') < timestamp(derived['observed_at'], 'observed_at'):
                raise ValueError('Review time must not precede the observation')
    else:
        raise ValueError('Expected records_updated, no_change or requires_review effect')


def _feedback(case, updated, batches, root, now, revision, completed, replacements):
    if not isinstance(batches, list):
        raise ValueError('feedback_batches must be a list')
    sources = _sources(case)
    follow = updated.setdefault('follow_up', {})
    cursors = {}
    for row in follow.get('source_cursors', []):
        _object(row, 'source cursor')
        key = (identifier(row.get('source_id'), 'cursor.source_id'),
               identifier(row.get('account_scope'), 'cursor.account_scope'))
        if key in cursors or key not in sources:
            raise ValueError('Duplicate or unknown saved source cursor')
        _cursor(row.get('cursor'))
        if _revision(row.get('committed_revision')) > revision - 1:
            raise ValueError('Saved cursor has a future revision')
        cursors[key] = row
    processed = follow.get('processed_message_ids', [])
    events = follow.get('feedback_events', [])
    receipts = follow.get('feedback_read_receipts', [])
    reviews = follow.get('feedback_review_items', [])
    if not all(isinstance(value, list) for value in (processed,events,receipts,reviews)):
        raise ValueError('Feedback state must use lists')
    _rows(reviews, 'feedback_review_items')
    if any(not isinstance(key, str) for key in processed) or len(processed) != len(set(processed)):
        raise ValueError('Invalid processed message keys')
    existing = {}
    for event in events:
        _object(event, 'feedback event')
        key = event.get('message_key')
        if not isinstance(key, str) or key in existing or _message_key(event) != key:
            raise ValueError('Invalid or duplicate feedback event')
        existing[key] = event
    if set(processed) != set(existing):
        raise ValueError('Processed keys must retain their corresponding derived state')
    covered = set()
    for batch in batches:
        _fields(batch, {'source_id','account_scope','covered_thread_or_ticket_ids',
                        'from_cursor','pages','read_receipt','read_at'})
        scope = (identifier(batch['source_id'], 'source_id'), identifier(batch['account_scope'], 'account_scope'))
        if scope not in sources or scope in covered:
            raise ValueError('Unknown, cross-account or duplicate feedback scope')
        covered.add(scope)
        threads = set(references(batch, 'covered_thread_or_ticket_ids'))
        if threads != sources[scope][1]:
            raise ValueError('Feedback coverage must include every authorized thread exactly')
        read_at = _time(batch['read_at'], 'batch.read_at', completed)
        _fields(batch['read_receipt'], {'path','sha256'})
        read_evidence(root, batch['read_receipt']['path'], digest(batch['read_receipt']['sha256']))
        cursor = _cursor(batch['from_cursor'])
        if cursor != cursors.get(scope, {}).get('cursor'):
            raise ValueError('Feedback cursor does not match the persisted position')
        pages = batch['pages']
        if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
            raise ValueError('Expected a bounded, complete feedback page chain')
        seen_pages, seen_messages = set(), set()
        for position,page in enumerate(pages):
            _fields(page, {'cursor','next_cursor','complete','messages'})
            current = _cursor(page['cursor'])
            next_cursor = _cursor(page['next_cursor'])
            if current != cursor or current in seen_pages:
                raise ValueError('Feedback page chain has a gap or cycle')
            seen_pages.add(current)
            last = position == len(pages) - 1
            if type(page['complete']) is not bool or page['complete'] != last:
                raise ValueError('Feedback page chain is incomplete or ends early')
            if not isinstance(page['messages'], list):
                raise ValueError('Feedback messages must be a list')
            for message in page['messages']:
                _fields(message, {'source_id','account_scope','thread_or_ticket_id','message_id',
                                  'content_sha256','derived_state'})
                if (message['source_id'],message['account_scope']) != scope:
                    raise ValueError('Message belongs to another feedback source or account')
                if identifier(message['thread_or_ticket_id'], 'thread_or_ticket_id') not in threads:
                    raise ValueError('Message thread is outside the authorized scope')
                identifier(message['message_id'], 'message_id')
                digest(message['content_sha256'])
                derived = message['derived_state']
                key = _message_key(message)
                if key in seen_messages:
                    raise ValueError('Duplicate message within feedback page chain')
                seen_messages.add(key)
                if key in existing:
                    if existing[key]['content_sha256'] != message['content_sha256'] or existing[key]['derived_state'] != derived:
                        raise ValueError('Previously processed message has conflicting content or state')
                    continue
                _derived(case, updated, derived, replacements, read_at)
                event = copy.deepcopy(message)
                event.update(message_key=key, committed_revision=revision)
                events.append(event)
                existing[key] = event
                processed.append(key)
                if derived['effect'] == 'requires_review':
                    reviews.append({'id':'feedback-' + hashlib.sha256(key.encode('ascii')).hexdigest(),
                                    'message_key':key,'status':'open','reason':derived['reason'],
                                    'review_at':derived['review_at'],'committed_revision':revision})
            cursor = next_cursor
        cursors[scope] = {'source_id':scope[0],'account_scope':scope[1],'cursor':cursor,
                          'coverage':sorted(threads),'committed_revision':revision,'updated_at':batch['read_at']}
        receipts.append({'source_id':scope[0],'account_scope':scope[1], 'read_at':batch['read_at'],
                         'read_receipt':copy.deepcopy(batch['read_receipt']), 'coverage':sorted(threads),
                         'page_count':len(pages),'complete':True,'committed_revision':revision})
    follow.update(processed_message_ids=processed,feedback_events=events,feedback_read_receipts=receipts,
                  feedback_review_items=reviews,source_cursors=[cursors[key] for key in sorted(cursors)])
    return covered, set(sources)


def prepare(case, request, root, now=None):
    """Pure validation and state derivation; persistence belongs to commit()."""
    now = _now(now)
    root = private_root(root)
    _object(case, 'case')
    validate_case_budget(case)
    validate_case_budget(request)
    _fields(request, {'version','expected_revision','run'}, {'records','transition','feedback_batches'})
    if request['version'] != VERSION:
        raise ValueError('Unsupported host request version')
    identifier(case.get('case_id'), 'case_id')
    identifier(case.get('brand_id'), 'brand_id')
    revision = _revision(case.get('revision'))
    if _revision(request['expected_revision']) != revision:
        raise ValueError('Revision compare-and-swap conflict')
    run = request['run']
    _fields(run, {'id','kind','scheduler_id','started_at','completed_at','source_read_results','errors'})
    identifier(run['id'], 'run.id')
    if run['id'] in _rows(case.get('runs', []), 'runs'):
        raise ValueError('Run ID has already been committed')
    if run['kind'] not in {'manual_trial','scheduled','manual_control'}:
        raise ValueError('Unsupported run kind')
    started = _time(run['started_at'], 'run.started_at', now)
    completed = _time(run['completed_at'], 'run.completed_at', now)
    if completed < started:
        raise ValueError('Run ends before it starts')
    if not isinstance(run['errors'], list) or any(not isinstance(value,str) for value in run['errors']):
        raise ValueError('run.errors must be a list of strings')
    updated = copy.deepcopy(case)
    config = _transition(case, updated, request.get('transition'), now)
    batches, replacements = request.get('feedback_batches', []), request.get('records', {})
    if (batches or replacements) and config['status'] != 'active':
        raise ValueError('Local work requires an active adapter; use an explicit transition')
    _records(case, updated, replacements, root, now)
    covered, expected_sources = _feedback(case, updated, batches, root, now, revision + 1, completed, replacements)
    results = run['source_read_results']
    if not isinstance(results, list):
        raise ValueError('source_read_results must be a list')
    observed, all_complete = set(), True
    for row in results:
        _fields(row, {'source_id','account_scope','status'})
        scope = (row['source_id'], row['account_scope'])
        if scope not in expected_sources or scope in observed or row['status'] not in {'complete','partial','failed'}:
            raise ValueError('Invalid or duplicate source read result')
        observed.add(scope)
        if (row['status'] == 'complete') != (scope in covered):
            raise ValueError('Complete source reads require complete scoped feedback receipts')
        all_complete = all_complete and row['status'] == 'complete'
    if not covered.issubset(observed) or ((run['errors'] or not all_complete) and covered):
        raise ValueError('Failed or incomplete runs cannot advance feedback cursors')
    if run['kind'] == 'scheduled':
        scheduler = _object(case.get('follow_up', {}).get('scheduler', {}), 'scheduler')
        if not scheduler.get('automation_id') or run['scheduler_id'] != scheduler['automation_id']:
            raise ValueError('Scheduled run does not match the configured scheduler')
    elif run['scheduler_id'] is not None:
        raise ValueError('Manual runs must not claim a scheduler identity')
    stored_run = copy.deepcopy(run)
    stored_run.update(status='completed',record_revision_before=revision,record_revision_after=revision + 1,
                      persisted_at=now.isoformat(),host_adapter_version=VERSION,
                      source_coverage_complete=bool(expected_sources) and covered == expected_sources,
                      success=bool(expected_sources) and covered == expected_sources and all_complete and not run['errors'])
    updated.setdefault('runs', []).append(stored_run)
    updated['revision'] = revision + 1
    validate_case_budget(updated)
    return updated


def _atomic_write(path, value):
    encoded = (json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False, indent=2) + '\n').encode('ascii')
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError('Committed case exceeds the JSON file budget')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(encoded).hexdigest()


def commit(directory, case_file, request_file, now=None):
    root = private_root(directory)
    case_path, request_path = _file(root, case_file), _file(root, request_file)
    if case_path == request_path:
        raise ValueError('Case and request must be separate files')
    with case_lock(case_path):
        updated = prepare(_load(case_path), _load(request_path), root, now)
        expected_hash = _atomic_write(case_path, updated)
        try:
            saved = _load(case_path)
            with case_path.open('rb') as handle:
                observed_hash = hashlib.sha256(handle.read(MAX_FILE_BYTES + 1)).hexdigest()
            if saved != updated or observed_hash != expected_hash:
                raise ValueError('Committed bytes differ from the requested state')
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise CommitUncertain('Replacement succeeded but readback is unverified; inspect before retrying') from exc
    return {'state':'committed','read_only':False,'persisted_verified':True,
            'revision':updated['revision'],'case_sha256':expected_hash,'authorizes_submission':False}


def health(case, now=None):
    now = _now(now)
    _object(case, 'case')
    report = {'state':'unverified','read_only':True,'authorizes_submission':False,
              'connector_access':'unverified','independent_observer':'unverified'}
    validate_case_budget(case)
    config = _config(case)
    report['adapter_status'] = config['status']
    follow = _object(case.get('follow_up', {}), 'follow_up')
    reviews = _rows(follow.get('feedback_review_items', []), 'feedback_review_items')
    report['open_feedback_reviews'] = sum(row.get('status') == 'open' for row in reviews.values())
    scheduler = _object(follow.get('scheduler', {}), 'scheduler')
    report['scheduler_status'] = scheduler.get('status', 'not_configured')
    observer = scheduler.get('health_observer', {})
    if isinstance(observer, dict):
        report['observer_mode'] = observer.get('mode', 'none')
    if (config['status'] != 'active' or follow.get('requested') is not True or follow.get('enabled') is not True
            or scheduler.get('status') not in {'configured','active_unverified','active_verified','degraded'}
            or not scheduler.get('automation_id') or not scheduler.get('kind')
            or not scheduler.get('configuration_verified_at')):
        return report
    _time(scheduler['configuration_verified_at'], 'scheduler.configuration_verified_at', now)
    interval = _interval(config)
    successes, unsuccessful = [], []
    sources = _sources(case)
    expected = set(sources)
    receipts = follow.get('feedback_read_receipts', [])
    if not isinstance(receipts, list) or any(not isinstance(value,dict) for value in receipts):
        raise ValueError('Invalid saved feedback receipts')
    for row in _rows(case.get('runs', []), 'runs').values():
        if row.get('kind') != 'scheduled' or row.get('scheduler_id') != scheduler['automation_id']:
            continue
        try:
            completed = _time(row.get('completed_at'), 'run.completed_at', now)
        except (ValueError, TypeError):
            report['reason'] = 'invalid_or_future_scheduled_run'
            return report
        if (row.get('host_adapter_version') != VERSION or row.get('status') != 'completed'
                or row.get('success') is not True or row.get('source_coverage_complete') is not True
                or row.get('errors') != []):
            unsuccessful.append(completed)
            continue
        try:
            started = _time(row.get('started_at'), 'run.started_at', completed)
            persisted = _time(row.get('persisted_at'), 'run.persisted_at', now)
            after = _revision(row.get('record_revision_after'))
            before = _revision(row.get('record_revision_before'))
            results = row.get('source_read_results')
            actual = {(value['source_id'], value['account_scope']) for value in results
                      if isinstance(value,dict) and value.get('status') == 'complete'} if isinstance(results,list) else set()
            selected_receipts = [value for value in receipts
                                 if value.get('committed_revision') == after and value.get('complete') is True]
            receipt_scopes = {(value.get('source_id'), value.get('account_scope')) for value in selected_receipts}
            coverage_valid = all(set(references(value, 'coverage')) == sources[(value['source_id'],value['account_scope'])][1]
                                 for value in selected_receipts)
            observed = min([completed] + [_time(value.get('read_at'), 'receipt.read_at', completed)
                                          for value in selected_receipts])
            if (started <= completed <= persisted and after == before + 1 and after <= _revision(case.get('revision'))
                    and expected and actual == expected and len(results) == len(expected)
                    and receipt_scopes == expected and len(selected_receipts) == len(expected) and coverage_valid):
                successes.append((observed,completed,after,persisted))
            else:
                unsuccessful.append(completed)
        except (ValueError, TypeError, KeyError):
            unsuccessful.append(completed)
            continue
    if successes:
        latest, completed, _, persisted = max(successes)
        report['last_successful_write_at'] = max(row[3] for row in successes).isoformat()
        report['last_successful_run_completed_at'] = completed.isoformat()
        report['freshness_observed_at'] = latest.isoformat()
        if unsuccessful and max(unsuccessful) >= latest:
            report['reason'] = 'latest_scheduled_run_was_not_successful'
        elif scheduler.get('status') == 'degraded':
            report['state'] = 'overdue'
            report['reason'] = 'scheduler_reports_degraded'
        else:
            report['state'] = 'healthy' if now <= latest + timedelta(hours=interval) else 'overdue'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('status','validate','commit'), default='status')
    parser.add_argument('--private-root', required=True, type=Path)
    parser.add_argument('--case', required=True, type=Path)
    parser.add_argument('--request', type=Path)
    args = parser.parse_args()
    try:
        root = private_root(args.private_root)
        path = _file(root, args.case)
        if args.command == 'status':
            result = health(_load(path))
        elif not args.request:
            raise ValueError('validate and commit require an explicit --request')
        elif args.command == 'commit':
            result = commit(root, path, args.request)
        else:
            prepared = prepare(_load(path), _load(_file(root, args.request)), root)
            result = {'state':'valid_request','read_only':True,'would_write_revision':prepared['revision'],
                      'authorizes_submission':False}
    except CommitUncertain as exc:
        result = {'state':'commit_uncertain','read_only':False,'persisted_verified':False,
                  'errors':[str(exc)],'authorizes_submission':False}
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        result = {'state':'rejected','read_only':True,'errors':[str(exc)],'authorizes_submission':False}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 1 if result.get('errors') else 0


if __name__ == '__main__':
    sys.exit(main())
