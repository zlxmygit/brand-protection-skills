#!/usr/bin/env python3
"""Match a recorded approval to exact local materials. Never sends or grants authority."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import text, timestamp, ids, index_rows, read_json
from evidence_export import digest, read_evidence
from capture_web import private_root
from record_integrity import correction_family
from submission_dependencies import resolve as resolve_dependencies

OPERATIONS = {'initial_notice', 'remind', 'resubmit', 'send_supplement', 'send_escalation', 'send_correction'}
PENDING = {'prepared', 'in_flight', 'outcome_unknown'}
APPROVAL_GATES = {'rights_checked', 'counter_evidence_checked', 'recipient_verified',
                  'disclosure_reviewed', 'signature_authority_checked', 'fees_checked', 'latest_feedback_checked'}
REVIEWABLE = {'ready_for_review', 'authorized', 'submitted', 'acknowledged', 'needs_information', 'appealed', 'correction_pending'}


def indexed(case, section):
    errors = []
    result = index_rows(case, section, errors)
    if errors:
        raise ValueError(section + ': repair record IDs first')
    return result


def family(actions, action_id):
    return correction_family(actions, action_id)


def snapshot(case, action_id, source_root):
    if not isinstance(case, dict):
        raise ValueError('Case must be an object')
    case_id = text(case.get('case_id'), 'case_id', 200)
    text(case.get('brand_id'), 'brand_id', 200)
    actions, targets, evidence, providers = (indexed(case, field) for field in ('actions','targets','evidence','providers'))
    text(action_id, 'action_id', 200)
    if action_id not in actions:
        raise ValueError('Unknown action')
    action = actions[action_id]
    family(actions, action_id)
    target_ids = ids(action.get('target_ids'), 'target_ids', allow_empty=False)
    if any(key not in targets for key in target_ids):
        raise ValueError('Unknown target in action')
    provider_id = text(action.get('provider_id'), 'provider_id', 200)
    if provider_id not in providers:
        raise ValueError('Unknown provider')
    for field in ('route', 'legal_or_policy_basis', 'requested_remedy', 'authorization_scope_and_source', 'signature_authority'):
        text(action.get(field), field)
    claim_ids = ids(action.get('claim_evidence_ids'), 'claim_evidence_ids', allow_empty=False)
    payload = action.get('preflight_payload')
    if not isinstance(payload, dict) or payload.get('version') != '1.0':
        raise ValueError('Expected preflight_payload version 1.0')
    required = {'version','operation','recipient','dedupe_key','body','attachment_ids','fees'}
    if set(payload) != required:
        raise ValueError('Unexpected or missing payload fields')
    operation = text(payload.get('operation'), 'operation', 200)
    if operation not in OPERATIONS:
        raise ValueError('Unsupported operation')
    if operation == 'send_correction' and not action.get('corrects_action_id'):
        raise ValueError('Correction needs its own linked action')
    text(payload.get('recipient'), 'recipient')
    text(payload.get('dedupe_key'), 'dedupe_key', 200)
    fees = payload.get('fees')
    if (not isinstance(fees, dict) or set(fees) != {'amount','currency'}
            or not isinstance(fees.get('amount'), str) or not re.fullmatch(r'\d{1,12}(?:\.\d{1,2})?', fees['amount'])
            or not isinstance(fees.get('currency'), str) or not re.fullmatch(r'[A-Z]{3}', fees['currency'])):
        raise ValueError('Fees must explicitly state amount and currency, including zero')
    if action.get('fees') is not None and action['fees'] != fees:
        raise ValueError('Action fees and payload fees differ')
    root = private_root(source_root)
    body = payload.get('body')
    if not isinstance(body, dict) or set(body) != {'path','sha256'}:
        raise ValueError('Body requires an exact path and hash')
    body_bytes = read_evidence(root, body.get('path'), digest(body.get('sha256')))
    content = body_bytes.decode('utf-8-sig')
    if not content.strip() or '\x00' in content or '{{' in content or '}}' in content or '[PLACEHOLDER]' in content:
        raise ValueError('Body is empty or contains unresolved template markers')
    attachment_ids = ids(payload.get('attachment_ids'), 'attachment_ids')
    try:
        dependencies = resolve_dependencies(case, action, targets, evidence, providers, root, indexed, read_evidence)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        # No approvable fingerprint is issued for an incomplete dependency set.
        return {'sha256': None, 'complete': False, 'errors': [str(exc)]}
    material = {
        'case_id': case_id, 'brand_id': case['brand_id'], 'markets': case.get('markets', []),
        'action_id': action_id, 'target_ids': target_ids, 'targets': [targets[key] for key in sorted(target_ids)],
        'snapshot_version': '1.1', 'provider': providers[provider_id],
        'rights': dependencies['rights'], 'parties': dependencies['parties'],
        'materials': dependencies['materials'], 'verified_files': dependencies['verified_files'],
        'route': action['route'], 'basis': action['legal_or_policy_basis'], 'remedy': action['requested_remedy'],
        'authority': action['authorization_scope_and_source'], 'signature_authority': action['signature_authority'],
        'corrects_action_id': action.get('corrects_action_id'), 'claim_evidence_ids': claim_ids,
        'evidence': dependencies['evidence'], 'payload': payload,
        'allegations': dependencies['allegations'], 'observations': dependencies['observations'],
        'follow_up_authorizations': case.get('follow_up', {}).get('authorizations') if isinstance(case.get('follow_up', {}), dict) else case.get('follow_up'),
    }
    if 'channel_review' in action:
        material['channel_review'] = action['channel_review']
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    return {'complete': True, 'snapshot_version': '1.1', 'sha256': hashlib.sha256(encoded).hexdigest(), 'body_sha256': hashlib.sha256(body_bytes).hexdigest(),
            'attachment_count': len(attachment_ids), 'operation': operation, 'dedupe_key': payload['dedupe_key']}


def check_state(case, action_id, material, now):
    actions = indexed(case, 'actions')
    action = actions[action_id]
    related = family(actions, action_id)
    if action.get('status') not in REVIEWABLE:
        raise ValueError('Action is not in a reviewable state')
    if 'claim_frozen' in action and type(action['claim_frozen']) is not bool:
        raise ValueError('claim_frozen must be a boolean')
    if action.get('claim_frozen') is True:
        raise ValueError('Claim is frozen')
    follow = action.get('followup', {})
    if not isinstance(follow, dict):
        raise ValueError('Invalid action followup')
    fields = ['all_outbound_not_before']
    if material['operation'] in {'remind','resubmit'}:
        fields.append('cooldown_until')
    if material['operation'] == 'remind':
        fields.append('expected_response_due_at')
    for field in fields:
        if field in follow and follow[field] is not None and timestamp(follow[field], field) > now:
            raise ValueError('Recorded outbound restriction is not yet satisfied')
    tasks = indexed(case, 'followup_tasks')
    for task in tasks.values():
        if task.get('action_id') in related and task.get('status') == 'in_progress':
            raise ValueError('Related work is still in progress; reconcile before external action')
    for attempt in indexed(case, 'outbound_attempts').values():
        if not isinstance(attempt.get('action_id'), str) or attempt['action_id'] not in actions:
            raise ValueError('Unknown action in send history')
        state = attempt.get('status')
        if not isinstance(state, str) or state not in PENDING | {'sent','confirmed_not_sent'}:
            raise ValueError('Unknown delivery state')
        key = text(attempt.get('dedupe_key'), 'attempt dedupe_key', 200)
        if attempt['action_id'] in related and state in PENDING:
            raise ValueError('Related delivery is unresolved')
        if key == material['dedupe_key'] and state in PENDING | {'sent'}:
            raise ValueError('Matching send is already recorded or unresolved')


def review(case, action_id, approval, source_root, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    report = {'read_only': True, 'authorizes_submission': False, 'state': 'needs_review',
              'errors': [], 'warnings': ['Recorded approval is not identity authentication, legal advice or a send permit',
                                       'Recheck live feedback, queue readiness and exclusive execution before any external action']}
    try:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('now requires timezone')
        now = now.astimezone(timezone.utc)
        material = snapshot(case, action_id, source_root)
        report['snapshot'] = material
        if material.get('complete') is not True:
            raise ValueError('Incomplete approval dependencies: ' + '; '.join(material.get('errors', [])))
        check_state(case, action_id, material, now)
        if not isinstance(approval, dict) or approval.get('version') != '1.0':
            raise ValueError('A recorded version 1.0 approval is required')
        if approval.get('case_id') != case['case_id'] or approval.get('action_id') != action_id:
            raise ValueError('Approval belongs to another case or action')
        if digest(approval.get('snapshot_sha256')) != material['sha256']:
            raise ValueError('Approval does not match the current materials and scope')
        for field in ('reviewed_by','authorization_source'):
            text(approval.get(field), field)
        approved = timestamp(approval.get('approved_at'), 'approved_at')
        expires = timestamp(approval.get('expires_at'), 'expires_at')
        if not approved <= now < expires:
            raise ValueError('Approval is future-dated or expired')
        gates = approval.get('checks')
        if not isinstance(gates, dict) or set(gates) != APPROVAL_GATES or any(value is not True for value in gates.values()):
            raise ValueError('All required review checks must be explicitly confirmed')
        report['state'] = 'approval_matches_snapshot'
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        report['errors'].append(str(exc))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, type=Path)
    parser.add_argument('--action-id', required=True)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--source-root', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = review(read_json(args.case), args.action_id,
                        read_json(args.approval) if args.approval else None, args.source_root)
    except (OSError, ValueError, TypeError, RecursionError):
        result = {'state': 'needs_review', 'errors': ['input_unavailable_or_invalid'], 'authorizes_submission': False}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
