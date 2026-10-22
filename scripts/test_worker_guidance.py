"""Retained tests for versioned selectable worker guidance.

Covers the resolver contract (default/all, explicit minimal selection,
malformed selections and assessment) and the real prepare integration
(task/metadata/payload agreement, prior-run byte preservation, and
unchanged acceptance/test commands).
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import worker_guidance

spec = importlib.util.spec_from_file_location('team_dev', Path(__file__).with_name('team_dev.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


CANONICAL = list(worker_guidance.CANONICAL_ORDER)
VALID_ASSESSMENT = {'by': 'astra', 'reason': 'task selects a bounded section subset'}


class GuidanceResolutionTests(unittest.TestCase):
    def test_omitted_selection_includes_every_section_without_assessment(self):
        policy = worker_guidance.resolve({'goal': 'unrelated prose about rendered images'})
        self.assertEqual(policy['version'], 1)
        self.assertEqual(policy['sections'], CANONICAL)
        self.assertIsNone(policy['assessment'])
        self.assertEqual(set(policy['content_sha256']), set(CANONICAL))

    def test_explicit_empty_selection_selects_none_but_still_requires_assessment(self):
        with self.assertRaises(ValueError):
            worker_guidance.resolve({'worker_guidance': []})
        policy = worker_guidance.resolve({'worker_guidance': [], 'guidance_assessment': VALID_ASSESSMENT})
        self.assertEqual(policy['sections'], [])
        self.assertEqual(policy['content_sha256'], {})
        self.assertEqual(policy['assessment'], VALID_ASSESSMENT)

    def test_nonempty_selection_is_canonical(self):
        policy = worker_guidance.resolve({'worker_guidance': ['live_serialization', 'rendered_artifacts'],
                                          'guidance_assessment': VALID_ASSESSMENT})
        self.assertEqual(policy['sections'], ['rendered_artifacts', 'live_serialization'])
        self.assertEqual(policy['assessment'], VALID_ASSESSMENT)

    def test_malformed_selections_rejected(self):
        for value in ('rendered_artifacts', {'rendered_artifacts': 1}, None, 3):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    worker_guidance.resolve({'worker_guidance': value,
                                             'guidance_assessment': VALID_ASSESSMENT})
        for value in ([3], [None], ['unknown_section'], ['rendered_artifacts', 'rendered_artifacts']):
            with self.subTest(entries=value):
                with self.assertRaises(ValueError):
                    worker_guidance.resolve({'worker_guidance': value,
                                             'guidance_assessment': VALID_ASSESSMENT})

    def test_malformed_or_missing_assessment_rejected_for_explicit_selection(self):
        for value in (None, 'astra', {'by': 'reviewer', 'reason': 'x'}, {'by': 'sol'},
                      {'by': 'sol', 'reason': '   '}, {'by': 'sol', 'reason': 7}):
            with self.subTest(assessment=value):
                task = {'worker_guidance': ['model_isolation']}
                if value is not None:
                    task['guidance_assessment'] = value
                with self.assertRaises(ValueError):
                    worker_guidance.resolve(task)
        # A valid assessment is accepted; a blank reason never falls back to the default.
        policy = worker_guidance.resolve({'worker_guidance': ['model_isolation'],
                                          'guidance_assessment': {'by': 'user', 'reason': 'ok'}})
        self.assertEqual(policy['sections'], ['model_isolation'])

    def test_policy_is_deterministic_json_safe_and_content_hashes_match_catalog(self):
        task = {'worker_guidance': ['archived_evidence', 'model_isolation'],
                'guidance_assessment': VALID_ASSESSMENT}
        first = worker_guidance.resolve(task)
        second = worker_guidance.resolve(copy.deepcopy(task))
        self.assertEqual(first, second)
        self.assertEqual(json.loads(json.dumps(first)), first)
        for name in first['sections']:
            expected = hashlib.sha256(worker_guidance.SECTIONS[name].encode('utf-8')).hexdigest()
            self.assertEqual(first['content_sha256'][name], expected)
        self.assertNotEqual(first['content_sha256'],
                            worker_guidance.resolve({'goal': 'x'})['content_sha256'])

    def test_text_returns_only_selected_section_content(self):
        policy = worker_guidance.resolve({'worker_guidance': ['model_isolation'],
                                          'guidance_assessment': VALID_ASSESSMENT})
        self.assertEqual(worker_guidance.text(policy, 'model_isolation'),
                         worker_guidance.SECTIONS['model_isolation'])
        self.assertEqual(worker_guidance.text(policy, 'rendered_artifacts'), '')
        self.assertEqual(worker_guidance.text(policy, 'not_a_section'), '')


class PrepareGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'code.py').write_text('original\n')
        m.RUNS = self.root / 'runs'
        m.git(self.repo, 'init', '-q')
        m.git(self.repo, 'add', '-A')
        m.git(self.repo, '-c', 'user.name=Tests', '-c', 'user.email=tests@localhost',
              'commit', '-qm', 'baseline')
        self.task = {'target': {'transport': 'local', 'repo': str(self.repo)},
                     'goal': 'guidance fixture', 'allowed_paths': ['code.py'],
                     'acceptance': ['retain bytes', {'id': 'c1', 'clauses': ['a', 'b']}],
                     'test_commands': [['/usr/bin/true']]}

    def prepare(self):
        task = self.root / 'task.json'
        task.write_text(json.dumps(self.task))
        run = Path(m.prepare(task)['run'])
        payload = json.loads((run / 'prompt.md').read_text().split('\n\n', 1)[1])
        return run, payload

    def test_default_prepare_renders_every_section_and_agrees_with_saved_state(self):
        run, payload = self.prepare()
        prompt = (run / 'prompt.md').read_text()
        for name in CANONICAL:
            self.assertIn(worker_guidance.SECTIONS[name], prompt)
        saved_task = m.load(run / 'task.json')
        saved_meta = m.load(run / 'metadata.json')
        policy = saved_task['worker_guidance_policy']
        self.assertEqual(policy['sections'], CANONICAL)
        self.assertIsNone(policy['assessment'])
        self.assertEqual(policy, saved_meta['worker_guidance_policy'])
        self.assertEqual(policy, payload['worker_guidance_policy'])
        self.assertEqual(policy, worker_guidance.resolve(copy.deepcopy(self.task)))
        for name in CANONICAL:
            self.assertEqual(policy['content_sha256'][name],
                             hashlib.sha256(worker_guidance.SECTIONS[name].encode('utf-8')).hexdigest())

    def test_explicit_empty_selection_omits_domain_text_but_keeps_common_rules(self):
        self.task['worker_guidance'] = []
        self.task['guidance_assessment'] = VALID_ASSESSMENT
        run, payload = self.prepare()
        prompt = (run / 'prompt.md').read_text()
        for name in CANONICAL:
            self.assertNotIn(worker_guidance.SECTIONS[name], prompt)
        self.assertIn('Preserve the interface_contracts exactly', prompt)
        self.assertIn('Map each contract acceptance row', prompt)
        self.assertIn('Status DONE_WITH_CONCERNS', prompt)
        self.assertIn('CONTEXT INSPECTION', prompt)
        self.assertEqual(payload['worker_guidance_policy']['sections'], [])
        self.assertEqual(payload['worker_guidance_policy']['assessment'], VALID_ASSESSMENT)
        self.assertEqual(m.load(run / 'task.json')['worker_guidance_policy'],
                         payload['worker_guidance_policy'])

    def test_selection_change_leaves_prior_prepared_run_bytes_untouched(self):
        run_a, _ = self.prepare()
        frozen = {name: (run_a / name).read_bytes()
                  for name in ('task.json', 'prompt.md', 'metadata.json')}
        self.task['worker_guidance'] = ['model_isolation']
        self.task['guidance_assessment'] = {'by': 'sol', 'reason': 'focused model-isolation slice'}
        run_b, payload_b = self.prepare()
        self.assertNotEqual(run_a, run_b)
        self.assertEqual(payload_b['worker_guidance_policy']['sections'], ['model_isolation'])
        self.assertNotIn(worker_guidance.SECTIONS['rendered_artifacts'], (run_b / 'prompt.md').read_text())
        self.assertEqual({name: (run_a / name).read_bytes() for name in frozen}, frozen)

    def test_prepare_preserves_acceptance_and_exact_test_commands(self):
        run, payload = self.prepare()
        self.assertEqual(payload['acceptance'], self.task['acceptance'])
        self.assertEqual(payload['test_commands'], self.task['test_commands'])
        saved = m.load(run / 'task.json')
        self.assertEqual(saved['acceptance'], self.task['acceptance'])
        self.assertEqual(saved['test_commands'], self.task['test_commands'])

    def test_prepare_prompt_leads_with_compact_acceptance_and_bounds_context_reads(self):
        run, _ = self.prepare()
        prompt = (run / 'prompt.md').read_text()
        # Ledger immediately after the status line, before any optional narrative.
        self.assertLess(prompt.index('Status DONE'), prompt.index('compact COMPLETE acceptance ledger'))
        self.assertLess(prompt.index('compact COMPLETE acceptance ledger'),
                        prompt.index('Only then add a short narrative'))
        self.assertNotIn('After that summary include the full acceptance ledger', prompt)
        # Default/standard mode keeps its existing command permissions. The new
        # context-size hint must not silently impose the strict Bash allowlist.
        self.assertNotIn('never Bash cat/head/tail/grep', prompt)
        # Original obligations survive the fallback rules.
        for clause in ('pass/fail/not_verified', 'persistent_test/coordinator_check/source_inspection/none',
                       'regression_test (present/missing/unknown/not_required)', 'NOT VERIFIED',
                       'cleanup status', 'pending jobs', 'task-constraint deviations'):
            self.assertIn(clause, prompt)
        # Native bounded context routing, without inventing grep arguments.
        for hint in ('CONTEXT INSPECTION', 'native read/grep/glob tools',
                     'no limit or max_results argument', 'file:50-100'):
            self.assertIn(hint, prompt)

    def test_invalid_selection_is_rejected_before_target_capture(self):
        self.task['worker_guidance'] = ['unknown_section']
        self.task['guidance_assessment'] = VALID_ASSESSMENT
        with mock.patch.object(m.Repo, 'capture', side_effect=AssertionError('target must not be read')) as capture:
            with self.assertRaisesRegex(ValueError, 'Unknown worker_guidance'):
                self.prepare()
            capture.assert_not_called()
        self.assertFalse(m.RUNS.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
