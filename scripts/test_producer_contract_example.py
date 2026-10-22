"""Run the real cross-language example and isolated fault-sensitivity checks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


EXAMPLE = Path(__file__).resolve().parents[1] / 'assets' / 'producer-consumer'


class ProducerContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='team-dev-example-test-')
        self.addCleanup(self.tmp.cleanup)
        self.copy = Path(self.tmp.name) / 'example'
        shutil.copytree(EXAMPLE, self.copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    def check(self, node=None):
        argv = [sys.executable, str(self.copy / 'check_contract.py')]
        if node:
            argv.extend(['--node', node])
        result = subprocess.run(argv, capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=45)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def mutate(self, name, old, new):
        path = self.copy / name
        text = path.read_text()
        self.assertEqual(text.count(old), 1, 'Mutation must target exactly one statement')
        path.write_text(text.replace(old, new))

    def test_real_producer_generated_input_reaches_consumer(self):
        code, receipt = self.check()
        self.assertEqual(code, 0, receipt)
        self.assertTrue(receipt['passed'])
        self.assertTrue(receipt['source_stable'])
        fixture_sha = hashlib.sha256((self.copy / 'preview.fixture.json').read_bytes()).hexdigest()
        self.assertEqual(receipt['generated_fixture_sha256'], fixture_sha)
        self.assertEqual(receipt['committed_fixture_sha256'], fixture_sha)
        self.assertEqual(len(receipt['sources_sha256']), 6)
        consumer = receipt['commands'][-1]
        self.assertIn('preview.generated.json', consumer['argv'][-1])
        self.assertNotEqual(Path(consumer['argv'][-1]).parent, self.copy)
        self.assertEqual(receipt['consumer_test_counts'],
                         {'tests': 11, 'pass': 11, 'fail': 0, 'cancelled': 0, 'skipped': 0, 'todo': 0})

    def test_producer_field_rename_is_detected(self):
        self.mutate('producer.py', "'station_count'", "'chosen_station_count'")
        code, receipt = self.check()
        self.assertEqual(code, 1)
        self.assertIn('differs from current producer', receipt['error'])

    def test_wrong_producer_type_is_detected(self):
        self.mutate('producer.py', "'station_count': count", "'station_count': str(count)")
        code, receipt = self.check()
        self.assertEqual(code, 1)
        self.assertIn('differs from current producer', receipt['error'])

    def test_consumer_field_rename_is_detected(self):
        self.mutate('consumer.mjs', 'payload?.station_count', 'payload?.chosen_station_count')
        code, receipt = self.check()
        self.assertEqual(code, 1)
        self.assertFalse(receipt['passed'])
        self.assertNotEqual(receipt['commands'][-1]['exit_code'], 0)
        self.assertIn('not ok', receipt['commands'][-1]['stdout'])

    def test_stale_fixture_fails_without_being_overwritten(self):
        self.mutate('preview.fixture.json', '"station_count": 42', '"station_count": 41')
        fixture = self.copy / 'preview.fixture.json'
        stale = fixture.read_bytes()
        code, receipt = self.check()
        self.assertEqual(code, 1)
        self.assertIn('differs from current producer', receipt['error'])
        self.assertEqual(fixture.read_bytes(), stale)

    def test_missing_runtime_is_not_verified(self):
        code, receipt = self.check(str(self.copy / 'nonexistent-node'))
        self.assertEqual(code, 1)
        self.assertFalse(receipt['passed'])
        self.assertIn('NOT VERIFIED', receipt['error'])

    def test_empty_consumer_suite_cannot_pass_with_exit_zero(self):
        (self.copy / 'consumer.test.mjs').write_text('// No tests accidentally collected.\n')
        code, receipt = self.check()
        self.assertEqual(code, 1)
        self.assertFalse(receipt['passed'])
        self.assertIn('consumer test collection summary', receipt['error'])


if __name__ == '__main__':
    unittest.main()
