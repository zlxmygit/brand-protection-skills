#!/usr/bin/env python3
"""Read-only checks for one Cloudflare copyright channel profile. Never submits."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from investigation import text, timestamp, ids, read_json
from submission_review import indexed
from record_integrity import check_identity, evidence_closure, provider_binding

PROFILE = Path(__file__).resolve().parents[1] / 'assets/channels/cloudflare-copyright.json'


def specific_url(value):
    text(value, 'reported_url', 8192)
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or '\\' in value):
        raise ValueError('A specific credential-free page URL is required')
    return value


def check(case, action_id, now=None):
    report = {'read_only': True, 'authorizes_submission': False, 'state': 'needs_review',
              'errors': [], 'checklist': [], 'warnings': [
                  'Recorded checks are not proof of actual rights, authority or provider attribution',
                  'Policy and form must be checked again before an authorized submission']}
    try:
        if not isinstance(case, dict):
            raise ValueError('Case must be an object')
        now = datetime.now(timezone.utc) if now is None else now
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('now requires timezone')
        now = now.astimezone(timezone.utc)
        profile = read_json(PROFILE)
        report['profile_id'] = profile['id']
        report['checklist'] = list(profile['required_notice_fields'])
        actions, targets, providers, evidence, rights, parties = (
            indexed(case, key) for key in ('actions', 'targets', 'providers', 'evidence', 'rights', 'parties'))
        text(action_id, 'action_id', 200)
        if action_id not in actions:
            raise ValueError('Unknown action')
        action = actions[action_id]
        check_identity(case, action, 'action')
        review = action.get('channel_review')
        if not isinstance(review, dict) or review.get('profile_id') != profile['id']:
            raise ValueError('Expected a recorded review for this channel profile')
        for field, expected in [('policy_url', profile['policy_url']), ('report_url', profile['report_url'])]:
            if review.get(field) != expected:
                raise ValueError('Review must reference the configured official ' + field)
        checked = timestamp(review.get('checked_at'), 'checked_at')
        expires = timestamp(review.get('review_before'), 'review_before')
        if not checked <= now < expires:
            raise ValueError('Channel review is future-dated or due for renewal')
        text(review.get('reviewed_by'), 'reviewed_by', 200)
        provider_id = text(action.get('provider_id'), 'provider_id', 200)
        if provider_id not in providers:
            raise ValueError('Unknown provider')
        provider = providers[provider_id]
        if not isinstance(provider.get('organization'), str) or provider['organization'].strip().casefold() != 'cloudflare':
            raise ValueError('Provider is not identified as Cloudflare')
        role = review.get('service_role')
        if not isinstance(role, str) or role not in profile['roles'] or provider.get('role') != role:
            raise ValueError('Confirm the actual proxy or hosting role before using this profile')
        verified = timestamp(provider.get('verified_at'), 'provider.verified_at')
        if verified > checked:
            raise ValueError('Provider attribution changed after channel review')
        attribution = ids(provider.get('attribution_evidence_ids'), 'attribution_evidence_ids', allow_empty=False)
        if any(key not in evidence for key in attribution) or review.get('attribution_verified') is not True:
            raise ValueError('Provider attribution requires reviewed evidence references')
        allowed = profile['roles'][role]
        if review.get('requested_action') not in allowed['requests']:
            raise ValueError('Requested action exceeds the configured service role')
        report['capability_boundary'] = allowed['boundary']
        confirmations = review.get('confirmations')
        required = set(profile['required_confirmations'])
        if not isinstance(confirmations, dict) or set(confirmations) != required or any(v is not True for v in confirmations.values()):
            raise ValueError('Complete each recorded form, disclosure and signer review')
        scope = ids(action.get('target_ids'), 'target_ids', allow_empty=False)
        if any(key not in targets for key in scope):
            raise ValueError('Unknown target in action scope')
        provider_binding(case, provider, scope, evidence, checked, timestamp)
        mappings = review.get('work_mappings')
        if not isinstance(mappings, list) or not 1 <= len(mappings) <= 100:
            raise ValueError('Specific work-to-page mappings are required')
        covered, seen = set(), set()
        brand_id = text(case.get('brand_id'), 'brand_id', 200)
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise ValueError('Each work mapping must be an object')
            target_id = text(mapping.get('target_id'), 'target_id', 200)
            right_id = text(mapping.get('right_id'), 'right_id', 200)
            url = specific_url(mapping.get('reported_url'))
            if target_id not in scope or right_id not in rights:
                raise ValueError('Work mapping is outside this action or case')
            target = targets[target_id]
            check_identity(case, target, 'target')
            check_identity(case, mapping, 'work mapping', {target_id})
            if url not in (target.get('supplied_url'), target.get('final_url')):
                raise ValueError('Reported URL must match an explicitly recorded target page')
            right = rights[right_id]
            if right.get('type') != 'copyright' or right.get('brand_id') != brand_id:
                raise ValueError('This profile requires same-brand copyright, not trademark or counterfeit labels')
            holder = text(right.get('holder_party_id'), 'holder_party_id', 200)
            if holder not in parties:
                raise ValueError('Record the rights holder in this case')
            text(right.get('specific_work'), 'specific_work')
            text(right.get('original_reference'), 'original_reference')
            text(mapping.get('location_description'), 'location_description')
            comparisons = ids(mapping.get('comparison_evidence_ids'), 'comparison_evidence_ids', allow_empty=False)
            if any(key not in evidence for key in comparisons):
                raise ValueError('Comparison evidence is missing from this case')
            evidence_closure(case, evidence, comparisons, {target_id})
            for key in comparisons:
                if evidence[key].get('brand_id', brand_id) != brand_id:
                    raise ValueError('Comparison evidence belongs to another brand')
            pair = (target_id, right_id, url)
            if pair in seen:
                raise ValueError('Duplicate work mapping')
            seen.add(pair)
            covered.add(target_id)
        if covered != set(scope):
            raise ValueError('Every target needs a specific work and location mapping')
        report.update(state='checklist_ready_for_review', mapped_targets=len(covered),
                      requested_action=review['requested_action'], service_role=role)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        report['errors'].append(str(exc))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, type=Path)
    parser.add_argument('--action-id', required=True)
    args = parser.parse_args()
    try:
        result = check(read_json(args.case), args.action_id)
    except (OSError, ValueError, TypeError, RecursionError):
        result = {'state': 'needs_review', 'authorizes_submission': False, 'errors': ['invalid_case_input']}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
