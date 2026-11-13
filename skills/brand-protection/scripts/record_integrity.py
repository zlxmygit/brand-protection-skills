"""Shared, bounded reference checks. Missing legacy labels are not ownership proof."""
from collections import deque

MAX_RECORDS = 10000
MAX_DEPENDENCIES = 1000


def identifier(value, label):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in
                   '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069' for c in value)):
        raise ValueError(label + ': expected a bounded identifier')
    return value


def references(record, field):
    """Only omission means no references; invalid explicit values are rejected."""
    value = record.get(field, [])
    if not isinstance(value, list) or len(value) > MAX_RECORDS:
        raise ValueError(field + ': expected a bounded ID list')
    for key in value:
        identifier(key, field)
    if len(value) != len(set(value)):
        raise ValueError(field + ': duplicate references')
    return value


def record_targets(record):
    targets = None
    if 'target_id' in record:
        targets = {identifier(record['target_id'], 'target_id')}
    if 'target_ids' in record:
        listed = references(record, 'target_ids')
        if not listed:
            raise ValueError('Explicit target_ids must not be empty')
        if targets is not None and targets != set(listed):
            raise ValueError('Conflicting singular and plural target scopes')
        targets = set(listed)
    return targets


def check_identity(case, record, label, scope=None):
    if not isinstance(record, dict):
        raise ValueError(label + ': expected an object')
    for field in ('case_id', 'brand_id'):
        if field in record:
            value = identifier(record[field], label + '.' + field)
            if value != case.get(field):
                raise ValueError(label + ': different or unconfirmed ' + field)
    targets = record_targets(record)
    if targets is not None and scope is not None and not targets.issubset(set(scope)):
        raise ValueError(label + ': explicit target is outside the reviewed scope')


def evidence_closure(case, evidence, seeds, scope=None):
    """Validate every selected item and parent, with iterative cycle detection."""
    selected, done, visiting = set(), set(), set()
    stack = [(key, False) for key in reversed(list(seeds))]
    while stack:
        key, exit_node = stack.pop()
        identifier(key, 'evidence reference')
        if exit_node:
            visiting.remove(key)
            done.add(key)
            continue
        if key in visiting:
            raise ValueError('Cyclic evidence lineage')
        if key in done:
            continue
        if key not in evidence:
            raise ValueError('Unknown or quarantined evidence reference')
        if len(selected) >= MAX_DEPENDENCIES:
            raise ValueError('Evidence dependency limit exceeded')
        row = evidence[key]
        check_identity(case, row, 'evidence ' + key, scope)
        selected.add(key)
        visiting.add(key)
        stack.append((key, True))
        parent = row.get('derived_from_id')
        if parent is not None:
            stack.append((identifier(parent, 'derived_from_id'), False))
    return sorted(selected)


def provider_binding(case, provider, scope, evidence, checked_at, parse_time):
    """Require explicit attribution for each target; never infer single-target scope."""
    scope = set(scope)
    check_identity(case, provider, 'provider')
    declared = record_targets(provider)
    rows = provider.get('target_attributions')
    if rows is None:
        if declared is None or len(declared) != 1 or not scope.issubset(declared):
            raise ValueError('Provider needs explicit attribution for every action target')
        rows = [{'target_id': next(iter(declared)),
                 'evidence_ids': references(provider, 'attribution_evidence_ids'),
                 'verified_at': provider.get('verified_at')}]
    elif not isinstance(rows, list) or not rows or len(rows) > 100:
        raise ValueError('target_attributions must be a non-empty bounded list')
    covered, selected = set(), set()
    for row in rows:
        check_identity(case, row, 'provider attribution')
        target = identifier(row.get('target_id'), 'attribution.target_id')
        if target in covered:
            raise ValueError('Duplicate provider target attribution')
        covered.add(target)
        if target not in scope:
            continue
        keys = references(row, 'evidence_ids')
        if not keys:
            raise ValueError('Provider attribution requires evidence per target')
        if parse_time(row.get('verified_at'), 'attribution.verified_at') > checked_at:
            raise ValueError('Target attribution changed after channel review')
        selected.update(evidence_closure(case, evidence, keys, {target}))
    if not scope.issubset(covered):
        raise ValueError('Provider attribution does not cover the action scope')
    if declared is not None and (not scope.issubset(declared) or not covered.issubset(declared)):
        raise ValueError('Provider attribution conflicts with its explicit scope')
    # Also validate the provider's legacy/top-level references; do not let the
    # new mapping hide a contradictory reference already recorded there.
    selected.update(evidence_closure(case, evidence, references(provider, 'attribution_evidence_ids'),
                                     declared or covered))
    return sorted(selected)


def correction_family(actions, action_id):
    """O(V+E) validation and component traversal; no recursive ancestor walks."""
    if not isinstance(actions, dict) or len(actions) > MAX_RECORDS or action_id not in actions:
        raise ValueError('Invalid or oversized action graph')
    graph = {key: [] for key in actions}
    indegree = {key: 0 for key in actions}
    parents = {}
    for key, row in actions.items():
        identifier(key, 'action_id')
        if not isinstance(row, dict):
            raise ValueError('Action must be an object')
        parent = row.get('corrects_action_id')
        if parent is None:
            continue
        identifier(parent, 'corrects_action_id')
        if parent not in actions or parent == key:
            raise ValueError('Invalid correction reference')
        parents[key] = parent
        indegree[parent] += 1
        graph[key].append(parent)
        graph[parent].append(key)
    ready = deque(key for key in actions if indegree[key] == 0)
    visited = 0
    while ready:
        key = ready.popleft()
        visited += 1
        if key in parents:
            parent = parents[key]
            indegree[parent] -= 1
            if indegree[parent] == 0:
                ready.append(parent)
    if visited != len(actions):
        raise ValueError('Cyclic correction reference')
    found, pending = set(), [action_id]
    while pending:
        key = pending.pop()
        if key not in found:
            found.add(key)
            pending.extend(graph[key])
    return found
