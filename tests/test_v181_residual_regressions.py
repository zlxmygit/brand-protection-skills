"""B01/B02 counterexamples, using real production modules and synthetic files.

Ten assertions: seven failures and three controls on v1.8.1.
No infrastructure replacements, network calls, live records or publication.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
sys.path.insert(0, str(SCRIPTS))
import submission_review as s
import channel_check as c
import consumer_materials as m

NOW = datetime(2026, 11, 13, 4, tzinfo=timezone.utc)
START = '2026-11-13T03:00:00Z'
END = '2026-11-13T05:00:00Z'


def consumer_case():
    return {'case_id':'synthetic-A','brand_id':'synthetic-brand-A','brand':'Synthetic Brand',
      'official_domains':['official.example'],
      'evidence':[{'id':'e1','brand_id':'synthetic-brand-A','case_id':'synthetic-A','sha256':'1'*64}],
      'consumer_communications':{'version':'1.0','brand_id':'synthetic-brand-A',
        'checked_at':START,'review_before':END,'verified_by':'synthetic reviewer',
        'verification_source':'synthetic recorded proof','support_channel_id':'support',
        'channels':[{'id':'support','kind':'support','url':'https://official.example/support',
          'verification_status':'verified','verified_at':START,'evidence_ids':['e1']}]}}


def approval_for(prepared):
    return {'version':'1.0','case_id':prepared['case_id'],'brand_id':prepared['brand_id'],
      'snapshot_sha256':prepared['snapshot_sha256'],'reviewed_by':'synthetic reviewer',
      'authorization_source':'synthetic approval','approved_at':START,'expires_at':END,
      'checks':{k:True for k in m.GATES}}


def write(root, name, body):
    (root/name).write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def rights_case(root):
    profile = c.read_json(c.PROFILE)
    ehash = write(root,'e.bin',b'Synthetic site evidence')
    bhash = write(root,'body.txt',b'Synthetic final notice about mapped work R2.')
    m1hash = write(root,'m1.bin',b'Proof for work R1')
    m2hash = write(root,'m2.bin',b'Proof for work R2')
    return {'case_id':'synthetic-A','brand_id':'synthetic-brand-A',
      'parties':[{'id':'p1','legal_name_original':'Synthetic Rights Holder'}],
      'rights':[{'id':'r1','brand_id':'synthetic-brand-A','holder_party_id':'p1','type':'copyright',
                 'specific_work':'Work R1','original_reference':'private original R1','material_ids':['m1']},
                {'id':'r2','brand_id':'synthetic-brand-A','holder_party_id':'p1','type':'copyright',
                 'specific_work':'Work R2','original_reference':'private original R2','material_ids':['m2']}],
      'materials':[{'id':'m1','source_path':'m1.bin','sha256':m1hash},
                   {'id':'m2','source_path':'m2.bin','sha256':m2hash}],
      'targets':[{'id':'t1','supplied_url':'https://suspected.example/item'}],
      'providers':[{'id':'cf','organization':'Cloudflare','role':'reverse_proxy','target_id':'t1',
        'verified_at':'2026-11-13T02:00:00Z','attribution_evidence_ids':['e1']}],
      'evidence':[{'id':'e1','original_path':'e.bin','sha256':ehash}],
      'followup_tasks':[],'outbound_attempts':[],
      'actions':[{'id':'a1','status':'ready_for_review','target_ids':['t1'],'right_ids':['r1'],
        'provider_id':'cf','route':'copyright_form','legal_or_policy_basis':'synthetic policy',
        'requested_remedy':'forward_report','authorization_scope_and_source':'synthetic authority',
        'signature_authority':'synthetic signer','claim_evidence_ids':['e1'],
        'preflight_payload':{'version':'1.0','operation':'initial_notice','recipient':'synthetic verified intake',
          'dedupe_key':'synthetic-round-1','body':{'path':'body.txt','sha256':bhash},
          'attachment_ids':['e1'],'fees':{'amount':'0.00','currency':'USD'}},
        'channel_review':{'profile_id':profile['id'],'policy_url':profile['policy_url'],
          'report_url':profile['report_url'],'checked_at':START,'review_before':END,
          'reviewed_by':'synthetic reviewer','attribution_verified':True,'service_role':'reverse_proxy',
          'requested_action':'forward_report','confirmations':{k:True for k in profile['required_confirmations']},
          'work_mappings':[{'target_id':'t1','right_id':'r2','reported_url':'https://suspected.example/item',
            'location_description':'Illustration R2 beside title','comparison_evidence_ids':['e1']}]}}]}


def submission_approval(case, root):
    return {'version':'1.0','case_id':case['case_id'],'action_id':'a1',
      'snapshot_sha256':s.snapshot(case,'a1',root)['sha256'],
      'reviewed_by':'synthetic reviewer','authorization_source':'synthetic authority',
      'approved_at':START,'expires_at':END,'checks':{k:True for k in s.APPROVAL_GATES}}


class ResidualContracts(unittest.TestCase):
    def test_B01_consumer_cross_case_evidence_rejected(self):
        case = consumer_case()
        case['evidence'][0]['case_id'] = 'synthetic-other-case'
        with self.assertRaises(ValueError):
            m.prepare(case, NOW)

    def test_B01_consumer_cross_brand_parent_rejected(self):
        case = consumer_case()
        case['evidence'][0]['derived_from_id'] = 'parent'
        case['evidence'].append({'id':'parent','brand_id':'synthetic-other-brand','sha256':'2'*64})
        with self.assertRaises(ValueError):
            m.prepare(case, NOW)

    def test_B01_consumer_missing_parent_rejected(self):
        case = consumer_case()
        case['evidence'][0]['derived_from_id'] = 'missing'
        with self.assertRaises(ValueError):
            m.prepare(case, NOW)

    def test_B01_parent_change_invalidates_existing_review(self):
        case = consumer_case()
        case['evidence'][0]['derived_from_id'] = 'parent'
        case['evidence'].append({'id':'parent','brand_id':case['brand_id'],'sha256':'2'*64})
        approved = approval_for(m.prepare(case, NOW))
        case['evidence'][1]['sha256'] = '3'*64
        with self.assertRaises(ValueError):
            m.match_approval(m.prepare(case, NOW), approved, NOW)

    def test_B02_channel_mapping_must_obey_action_right_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = rights_case(Path(tmp))
            self.assertEqual(c.check(case,'a1',NOW)['state'], 'needs_review')

    def test_B02_mapped_material_change_invalidates_old_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = rights_case(root)
            approved = submission_approval(case, root)
            case['materials'][1]['sha256'] = write(root,'m2.bin',b'Modified proof for mapped work R2')
            self.assertEqual(s.review(case,'a1',approved,root,NOW)['state'], 'needs_review')

    def test_B02_mapped_material_missing_blocks_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = rights_case(root)
            approved = submission_approval(case, root)
            (root/'m2.bin').unlink()
            self.assertEqual(s.review(case,'a1',approved,root,NOW)['state'], 'needs_review')

    def test_control_direct_other_brand_is_rejected(self):
        case = consumer_case()
        case['evidence'][0]['brand_id'] = 'synthetic-other-brand'
        with self.assertRaises(ValueError):
            m.prepare(case, NOW)

    def test_control_direct_evidence_change_invalidates_review(self):
        case = consumer_case()
        approved = approval_for(m.prepare(case, NOW))
        case['evidence'][0]['sha256'] = '4'*64
        with self.assertRaises(ValueError):
            m.match_approval(m.prepare(case, NOW), approved, NOW)

    def test_control_matching_right_scope_binds_changed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = rights_case(root)
            case['actions'][0]['right_ids'] = ['r2']
            self.assertEqual(c.check(case,'a1',NOW)['state'], 'checklist_ready_for_review')
            approved = submission_approval(case, root)
            self.assertEqual(s.review(case,'a1',approved,root,NOW)['state'], 'approval_matches_snapshot')
            case['materials'][1]['sha256'] = write(root,'m2.bin',b'Modified relevant bytes')
            self.assertEqual(s.review(case,'a1',approved,root,NOW)['state'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
