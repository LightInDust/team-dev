"""Run the real cross-language example and isolated fault-sensitivity checks."""
import hashlib
import json
import os
from pathlib import Path
import re
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

    def tap_counts(self, output):
        counts = {}
        for key in ('tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo'):
            matches = re.findall(r'^# ' + key + r' (\d+)\r?$', output, re.MULTILINE)
            self.assertEqual(len(matches), 1, 'TAP summary for ' + key + ' is missing or ambiguous')
            counts[key] = int(matches[0])
        return counts

    def test_nested_shape_stays_green_on_stale_fixture_but_current_shape_is_rejected(self):
        # Mutate the disposable producer copy FIRST: the acceptance chain must reject
        # this nested field shape. Original source assets stay untouched.
        self.mutate('producer.py', "'station_count': count", "'station_count': {'value': count}")

        # Stale consumer unit suite: still green on the old committed primitive fixture.
        fixture = self.copy / 'preview.fixture.json'
        stale_fixture = fixture.read_bytes()
        self.assertEqual(
            json.loads(stale_fixture.decode()),
            [{'kind': 'preview', 'station_count': n} for n in (0, 1, 42)],
            'Stale committed fixture must retain primitive numeric station_count values')
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node.js runtime is required to run the consumer suite')
        consumer = subprocess.run(
            [node, '--test-reporter=tap', str(self.copy / 'consumer.test.mjs'), str(fixture)],
            cwd=self.copy, capture_output=True, text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=45)
        self.assertEqual(consumer.returncode, 0, consumer.stderr or consumer.stdout)
        counts = self.tap_counts(consumer.stdout)
        self.assertEqual(counts, {'tests': 11, 'pass': 11, 'fail': 0,
                                  'cancelled': 0, 'skipped': 0, 'todo': 0})

        # The mutated producer's nested shape reaches real serialization via the generator.
        code, receipt = self.check()
        self.assertEqual(code, 1)
        self.assertFalse(receipt['passed'])
        self.assertIn('differs from current producer', receipt['error'])
        generated = [c for c in receipt['commands']
                     if c['argv'][-1].endswith('generate_fixture.py')]
        self.assertEqual(len(generated), 1, 'expected exactly one fixture generation command')
        payload = json.loads(generated[0]['stdout'])
        self.assertEqual(
            payload,
            [{'kind': 'preview', 'station_count': {'value': n}} for n in (0, 1, 42)],
            'Generator stdout must carry the nested nonempty station_count shape')
        self.assertNotEqual(generated[0]['stdout'], stale_fixture.decode())
        # Expected fixture bytes and the real source example stay preserved.
        self.assertEqual(fixture.read_bytes(), stale_fixture)
        self.assertIn("'station_count': count", (EXAMPLE / 'producer.py').read_text())
        self.assertEqual((EXAMPLE / 'preview.fixture.json').read_bytes(), stale_fixture)


if __name__ == '__main__':
    unittest.main()
