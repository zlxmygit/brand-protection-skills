"""Resolve approval inputs to verified local bytes, not just reference labels."""
import hashlib

from record_integrity import (MAX_DEPENDENCIES, MAX_RECORDS, check_identity,
                              identifier, references, record_targets, review_right_scope)

TOTAL_BYTES = 50 * 1024 * 1024


def scoped_rows(case, section, scope, index):
    selected = []
    for row in index(case, section).values():
        targets = record_targets(row)
        # Omitted scope is legacy case-wide context, never an excuse to discard
        # possible counter-evidence. Explicit other-target context is unrelated.
        if targets is None or targets.intersection(scope):
            check_identity(case, row, section)
            selected.append(row)
    return sorted(selected, key=lambda row: row['id'])


def resolve(case, action, targets, evidence, providers, root, index, read_file):
    """Return selected records and byte hashes; fail closed on any referenced gap."""
    scope = set(references(action, 'target_ids'))
    check_identity(case, action, 'action', scope)
    all_rights, all_parties = index(case, 'rights'), index(case, 'parties')
    materials = index(case, 'materials')
    if any(len(rows) > MAX_RECORDS for rows in (evidence, materials, all_rights, all_parties)):
        raise ValueError('Record dependency index limit exceeded')
    right_ids = review_right_scope(case, action, all_rights)
    if not right_ids:
        raise ValueError('Record the rights relied upon by this action')
    rights, party_ids = {}, set()
    for key in right_ids:
        if key not in all_rights:
            raise ValueError('Unknown right in action scope')
        row = all_rights[key]
        check_identity(case, row, 'right ' + key)
        if row.get('brand_id') != case.get('brand_id'):
            raise ValueError('Right belongs to another or unconfirmed brand')
        party_ids.add(identifier(row.get('holder_party_id'), 'holder_party_id'))
        rights[key] = row
    if 'authority_party_ids' in action:
        party_ids.update(references(action, 'authority_party_ids'))
    else:
        # The legacy record stores authority as prose, so retain the conservative
        # all-party scope until an operator explicitly records authority parties.
        party_ids.update(all_parties)
    parties = {}
    for key in sorted(party_ids):
        if key not in all_parties:
            raise ValueError('Unknown rights holder or authority party')
        check_identity(case, all_parties[key], 'party ' + key)
        parties[key] = all_parties[key]
    allegations = scoped_rows(case, 'allegations', scope, index)
    observations = scoped_rows(case, 'observations', scope, index)
    provider = providers[action['provider_id']]
    check_identity(case, provider, 'provider')
    declared = record_targets(provider)
    if declared is not None and not scope.issubset(declared):
        raise ValueError('Provider target does not cover this action')
    selected_targets = []
    for key in sorted(scope):
        check_identity(case, targets[key], 'target ' + key)
        selected_targets.append(targets[key])

    evidence_ids, material_ids = set(), set()
    def collect(row):
        if not isinstance(row, dict):
            raise ValueError('Dependency context must be an object')
        for field in ('evidence_ids', 'basis_evidence_ids', 'counter_evidence_ids',
                      'authorization_evidence_ids', 'attribution_evidence_ids',
                      'comparison_evidence_ids', 'relationship_evidence_ids', 'claim_evidence_ids'):
            evidence_ids.update(references(row, field))
        for field in ('material_ids', 'authorization_material_ids'):
            material_ids.update(references(row, field))
    context = [action, provider] + selected_targets + list(rights.values()) + list(parties.values()) + allegations + observations
    for row in context:
        collect(row)
    evidence_ids.update(references(action['preflight_payload'], 'attachment_ids'))
    for right in rights.values():
        for field in ('authority_verification', 'registry_verification'):
            value = right.get(field)
            if isinstance(value, dict):
                collect(value)
    for row in provider.get('target_attributions', []):
        if not isinstance(row, dict):
            raise ValueError('Invalid target attribution')
        if row.get('target_id') in scope:
            collect(row)
    channel = action.get('channel_review')
    if isinstance(channel, dict):
        for row in channel.get('work_mappings', []):
            collect(row)
    authorizations = case.get('follow_up', {}).get('authorizations', {}) if isinstance(case.get('follow_up', {}), dict) else {}
    if isinstance(authorizations, dict):
        for value in authorizations.values():
            if isinstance(value, dict):
                collect(value)

    # Cross-namespace references and derived copies form one bounded graph.
    # Process each node once; validate parent chains separately for cycles.
    pending = [('evidence', key) for key in sorted(evidence_ids)] + [('materials', key) for key in sorted(material_ids)]
    records, edges, verified = {}, {}, []
    total = 0
    while pending:
        section, key = pending.pop()
        node = (section, identifier(key, 'dependency reference'))
        if node in records:
            continue
        if len(records) >= MAX_DEPENDENCIES:
            raise ValueError('Approval dependency limit exceeded')
        source = evidence if section == 'evidence' else materials
        if key not in source:
            raise ValueError('Missing ' + section + ' dependency: ' + key)
        row = source[key]
        check_identity(case, row, section + ' ' + key, scope)
        path = row.get('original_path') if section == 'evidence' else row.get('source_path')
        data = read_file(root, path, row.get('sha256'))
        total += len(data)
        if total > TOTAL_BYTES:
            raise ValueError('Approval dependencies exceed 50 MiB')
        verified.append({'section': section, 'id': key, 'size': len(data),
                         'sha256': hashlib.sha256(data).hexdigest()})
        records[node] = row
        links = []
        parent = row.get('derived_from_id')
        if parent is not None:
            links.append((section, identifier(parent, 'derived_from_id')))
        for field in ('evidence_ids', 'basis_evidence_ids', 'counter_evidence_ids', 'authorization_evidence_ids'):
            links.extend(('evidence', ref) for ref in references(row, field))
        for field in ('material_ids', 'authorization_material_ids'):
            links.extend(('materials', ref) for ref in references(row, field))
        edges[node] = links
        pending.extend(links)
    # Explicit iterative DFS also catches mixed material/evidence cycles.
    done, active = set(), set()
    for node in records:
        stack = [(node, False)]
        while stack:
            current, exit_node = stack.pop()
            if exit_node:
                active.remove(current)
                done.add(current)
            elif current in active:
                raise ValueError('Cyclic approval dependency')
            elif current not in done:
                active.add(current)
                stack.append((current, True))
                stack.extend((child, False) for child in edges[current])
    return {'rights': [rights[key] for key in sorted(rights)],
            'parties': [parties[key] for key in sorted(parties)],
            'allegations': allegations, 'observations': observations,
            'evidence': [records[node] for node in sorted(records) if node[0] == 'evidence'],
            'materials': [records[node] for node in sorted(records) if node[0] == 'materials'],
            'verified_files': sorted(verified, key=lambda row: (row['section'], row['id']))}
