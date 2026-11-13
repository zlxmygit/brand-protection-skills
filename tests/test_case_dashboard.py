"""Offline projection/rendering tests; queue integration is verified separately."""
import copy
from datetime import datetime, timezone
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('case_dashboard', SCRIPTS/'case_dashboard.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026,11,13,4,tzinfo=timezone.utc)


def fixture():
    return {'case_id':'synthetic','targets':[{'id':'t1'}], 'actions':[{'id':'a1','status':'submitted'}],
            'deadlines':[{'id':'d1','action_id':'a1','due_at':'2026-11-13T03:00:00Z','status':'open'}]}


def queue(case, now):
    return {'state':'preview','errors':[], **{key:[] for key in m.QUEUES}}


class Parser(HTMLParser):
    def __init__(self):
        super().__init__();self.tags=[];self.attributes=[]
    def handle_starttag(self,tag,attrs):
        self.tags.append(tag);self.attributes.extend(attrs)


class DashboardContracts(unittest.TestCase):
    def test_projection_does_not_mutate_case_or_conflate_stages(self):
        case=fixture()
        for status in ('draft','acknowledged','action_reported','verified_effective'):
            case['actions'].append({'id':status,'status':status})
        before=copy.deepcopy(case)
        view=m.project(case,NOW,queue)
        self.assertEqual(view['stages'],{'action_reported':1,'acknowledged':1,'draft':1,'submitted':1,'verified_effective':1})
        self.assertEqual(case,before)
        self.assertFalse(view['authorizes_submission'])

    def test_queue_results_are_projected_not_recomputed(self):
        actual=queue({},NOW)
        actual['held_tasks']=[{'id':'busy','reason':'claim_frozen','secret_field':'must not copy'}]
        actual['due_tasks']=[{'id':'read','kind':'check_feedback'}]
        builder=Mock(return_value=actual)
        case=fixture();view=m.project(case,NOW,builder)
        builder.assert_called_once_with(case,NOW)
        self.assertEqual(view['queue_counts']['held_tasks'],1)
        self.assertEqual(view['queues']['held_tasks'],[{'id':'busy','reason':'claim_frozen'}])

    def test_disabled_queue_does_not_hide_recorded_deadline(self):
        report=queue({},NOW);report['state']='not_enabled'
        view=m.project(fixture(),NOW,lambda *a:report)
        self.assertEqual(view['deadlines'][0]['state'],'overdue')
        self.assertEqual(view['queue_state'],'not_enabled')

    def test_missing_deadline_date_is_visible_unknown(self):
        case=fixture();case['deadlines'][0]['due_at']=None
        view=m.project(case,NOW,queue)
        self.assertEqual(view['deadlines'][0]['state'],'unknown')
        self.assertTrue(view['errors'])

    def test_closed_deadlines_are_not_current_open_work(self):
        case=fixture();case['deadlines'][0]['status']='resolved'
        self.assertEqual(m.project(case,NOW,queue)['deadlines'],[])

    def test_unknown_action_status_is_not_treated_as_finished(self):
        case=fixture();case['actions'][0]['status']={'bad':True}
        view=m.project(case,NOW,queue)
        self.assertEqual(view['stages'],{'unknown':1})
        self.assertTrue(view['errors'])

    def test_duplicate_records_quarantined_without_hiding_deadline(self):
        case=fixture();case['actions']*=2
        view=m.project(case,NOW,queue)
        self.assertEqual(view['action_count'],0)
        self.assertTrue(view['errors'])
        self.assertEqual(len(view['deadlines']),1)

    def test_closed_original_does_not_close_open_correction(self):
        case=fixture();case['actions'][0]['status']='withdrawn'
        case['actions'].append({'id':'c1','status':'correction_pending','corrects_action_id':'a1'})
        self.assertEqual(m.project(case,NOW,queue)['open_corrections'],1)

    def test_failed_or_missing_queue_is_unknown_not_zero_work(self):
        for builder in (Mock(side_effect=ValueError('private source path')),Mock(return_value={}),Mock(return_value=None)):
            view=m.project(fixture(),NOW,builder)
            self.assertEqual(view['queue_state'],'unavailable')
            self.assertTrue(all(value is None for value in view['queue_counts'].values()))
            self.assertNotIn('private source path',json.dumps(view))

    def test_bad_queue_section_is_reported(self):
        report=queue({},NOW);report['due_tasks']=[None]
        view=m.project(fixture(),NOW,lambda *a:report)
        self.assertIsNone(view['queue_counts']['due_tasks'])
        self.assertTrue(view['errors'])

    def test_private_content_and_credentials_are_not_projected(self):
        case=fixture();case['materials']=[{'path':'/private/secret-certificate'}]
        case['actions'][0].update(final_text_path='/private/notice',authorization_scope_and_source='secret authorization',
                                  preflight_payload={'recipient':'private@example.com'})
        html=m.render([m.project(case,NOW,queue)])
        for secret in ('secret-certificate','/private/notice','secret authorization','private@example.com'):
            self.assertNotIn(secret,html)

    def test_html_injection_is_escaped_and_never_active(self):
        case=fixture();case['actions'][0]['id']='<img src=x onerror=alert(1)>'
        report=queue({},NOW);report['held_tasks']=[{'id':'t','reason':'</pre><script>alert(1)</script>'}]
        html=m.render([m.project(case,NOW,lambda *a:report)])
        parsed=Parser();parsed.feed(html)
        self.assertNotIn('script',parsed.tags);self.assertNotIn('img',parsed.tags)
        self.assertIn('&lt;script&gt;',html)
        self.assertTrue(all(not name.startswith('on') for name,value in parsed.attributes))

    def test_static_page_has_no_links_forms_or_external_resources(self):
        html=m.render([m.project(fixture(),NOW,queue)])
        parsed=Parser();parsed.feed(html)
        for tag in ('script','iframe','form','a','img','link','button','input'):
            self.assertNotIn(tag,parsed.tags)
        self.assertIn('Content-Security-Policy',html)
        self.assertIn('sha256-',html)
        self.assertIn('no-referrer',html)

    def test_control_and_direction_characters_removed(self):
        self.assertEqual(m.label('x\u202ey\x00z\n'),'xyz')
        self.assertEqual(m.label({}), '未知')
        self.assertEqual(m.label('a'*400), 'a'*300+'…')

    def test_truncated_details_keep_counts_and_display_warning(self):
        case=fixture();case['actions']=[{'id':'a'+str(i),'status':'draft'} for i in range(5)]
        view=m.project(case,NOW,queue)
        html=m.render([view],row_limit=1)
        self.assertEqual(view['action_count'],5)
        self.assertIn('已截断',html)

    def test_multiple_cases_and_bad_input_are_not_merged_or_lost(self):
        first=m.project(fixture(),NOW,queue)
        second=copy.deepcopy(first)
        html=m.render([first,second,{'case_id':'输入 3','input_error':True}])
        self.assertIn('重复案件 ID',html)
        self.assertIn('无法读取或解析',html)

    def test_private_unique_output_preserves_existing_files(self):
        view=m.project(fixture(),NOW,queue)
        with tempfile.TemporaryDirectory() as directory:
            first=m.save_dashboard([view],directory);second=m.save_dashboard([view],directory)
            self.assertNotEqual(first['path'],second['path'])
            self.assertEqual(Path(first['path']).read_bytes(),Path(second['path']).read_bytes())
            self.assertTrue(first['private_report'])
            self.assertFalse(first['authorizes_submission'])

    def test_write_failure_cleans_only_new_report(self):
        with tempfile.TemporaryDirectory() as directory:
            old=Path(directory)/'existing';old.write_text('keep')
            with patch.object(m,'write_private',side_effect=OSError('full')),self.assertRaises(OSError):
                m.save_dashboard([m.project(fixture(),NOW,queue)],directory)
            self.assertEqual(list(Path(directory).iterdir()),[old])

    def test_invalid_row_limit_and_in_skill_destination_are_rejected(self):
        for limit in (0,1001,True):
            with self.assertRaises(ValueError):m.render([],limit)
        with self.assertRaises(ValueError):m.save_dashboard([],SCRIPTS)


if __name__ == '__main__':unittest.main()
