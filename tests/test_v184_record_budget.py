"""Independent resource limits for direct APIs as well as bounded JSON files."""
import importlib.util
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ.get('BRAND_SKILL_SCRIPTS', ROOT/'skills/brand-protection/scripts'))
spec = importlib.util.spec_from_file_location('v184_integrity', SCRIPTS/'record_integrity.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RecordBudgets(unittest.TestCase):
    def test_plain_profiles_and_small_cases_remain_valid(self):
        for value in ({'version':'1.0', 'requirements':['a','b']},
                      {'targets':[{'id':'t1'}], 'actions':[], 'right_ids':['r1']}):
            self.assertIsNone(m.validate_case_budget(value))

    def test_record_budget_is_global_across_sections(self):
        case = {'actions':[{} for _ in range(6000)], 'targets':[{} for _ in range(4001)]}
        with self.assertRaisesRegex(ValueError, 'record budget'):
            m.validate_case_budget(case)

    def test_record_boundary_is_accepted(self):
        self.assertIsNone(m.validate_case_budget({'actions':[{} for _ in range(m.MAX_RECORDS)]}))

    def test_reference_budget_is_global(self):
        with self.assertRaisesRegex(ValueError, 'reference budget'):
            m.validate_case_budget({'right_ids':['r']*30000, 'target_ids':['t']*20001})

    def test_deep_inputs_fail_without_recursion(self):
        value = 'leaf'
        for _ in range(m.MAX_DEPTH + 1):
            value = {'nested':value}
        with self.assertRaisesRegex(ValueError, 'structure budget'):
            m.validate_case_budget(value)

    def test_cycles_rejected_but_shared_immutable_inputs_allowed(self):
        value = {}; value['self'] = value
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            m.validate_case_budget(value)
        shared = {'id':'reused'}
        self.assertIsNone(m.validate_case_budget({'a':shared,'b':shared}))


if __name__ == '__main__':
    unittest.main()
