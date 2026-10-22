"""Exercise generated handoff scaffolds through the real prepare boundary."""
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

import team_dev as m


class HandoffPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'code.py').write_text('value = 1\n')
        m.git(self.repo, 'init', '-q')
        m.git(self.repo, 'add', '-A')
        m.git(self.repo, '-c', 'user.name=Tests', '-c', 'user.email=tests@localhost',
              'commit', '-qm', 'baseline')
        patch = mock.patch.object(m, 'RUNS', self.root / 'runs')
        patch.start()
        self.addCleanup(patch.stop)
        self.task = {
            'target': {'transport': 'local', 'repo': str(self.repo)},
            'goal': 'exercise generated reports without a model call',
            'allowed_paths': ['code.py'],
            'acceptance': ['first clause', {'clauses': ['second', 'third']},
                           'fourth clause', 'fifth clause', 'sixth clause'],
            'worker_thinking': 'low', 'budget_mode': 'auto',
            'effort_assessment': {'by': 'astra', 'reason': 'fixed test fixture'},
            'worker_bash_mode': 'declared_tests_only',
            'worker_guidance': [],
            'guidance_assessment': {'by': 'astra', 'reason': 'no domain behavior'},
            'test_commands': [[sys.executable, '-B', '-m', 'unittest', 'test_a'],
                              [sys.executable, '-B', '-m', 'unittest', 'test_b']],
        }

    def prepare(self):
        task = self.root / 'task.json'
        task.write_text(json.dumps(self.task))
        run = Path(m.prepare(task)['run'])
        payload = json.loads((run / 'prompt.md').read_text().split('\n\n', 1)[1])
        return run, payload

    def test_real_prepare_scaffold_covers_every_requirement_and_check_once(self):
        run, payload = self.prepare()
        scaffold = payload.get('worker_handoff_template')
        self.assertIsInstance(scaffold, str, 'prepare must supply a concrete report scaffold')
        self.assertEqual(re.findall(r'^(A\d+)\s*\|', scaffold, re.M),
                         ['A1', 'A2', 'A3', 'A4', 'A5'])
        self.assertEqual(re.findall(r'^Evidence (E\d+): T(\d+)\b', scaffold, re.M),
                         [('E1', '1'), ('E2', '2')])
        self.assertEqual(payload['acceptance'], self.task['acceptance'])
        self.assertEqual(payload['test_commands'], self.task['test_commands'])
        self.assertEqual(payload['worker_acceptance_inventory']['rows'][1]['source_pointer'],
                         '/acceptance/1')
        self.assertEqual(m.load(run / 'metadata.json')['process'], 'not_started')
        self.assertEqual(m.load(run / 'task.json')['execution_policy']['closeout'],
                         {'warn_seconds': 180, 'deadline_seconds': 300})

    def test_template_is_unfilled_and_has_process_disposition_slots(self):
        _, payload = self.prepare()
        scaffold = payload.get('worker_handoff_template', '')
        self.assertTrue(scaffold.startswith('Status <'))
        for label in ['Pending tools/jobs:', 'Scratch:', 'Deviations/concerns:']:
            self.assertIn(label, scaffold)
        self.assertNotRegex(scaffold, r'(?im)^Status DONE(?:\s|$)|\|\s*pass\s*\|')
        for original in ('first clause', 'fourth clause', 'fifth clause'):
            self.assertNotIn(original, scaffold, 'scaffold indexes rather than duplicates requirements')

    def test_absent_and_empty_acceptance_remain_explicit_gaps(self):
        for supplied, value, state in [(False, None, 'absent'), (True, [], 'empty'),
                                       (True, '', 'empty'), (True, {}, 'empty')]:
            with self.subTest(value=value, supplied=supplied):
                self.task.pop('acceptance', None)
                if supplied:
                    self.task['acceptance'] = value
                _, payload = self.prepare()
                scaffold = payload.get('worker_handoff_template', '')
                self.assertIn('Acceptance: ' + state, scaffold)
                self.assertNotRegex(scaffold, r'(?m)^A\d+\s*\|')
                self.assertIn('NOT VERIFIED', scaffold)

    def test_scalar_and_object_acceptance_keep_original_semantics(self):
        for value in ('one compound requirement and its exception', {'a': ['one', 'two']}):
            with self.subTest(value=value):
                self.task['acceptance'] = copy.deepcopy(value)
                _, payload = self.prepare()
                self.assertEqual(payload['acceptance'], value)
                self.assertEqual(re.findall(r'^(A\d+)\s*\|',
                                 payload.get('worker_handoff_template', ''), re.M), ['A1'])
                self.assertEqual(payload['worker_acceptance_inventory']['rows'],
                                 [{'id': 'A1', 'source_pointer': '/acceptance'}])

    def test_preparing_another_task_does_not_rewrite_existing_run(self):
        run, _ = self.prepare()
        saved = {name: (run / name).read_bytes() for name in
                 ('prompt.md', 'task.json', 'metadata.json')}
        self.task['acceptance'] = ['a new scope']
        self.prepare()
        for name, content in saved.items():
            self.assertEqual((run / name).read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
