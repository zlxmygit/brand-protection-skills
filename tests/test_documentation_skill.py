"""The maintainer's writing Skill is pinned and excluded from runtime packages."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DocumentationSkill(unittest.TestCase):
    def test_upstream_files_match_reviewed_blobs(self):
        expected = {
            'SKILL.md': 'f0ef55c2e87e3c65463392b014a9e01850f527bd',
            'references/typography.md': 'f51bc6aae51da4b5b49236e4b99d091716cf3625',
            'references/manual-structure.md': 'a5d829820b57846a7c7c6cbcfe62a138ec85a163',
            'LICENSE': '012e98bf03de57258be3d0d3eaec94993453213e',
        }
        for name, wanted in expected.items():
            with self.subTest(name=name):
                data = (ROOT/'.agents/skills/zh-tech-writing'/name).read_bytes()
                actual = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                self.assertEqual(actual, wanted)

    def test_developer_skill_does_not_enter_runtime_allowlist(self):
        names = json.loads((ROOT/'release/skill-files.json').read_text(encoding='utf-8'))
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertNotIn('zh-tech-writing', name)
            self.assertNotIn('..', Path(name).parts)
            self.assertFalse(Path(name).is_absolute())


if __name__ == '__main__':
    unittest.main()
