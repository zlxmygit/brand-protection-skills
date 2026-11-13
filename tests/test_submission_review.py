"""Synthetic approval matching and material-mutation contracts."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('submission_review', SCRIPTS/'submission_review.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026,11,13,4,tzinfo=timezone.utc)


def fixture(root):
    body, evidence = b'Synthetic final notice for an identified work.', b'Synthetic claim evidence'
    (root/'body.txt').write_bytes(body)
    (root/'evidence.bin').write_bytes(evidence)
    case = {'case_id':'synthetic', 'brand_id':'brand-1', 'markets':['US'],
            'rights':[{'id':'right-1', 'brand_id':'brand-1', 'holder_party_id':'party-1', 'type':'copyright'}],
            'parties':[{'id':'party-1', 'legal_name_original':'Synthetic Rights Holder'}],
            'targets':[{'id':'target-1','supplied_url':'https://suspected.example/item'}],
            'providers':[{'id':'provider-1','role':'host','organization':'Synthetic Host'}],
            'evidence':[{'id':'e1','original_path':'evidence.bin','sha256':hashlib.sha256(evidence).hexdigest()}],
            'followup_tasks':[], 'outbound_attempts':[],
            'actions':[{'id':'a1','status':'ready_for_review','target_ids':['target-1'],
                        'provider_id':'provider-1','route':'copyright_form','legal_or_policy_basis':'verified policy',
                        'requested_remedy':'remove specified material','authorization_scope_and_source':'synthetic user approval scope',
                        'signature_authority':'synthetic signer source','claim_evidence_ids':['e1'],
                        'preflight_payload':{'version':'1.0','operation':'initial_notice','recipient':'verified provider intake',
                            'dedupe_key':'synthetic-a1-first','body':{'path':'body.txt','sha256':hashlib.sha256(body).hexdigest()},
                            'attachment_ids':['e1'],'fees':{'amount':'0.00','currency':'USD'}}}]}
    approval = {'version':'1.0','case_id':'synthetic','action_id':'a1','reviewed_by':'synthetic reviewer',
                'authorization_source':'synthetic instruction', 'approved_at':'2026-11-13T03:00:00Z',
                'expires_at':'2026-11-13T05:00:00Z','checks':{key:True for key in m.APPROVAL_GATES}}
    approval['snapshot_sha256'] = m.snapshot(case, 'a1', root)['sha256']
    return case, approval


class SubmissionReviewContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.case, self.approval = fixture(self.root)

    def tearDown(self): self.directory.cleanup()

    def check(self): return m.review(self.case, 'a1', self.approval, self.root, NOW)

    def blocked(self):
        result = self.check()
        self.assertEqual(result['state'], 'needs_review')
        self.assertTrue(result['errors'])
        self.assertFalse(result['authorizes_submission'])

    def test_matching_review_is_not_a_send_permit(self):
        before = copy.deepcopy((self.case,self.approval))
        result = self.check()
        self.assertEqual(result['state'],'approval_matches_snapshot')
        self.assertFalse(result['authorizes_submission'])
        self.assertEqual((self.case,self.approval),before)

    def test_without_approval_returns_fingerprint_for_human_review(self):
        result = m.review(self.case,'a1',None,self.root,NOW)
        self.assertIn('snapshot',result)
        self.assertTrue(result['errors'])

    def test_body_changes_invalidate_approval_even_after_record_hash_update(self):
        (self.root/'body.txt').write_bytes(b'Changed final notice.')
        self.blocked()
        self.case['actions'][0]['preflight_payload']['body']['sha256'] = hashlib.sha256(b'Changed final notice.').hexdigest()
        self.blocked()

    def test_attachment_changes_invalidate_approval(self):
        (self.root/'evidence.bin').write_bytes(b'changed evidence')
        self.case['evidence'][0]['sha256'] = hashlib.sha256(b'changed evidence').hexdigest()
        self.blocked()

    def test_recipient_fees_operation_and_dedupe_changes_invalidate_approval(self):
        for field, value in [('recipient','different recipient'),('fees',{'amount':'100','currency':'EUR'}),
                             ('operation','resubmit'),('dedupe_key','different round')]:
            original = copy.deepcopy(self.case)
            self.case['actions'][0]['preflight_payload'][field] = value
            self.blocked()
            self.case = original

    def test_target_and_legal_scope_changes_invalidate_approval(self):
        for changes in ('target','basis','remedy','authority','right'):
            original = copy.deepcopy(self.case)
            if changes == 'target': self.case['targets'][0]['supplied_url'] = 'https://other.example/'
            elif changes == 'right': self.case['rights'][0]['territory'] = 'other'
            else:
                field = {'basis':'legal_or_policy_basis','remedy':'requested_remedy','authority':'authorization_scope_and_source'}[changes]
                self.case['actions'][0][field] = 'changed'
            self.blocked()
            self.case = original

    def test_missing_false_and_nonboolean_gate_fail(self):
        for value in (False,None,1,'true'):
            self.approval['checks']['rights_checked'] = value
            self.blocked()
        self.approval['checks'].pop('rights_checked')
        self.blocked()

    def test_expired_future_and_naive_approvals_fail(self):
        for field, value in [('expires_at','2026-11-13T04:00:00Z'),('approved_at','2026-11-14T00:00:00Z'),('approved_at','2026-11-13T03:00:00')]:
            original = copy.deepcopy(self.approval)
            self.approval[field] = value
            self.blocked()
            self.approval = original

    def test_cross_case_and_action_approval_fail(self):
        for field in ('case_id','action_id'):
            original = self.approval[field]
            self.approval[field] = 'other'
            self.blocked()
            self.approval[field] = original

    def test_placeholder_or_empty_body_is_not_final(self):
        for body in (b'', b'{{work}}', b'Notice [PLACEHOLDER]', b'bad\0body'):
            (self.root/'body.txt').write_bytes(body)
            self.case['actions'][0]['preflight_payload']['body']['sha256'] = hashlib.sha256(body).hexdigest()
            self.blocked()

    def test_unknown_targets_and_evidence_fail(self):
        for field in ('target_ids','claim_evidence_ids'):
            original = self.case['actions'][0][field]
            self.case['actions'][0][field] = ['missing']
            self.blocked()
            self.case['actions'][0][field] = original

    def test_other_brand_right_or_evidence_fails(self):
        self.case['rights'][0]['brand_id'] = 'other'
        self.blocked()
        self.case['rights'][0]['brand_id'] = 'brand-1'
        self.case['evidence'][0]['brand_id'] = 'other'
        self.blocked()

    def test_frozen_terminal_or_invalid_status_blocks_matching_approval(self):
        for status in ('withdrawn','verified_effective','draft','unknown'):
            self.case['actions'][0]['status'] = status
            self.blocked()
        self.case['actions'][0]['status'] = 'authorized'
        for value in (True,'true',0,None):
            self.case['actions'][0]['claim_frozen'] = value
            self.blocked()

    def test_state_transition_alone_does_not_change_material_fingerprint(self):
        self.case['actions'][0]['status'] = 'authorized'
        self.assertEqual(self.check()['state'],'approval_matches_snapshot')

    def test_related_pending_delivery_blocks_but_unrelated_does_not(self):
        self.case['outbound_attempts'] = [{'id':'attempt-1','action_id':'a1','status':'outcome_unknown','dedupe_key':'old'}]
        self.blocked()
        self.case['actions'].append({'id':'unrelated'})
        self.case['outbound_attempts'][0]['action_id'] = 'unrelated'
        self.assertEqual(self.check()['state'],'approval_matches_snapshot')

    def test_matching_sent_key_blocks(self):
        self.case['outbound_attempts'] = [{'id':'attempt-1','action_id':'a1','status':'sent','dedupe_key':'synthetic-a1-first'}]
        self.blocked()

    def test_running_related_task_blocks_without_attempt(self):
        self.case['followup_tasks'] = [{'id':'running','action_id':'a1','status':'in_progress','kind':'remind'}]
        self.blocked()

    def test_correction_family_delivery_barrier_and_closed_original(self):
        self.case['actions'].append({'id':'original','status':'withdrawn','claim_frozen':True})
        self.case['actions'][0]['corrects_action_id'] = 'original'
        self.case['actions'][0]['preflight_payload']['operation'] = 'send_correction'
        self.approval['snapshot_sha256'] = m.snapshot(self.case,'a1',self.root)['sha256']
        self.assertEqual(self.check()['state'],'approval_matches_snapshot')
        self.case['outbound_attempts'] = [{'id':'attempt-1','action_id':'original','status':'prepared','dedupe_key':'original'}]
        self.blocked()

    def test_correction_cycle_is_rejected(self):
        self.case['actions'].append({'id':'original','corrects_action_id':'a1'})
        self.case['actions'][0]['corrects_action_id'] = 'original'
        self.blocked()

    def test_unknown_delivery_state_and_duplicate_ids_fail_closed(self):
        self.case['outbound_attempts'] = [{'id':'x','action_id':'a1','status':'maybe_sent','dedupe_key':'x'}]
        self.blocked()
        self.case['outbound_attempts'] = []
        self.case['evidence'].append(copy.deepcopy(self.case['evidence'][0]))
        self.blocked()

    def test_outbound_restriction_cannot_be_overridden_by_approval(self):
        self.case['actions'][0]['followup'] = {'all_outbound_not_before':'2026-11-14T00:00:00Z'}
        self.blocked()
        self.case['actions'][0]['followup']['all_outbound_not_before'] = 0
        self.blocked()

    def test_new_counter_evidence_context_invalidates_prior_approval(self):
        self.case['allegations'] = [{'id':'claim-1','counter_evidence_ids':['new-counter-evidence']}]
        self.blocked()

    def test_changed_case_authorizations_invalidate_prior_approval(self):
        self.case['follow_up'] = {'authorizations': {'same_ticket_followup': 'revoked'}}
        self.blocked()

    def test_unknown_payload_fields_and_missing_fees_are_rejected(self):
        self.case['actions'][0]['preflight_payload']['cc'] = 'other recipient'
        self.blocked()
        self.case['actions'][0]['preflight_payload'].pop('cc')
        self.case['actions'][0]['preflight_payload']['fees'] = None
        self.blocked()


if __name__ == '__main__': unittest.main()
