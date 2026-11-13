"""Release preflight failures must never be interpreted as a missing tag."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

spec = importlib.util.spec_from_file_location('release_preflight', Path(__file__).resolve().parents[1]/'tools/release_preflight.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SOURCE = 'a' * 40


class ReleasePreflight(unittest.TestCase):
    def test_absent_release_and_tag(self):
        result = m.preflight('example/repository', 'v1.1.0', SOURCE, fetch=lambda path: None)
        self.assertFalse(result['tag_exists'])

    def test_matching_tag_can_be_used_without_replacement(self):
        fetch = Mock(side_effect=[None, {'object': {'type': 'commit', 'sha': SOURCE}}])
        self.assertTrue(m.preflight('example/repository', 'v1.1.0', SOURCE, fetch)['tag_exists'])

    def test_existing_release_is_never_overwritten(self):
        with self.assertRaises(ValueError):
            m.preflight('example/repository', 'v1.1.0', SOURCE, lambda p: {'id': 1})

    def test_error_body_and_wrong_tag_are_rejected(self):
        for payload in ({'message': 'Not Found', 'status': '404'}, {}, {'object': []},
                        {'object': {'type': 'commit', 'sha': 'b'*40}},
                        {'object': {'type': 'tag', 'sha': SOURCE}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                m.preflight('example/repository', 'v1.1.0', SOURCE, Mock(side_effect=[None, payload]))

    def test_only_http_404_means_absent(self):
        for code in (404, 403, 401, 429, 500):
            opener = Mock()
            opener.open.side_effect = HTTPError('https://api.github.com', code, 'test', {}, None)
            with patch.dict(m.os.environ, {'GH_TOKEN': 'synthetic'}), patch.object(m, 'build_opener', return_value=opener):
                if code == 404:
                    self.assertIsNone(m.get_json('repos/example/repository'))
                else:
                    with self.assertRaises(RuntimeError):
                        m.get_json('repos/example/repository')

    def test_network_failure_is_not_absence(self):
        opener = Mock()
        opener.open.side_effect = URLError('synthetic unavailable')
        with patch.dict(m.os.environ, {'GH_TOKEN': 'synthetic'}), patch.object(m, 'build_opener', return_value=opener), self.assertRaises(RuntimeError):
            m.get_json('repos/example/repository')


if __name__ == '__main__':
    unittest.main()
