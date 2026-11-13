"""Use the actual shipping queue, not a simulated queue, in full CI/install tests."""
import copy
import unittest
import test_case_dashboard as d


class DashboardQueueIntegration(unittest.TestCase):
    def case(self):
        return {'schema_version':'1.3','case_id':'synthetic',
                'follow_up':{'requested':True,'enabled':True},
                'targets':[{'id':'t1','status':'open','channel_plan':[{'provider_id':'p1','route':'email'}]}],
                'providers':[{'id':'p1'}],
                'actions':[{'id':'a1','status':'acknowledged','target_ids':['t1'],'provider_id':'p1','route':'email'}],
                'followup_tasks':[{'id':'read','action_id':'a1','kind':'check_feedback','status':'planned',
                                   'due_at':'2026-11-13T03:00:00Z'}],
                'outbound_attempts':[],'runs':[],
                'deadlines':[{'id':'deadline','action_id':'a1','status':'open','due_at':'2026-11-13T05:00:00Z'}]}

    def test_real_queue_and_dashboard_counts_match(self):
        case=self.case();before=copy.deepcopy(case)
        actual=d.m.run_queue(case,d.NOW)
        view=d.m.project(case,d.NOW)
        for key in d.m.QUEUES:
            self.assertEqual(view['queue_counts'][key],len(actual[key]))
        self.assertEqual(view['queues']['due_tasks'][0]['id'],'read')
        self.assertEqual(case,before)

    def test_real_disabled_queue_preserves_independent_deadline(self):
        case=self.case();case['follow_up']['enabled']=False
        view=d.m.project(case,d.NOW)
        self.assertEqual(view['queue_state'],'not_enabled')
        self.assertEqual(view['deadlines'][0]['id'],'deadline')

    def test_real_frozen_action_exposes_blocker_and_coverage_gap(self):
        case=self.case();case['actions'][0]['claim_frozen']=True
        case['followup_tasks'][0].update(kind='remind',dedupe_key='synthetic-remind')
        view=d.m.project(case,d.NOW)
        self.assertEqual(view['queue_counts']['due_tasks'],0)
        self.assertGreater(view['queue_counts']['held_tasks'],0)
        self.assertGreater(view['queue_counts']['coverage_gaps'],0)


if __name__ == '__main__':unittest.main()
