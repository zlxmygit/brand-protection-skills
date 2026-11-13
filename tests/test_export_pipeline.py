"""Offline integration: capture -> recorded evidence -> approved byte export."""
import copy
from pathlib import Path
import socket
import tempfile
import unittest
import zipfile
import test_evidence_export as e
from capture_web import capture


class ExportPipeline(unittest.TestCase):
    def test_capture_to_reviewed_export_preserves_the_approved_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, template = e.fixtures(root)
            resolve = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
            fetch = lambda *a: (200, {'content-type': 'text/plain'}, b'synthetic public response')
            captured = capture('https://example.com/', 't1', root, ['example.com'],
                               allow_network=True, resolver=resolve, transport=fetch)
            evidence = captured['evidence']
            case = {'case_id': 'synthetic', 'evidence': [evidence]}
            review = copy.deepcopy(template)
            review['items'] = [{'evidence_id': evidence['id'], 'sha256': evidence['sha256'],
                                'disclosure_reviewed': True, 'allow_original': True}]
            receipt = e.m.export(case, review, root, root, e.NOW)
            with zipfile.ZipFile(receipt['package']) as handle:
                self.assertEqual(handle.read('files/0001.bin'), Path(evidence['original_path']).read_bytes())
            self.assertFalse(receipt['authorizes_submission'])


if __name__ == '__main__': unittest.main()
