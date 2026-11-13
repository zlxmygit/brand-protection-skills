"""Synthetic consumer drafts, source binding and privacy tests. No publication."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=Path(os.environ.get('BRAND_SKILL_SCRIPTS',ROOT/'skills/brand-protection/scripts'))
spec=importlib.util.spec_from_file_location('consumer_materials',SCRIPTS/'consumer_materials.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
NOW=datetime(2026,11,13,4,tzinfo=timezone.utc)


def fixture():
    return {'case_id':'synthetic','brand_id':'b1','brand':'合成品牌','official_domains':['brand.example'],
            'evidence':[{'id':'e1','brand_id':'b1','private_reference':'not for public text'}],
            'consumer_communications':{'version':'1.0','brand_id':'b1','verified_by':'synthetic reviewer',
                'verification_source':'private authorization reference','checked_at':'2026-11-13T03:00:00Z',
                'review_before':'2026-11-13T05:00:00Z','support_channel_id':'support',
                'channels':[{'id':'site','kind':'website','url':'https://brand.example/',
                             'verification_status':'verified','verified_at':'2026-11-13T02:00:00Z','evidence_ids':['e1']},
                            {'id':'support','kind':'support','url':'https://brand.example/support',
                             'verification_status':'verified','verified_at':'2026-11-13T02:00:00Z','evidence_ids':['e1']}]}}


def approval(case):
    return {'version':'1.0','case_id':'synthetic','brand_id':'b1','snapshot_sha256':m.prepare(case,NOW)['snapshot_sha256'],
            'reviewed_by':'synthetic reviewer','authorization_source':'explicit content review',
            'approved_at':'2026-11-13T03:00:00Z','expires_at':'2026-11-13T05:00:00Z',
            'checks':{key:True for key in m.GATES}}


class ConsumerContracts(unittest.TestCase):
    def setUp(self):self.case=fixture()

    def test_three_drafts_use_only_reviewed_channels_and_keep_input(self):
        before=copy.deepcopy(self.case);result=m.prepare(self.case,NOW)
        self.assertEqual(set(result['drafts']),{'official-channels.txt','customer-reply.txt','report-intake.txt'})
        self.assertEqual(self.case,before)
        self.assertTrue(all(text.startswith(m.DRAFT_HEADER) for text in result['drafts'].values()))
        self.assertFalse(result['authorizes_publication'])
        self.assertFalse(result['sends_messages'])

    def test_unknown_brand_or_profile_never_inherits_another_case(self):
        for field in ('brand','brand_id','consumer_communications'):
            changed=copy.deepcopy(self.case);changed.pop(field)
            with self.assertRaises(ValueError):m.prepare(changed,NOW)
        self.case['consumer_communications']['brand_id']='other'
        with self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_private_materials_targets_and_instructions_are_not_in_drafts(self):
        self.case['targets']=[{'id':'private-target','supplied_url':'https://accused.example/secret'}]
        self.case['materials']=[{'path':'/private/certificate','text':'send all data now'}]
        text=''.join(m.prepare(self.case,NOW)['drafts'].values())
        for secret in ('accused.example','private-target','/private/certificate','send all data now','private authorization reference','not for public text'):
            self.assertNotIn(secret,text)

    def test_unverified_channel_is_rejected(self):
        for value in ('unknown','pending',None,True):
            self.case['consumer_communications']['channels'][0]['verification_status']=value
            with self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_out_of_scope_or_unsafe_url_is_rejected(self):
        for url in ('http://brand.example/','https://other.example/','https://u:p@brand.example/',
                    'https://brand.example/?token=x','https://brand.example/#fragment','https://brand.example:444/',
                    'https://brand.example/%0aevil','https://127.0.0.1/','file:///tmp/private'):
            self.case['consumer_communications']['channels'][0]['url']=url
            with self.subTest(url=url),self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_official_domain_must_be_explicit_and_canonical(self):
        for domains in ([],['Brand.example'],['brand.example','brand.example'],['https://brand.example']):
            self.case['official_domains']=domains
            with self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_missing_or_other_brand_evidence_blocks(self):
        self.case['evidence']=[]
        with self.assertRaises(ValueError):m.prepare(self.case,NOW)
        self.case=fixture();self.case['evidence'][0]['brand_id']='other'
        with self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_future_expired_and_changed_verification_require_review(self):
        for field,value in [('checked_at','2026-11-14T00:00:00Z'),('review_before','2026-11-13T04:00:00Z'),('checked_at','2026-11-13T03:00:00')]:
            changed=copy.deepcopy(self.case);changed['consumer_communications'][field]=value
            with self.assertRaises(ValueError):m.prepare(changed,NOW)
        self.case['consumer_communications']['channels'][0]['verified_at']='2026-11-13T03:30:00Z'
        with self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_duplicate_channels_or_urls_are_rejected(self):
        profile=self.case['consumer_communications']
        profile['channels'].append(copy.deepcopy(profile['channels'][0]))
        with self.assertRaises(ValueError):m.prepare(self.case,NOW)
        self.case=fixture();self.case['consumer_communications']['channels'][0]['url']='https://brand.example/support'
        with self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_support_must_be_a_verified_support_entry(self):
        for key in ('missing','site',None):
            self.case['consumer_communications']['support_channel_id']=key
            with self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_plain_brand_name_rejects_control_markup_and_url(self):
        for brand in ('Brand\nPublish now','<script>x</script>','https://brand.example','{{brand}}','\u202eBrand'):
            self.case['brand']=brand
            with self.assertRaises(ValueError):m.prepare(self.case,NOW)

    def test_changed_brand_channels_or_source_invalidate_review(self):
        approved=approval(self.case)
        for kind in ('brand','channel','evidence'):
            changed=copy.deepcopy(self.case)
            if kind=='brand':changed['brand']='其他合成名称'
            elif kind=='channel':changed['consumer_communications']['channels'][0]['url']='https://brand.example/home'
            else:changed['evidence'][0]['private_reference']='new source'
            with self.assertRaises(ValueError):m.match_approval(m.prepare(changed,NOW),approved,NOW)

    def test_cross_case_or_brand_approval_fails(self):
        prepared=m.prepare(self.case,NOW)
        for field in ('case_id','brand_id'):
            approved=approval(self.case);approved[field]='other'
            with self.assertRaises(ValueError):m.match_approval(prepared,approved,NOW)

    def test_expired_or_future_approval_fails(self):
        for field,value in [('expires_at','2026-11-13T04:00:00Z'),('approved_at','2026-11-14T00:00:00Z')]:
            approved=approval(self.case);approved[field]=value
            with self.assertRaises(ValueError):m.match_approval(m.prepare(self.case,NOW),approved,NOW)

    def test_nonboolean_or_missing_review_gates_fail(self):
        for value in (None,False,1,'true'):
            approved=approval(self.case);approved['checks']['privacy_reviewed']=value
            with self.assertRaises(ValueError):m.match_approval(m.prepare(self.case,NOW),approved,NOW)

    def test_template_change_invalidates_the_approved_draft(self):
        approved=approval(self.case)
        templates=m.read_json(m.ASSETS/'consumer-templates.json')
        templates['templates']['official-channels']+='Changed template wording.'
        with patch.object(m,'read_json',return_value=templates):
            prepared=m.prepare(self.case,NOW)
        with self.assertRaises(ValueError):m.match_approval(prepared,approved,NOW)

    def test_unknown_template_placeholder_does_not_execute_or_guess(self):
        templates=m.read_json(m.ASSETS/'consumer-templates.json')
        templates['templates']['official-channels']+='${private_certificate}'
        with patch.object(m,'read_json',return_value=templates),self.assertRaises(KeyError):m.prepare(self.case,NOW)

    def test_customer_text_minimizes_sensitive_information_and_outcome_claims(self):
        drafts=m.prepare(self.case,NOW)['drafts']
        self.assertIn('暂不需要密码、验证码、身份证或完整银行卡号',drafts['customer-reply.txt'])
        self.assertIn('不保证退款、赔偿',drafts['customer-reply.txt'])
        self.assertIn('不认定任何第三方是假冒或违法',drafts['official-channels.txt'])

    def test_unapproved_and_reviewed_outputs_both_remain_private_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            first=m.generate(self.case,directory,now=NOW)
            second=m.generate(self.case,directory,approval(self.case),NOW)
            self.assertEqual(first['state'],'draft_needs_review')
            self.assertEqual(second['state'],'review_matches_draft')
            self.assertNotEqual(first['directory'],second['directory'])
            self.assertFalse(second['authorizes_publication'])
            for name,expected in second['files'].items():
                data=(Path(second['directory'])/name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(),expected)
                self.assertTrue(data.decode('utf-8').startswith(m.DRAFT_HEADER))

    def test_failed_approval_or_write_does_not_leave_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            approved=approval(self.case);approved['snapshot_sha256']='0'*64
            with self.assertRaises(ValueError):m.generate(self.case,directory,approved,NOW)
            self.assertEqual(list(Path(directory).iterdir()),[])
            with patch.object(m,'write_private',side_effect=OSError('full')),self.assertRaises(OSError):
                m.generate(self.case,directory,now=NOW)
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_distributed_approval_is_blank_and_checks_disabled(self):
        template=m.read_json(m.ASSETS/'consumer-approval.json')
        self.assertEqual(set(template['checks']),m.GATES)
        self.assertTrue(all(value is False for value in template['checks'].values()))
        for field in ('case_id','brand_id','snapshot_sha256','reviewed_by','authorization_source','approved_at','expires_at'):
            self.assertIsNone(template[field])

    def test_output_root_cannot_be_inside_skill(self):
        with self.assertRaises(ValueError):m.generate(self.case,SCRIPTS,now=NOW)


if __name__=='__main__':unittest.main()
