"""Synthetic channel scope checks; never submits a Cloudflare report."""
import copy
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('channel_check', SCRIPTS/'channel_check.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026,11,13,4,tzinfo=timezone.utc)


def fixture():
    profile = m.read_json(m.PROFILE)
    return {'case_id':'synthetic', 'brand_id':'b1',
            'targets':[{'id':'t1','supplied_url':'https://suspected.example/item'}],
            'parties':[{'id':'p1'}],
            'rights':[{'id':'r1','type':'copyright','brand_id':'b1','holder_party_id':'p1',
                       'specific_work':'Synthetic product photograph','original_reference':'Original private file reference'}],
            'evidence':[{'id':'e1'}],
            'providers':[{'id':'cf','organization':'Cloudflare','role':'reverse_proxy',
                          'verified_at':'2026-11-13T02:00:00Z','attribution_evidence_ids':['e1']}],
            'actions':[{'id':'a1','provider_id':'cf','target_ids':['t1'],'channel_review':{
                'profile_id':profile['id'], 'policy_url':profile['policy_url'],'report_url':profile['report_url'],
                'checked_at':'2026-11-13T03:00:00Z','review_before':'2026-11-13T05:00:00Z',
                'reviewed_by':'synthetic reviewer','attribution_verified':True,'service_role':'reverse_proxy',
                'requested_action':'forward_report','confirmations':{key:True for key in profile['required_confirmations']},
                'work_mappings':[{'target_id':'t1','right_id':'r1','reported_url':'https://suspected.example/item',
                                  'location_description':'Product photograph beside the title','comparison_evidence_ids':['e1']}]}}]}


class ChannelContracts(unittest.TestCase):
    def setUp(self):
        self.case = fixture()
        self.review = self.case['actions'][0]['channel_review']

    def check(self): return m.check(self.case,'a1',NOW)

    def blocked(self):
        result = self.check()
        self.assertEqual(result['state'],'needs_review')
        self.assertTrue(result['errors'])
        self.assertFalse(result['authorizes_submission'])

    def test_proxy_reports_are_forwarding_only_and_readonly(self):
        before = copy.deepcopy(self.case)
        result = self.check()
        self.assertEqual(result['state'],'checklist_ready_for_review')
        self.assertEqual(result['requested_action'],'forward_report')
        self.assertFalse(result['authorizes_submission'])
        self.assertEqual(self.case,before)

    def test_proxy_cannot_request_origin_deletion(self):
        self.review['requested_action'] = 'hosted_removal_review'
        self.blocked()

    def test_verified_hosting_role_supports_review_not_guarantee(self):
        self.case['providers'][0]['role'] = 'hosting'
        self.review.update(service_role='hosting',requested_action='hosted_removal_review')
        self.assertEqual(self.check()['state'],'checklist_ready_for_review')

    def test_unknown_registrar_and_mismatched_roles_block(self):
        for role in ('unknown','registrar',None,[], 'hosting'):
            self.review['service_role'] = role
            self.blocked()

    def test_policy_and_form_urls_cannot_be_swapped(self):
        for field in ('policy_url','report_url'):
            old = self.review[field]
            self.review[field] = 'https://other.example/collect'
            self.blocked()
            self.review[field] = old

    def test_policy_expiry_future_date_and_invalid_timezone_block(self):
        for field,value in [('review_before','2026-11-13T04:00:00Z'),('checked_at','2026-11-14T00:00:00Z'),('checked_at','2026-11-13T03:00:00')]:
            old = self.review[field]
            self.review[field] = value
            self.blocked()
            self.review[field] = old

    def test_attribution_requires_known_evidence_and_explicit_review(self):
        self.review['attribution_verified'] = 'true'
        self.blocked()
        self.review['attribution_verified'] = True
        self.case['providers'][0]['attribution_evidence_ids'] = ['missing']
        self.blocked()

    def test_changed_attribution_requires_new_channel_review(self):
        self.case['providers'][0]['verified_at'] = '2026-11-13T03:30:00Z'
        self.blocked()

    def test_trademark_and_other_brand_are_not_copyright(self):
        self.case['rights'][0]['type'] = 'trademark'
        self.blocked()
        self.case['rights'][0]['type'] = 'copyright'
        self.case['rights'][0]['brand_id'] = 'other'
        self.blocked()

    def test_work_identity_holder_and_location_are_required(self):
        for field in ('specific_work','original_reference','holder_party_id'):
            old = self.case['rights'][0][field]
            self.case['rights'][0][field] = None
            self.blocked()
            self.case['rights'][0][field] = old
        self.review['work_mappings'][0]['location_description'] = ''
        self.blocked()

    def test_exact_target_page_not_unrecorded_domain_or_other_target(self):
        for url in ('https://suspected.example/','https://other.example/item','file:///tmp/x','https://u:p@suspected.example/item'):
            self.review['work_mappings'][0]['reported_url'] = url
            self.blocked()

    def test_comparison_ids_are_required_and_same_brand(self):
        self.review['work_mappings'][0]['comparison_evidence_ids'] = []
        self.blocked()
        self.review['work_mappings'][0]['comparison_evidence_ids'] = ['e1']
        self.case['evidence'][0]['brand_id'] = 'other'
        self.blocked()

    def test_each_scope_target_needs_a_mapping(self):
        self.case['targets'].append({'id':'t2','supplied_url':'https://suspected.example/other'})
        self.case['actions'][0]['target_ids'].append('t2')
        self.blocked()

    def test_duplicate_mapping_or_record_is_rejected(self):
        self.review['work_mappings'] *= 2
        self.blocked()
        self.review['work_mappings'] = self.review['work_mappings'][:1]
        self.case['evidence'].append({'id':'e1'})
        self.blocked()

    def test_all_confirmation_values_are_actual_booleans(self):
        field = next(iter(self.review['confirmations']))
        for value in (None,False,1,'true'):
            self.review['confirmations'][field] = value
            self.blocked()

    def test_missing_profile_and_wrong_provider_do_not_default(self):
        self.review['profile_id'] = 'other-profile'
        self.blocked()
        self.review['profile_id'] = 'cloudflare-copyright-v1'
        self.case['providers'][0]['organization'] = 'Other Host'
        self.blocked()

    def test_cache_removal_and_counter_notice_are_out_of_scope(self):
        for action in ('cache_removal','counter_notice'):
            self.review['requested_action'] = action
            self.blocked()

    def test_types_do_not_crash_or_grant_submission(self):
        for field in ('service_role','work_mappings','confirmations','checked_at','attribution_verified'):
            original = copy.deepcopy(self.review[field])
            for value in (None,False,0,{},[]):
                self.review[field] = value
                result = self.check()
                self.assertFalse(result['authorizes_submission'])
                self.assertTrue(result['errors'])
            self.review[field] = original


if __name__ == '__main__': unittest.main()
