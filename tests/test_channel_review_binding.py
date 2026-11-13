"""A channel review becomes part of the approved submission snapshot."""
import copy
from pathlib import Path
import tempfile
import unittest
import test_submission_review as s


class ChannelBinding(unittest.TestCase):
    def test_channel_review_change_invalidates_prior_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, approval = s.fixture(root)
            before = s.m.snapshot(case,'a1',root)['sha256']
            self.assertEqual(before,approval['snapshot_sha256'])
            case['actions'][0]['channel_review'] = {'profile_id':'cloudflare-copyright-v1','requested_action':'forward_report'}
            self.assertEqual(s.m.review(case,'a1',approval,root,s.NOW)['state'],'needs_review')
            approval['snapshot_sha256'] = s.m.snapshot(case,'a1',root)['sha256']
            self.assertEqual(s.m.review(case,'a1',approval,root,s.NOW)['state'],'approval_matches_snapshot')
            case['actions'][0]['channel_review']['requested_action'] = 'hosted_removal_review'
            self.assertEqual(s.m.review(case,'a1',approval,root,s.NOW)['state'],'needs_review')


if __name__ == '__main__': unittest.main()
