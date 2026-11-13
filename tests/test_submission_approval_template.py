"""The distributed approval template must never preapprove a private case."""
import json
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))


class ApprovalTemplate(unittest.TestCase):
    def test_all_approval_fields_are_blank_or_false(self):
        template = json.loads((SCRIPTS.parent/'assets/submission-approval.json').read_text(encoding='utf-8'))
        self.assertEqual(template['version'],'1.0')
        self.assertEqual(len(template['checks']),7)
        self.assertTrue(all(value is False for value in template['checks'].values()))
        for field in ('case_id','action_id','snapshot_sha256','reviewed_by','authorization_source','approved_at','expires_at'):
            self.assertIsNone(template[field])


if __name__ == '__main__': unittest.main()
