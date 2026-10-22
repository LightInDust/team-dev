import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import reviewer_provenance as p


def synthetic_review(root):
    """Synthetic gate fixture only; never represents a real model invocation."""
    record = {'version': 1, 'tool': 'collaboration.spawn_agent',
              'source': {'kind': 'parent_tool_observation', 'reference': 'SYNTHETIC UNIT TEST ONLY'},
              'request': {'task_name': 'fixture_review', 'model': 'gpt-5.6-sol',
                          'reasoning_effort': 'max', 'fork_turns': 'none'},
              'response': {'task_name': '/root/fixture_review'}}
    path = Path(root) / 'fixture-dispatch.json'
    path.write_text(json.dumps(record) + '\n')
    return {'model': 'gpt-5.6-sol', 'effort': 'max', 'reviewer_provenance': {
        'version': 1, 'requested_model': 'gpt-5.6-sol', 'requested_effort': 'max',
        'agent_id': '/root/fixture_review', 'reused_agent': False,
        'dispatch_evidence': {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
        'self_reported_model': 'unknown', 'actual_verified_model': 'unknown',
        'actual_verified_effort': 'unknown', 'verification_basis': 'parent_dispatch_only'}}


class ReviewerProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = synthetic_review(self.root)

    def change_dispatch(self, edit):
        path = self.root / 'fixture-dispatch.json'
        record = json.loads(path.read_text())
        edit(record)
        path.write_text(json.dumps(record) + '\n')
        self.review['reviewer_provenance']['dispatch_evidence']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()

    def test_parent_dispatch_is_accepted_without_inventing_actual_identity(self):
        result = p.validate(self.root, self.review)
        self.assertEqual(result['requested_model'], 'gpt-5.6-sol')
        self.assertEqual(result['actual_verified_model'], 'unknown')
        self.assertEqual(result['actual_verified_effort'], 'unknown')
        self.assertEqual(result['verification_basis'], 'parent_dispatch_only')
        # Contradictory self-report is retained; it never becomes route evidence.
        self.review['reviewer_provenance']['self_reported_model'] = 'gpt-6-sol'
        result = p.validate(self.root, self.review)
        self.assertEqual(result['self_reported_model'], 'gpt-6-sol')
        self.assertEqual(result['actual_verified_model'], 'unknown')

    def test_generic_identity_text_and_self_report_cannot_supply_dispatch(self):
        for source in ('generic_developer_identity', 'reviewer_self_report'):
            with self.subTest(source=source):
                self.review = synthetic_review(self.root)
                self.change_dispatch(lambda r: r.update(source={'kind': source, 'reference': 'agent based on GPT-6'}))
                with self.assertRaisesRegex(ValueError, 'parent-observed'):
                    p.validate(self.root, self.review)
        for field in ('actual_verified_model', 'actual_verified_effort'):
            review = synthetic_review(self.root)
            review['reviewer_provenance'][field] = 'gpt-5.6-sol' if field.endswith('model') else 'max'
            with self.assertRaisesRegex(ValueError, 'must remain unknown'):
                p.validate(self.root, review)
        review = synthetic_review(self.root)
        review.pop('reviewer_provenance')
        with self.assertRaisesRegex(ValueError, 'Require reviewer_provenance'):
            p.validate(self.root, review)

    def test_wrong_model_effort_or_inherited_context_dispatch_rejected(self):
        for changes in ({'model': 'gpt-6-sol'}, {'model': None}, {'reasoning_effort': 'high'},
                        {'reasoning_effort': None}, {'fork_turns': 'all'}, {'fork_turns': None}):
            with self.subTest(changes=changes):
                self.review = synthetic_review(self.root)
                self.change_dispatch(lambda r: r['request'].update(changes))
                with self.assertRaises(ValueError):
                    p.validate(self.root, self.review)

    def test_reuse_requires_original_spawn_of_same_agent(self):
        self.review['reviewer_provenance']['reused_agent'] = True
        self.assertEqual(p.validate(self.root, self.review)['agent_id'], '/root/fixture_review')
        self.review['reviewer_provenance']['agent_id'] = '/root/different_review'
        with self.assertRaisesRegex(ValueError, 'identity does not match'):
            p.validate(self.root, self.review)
        self.review = synthetic_review(self.root)
        self.change_dispatch(lambda r: r.update(tool='collaboration.followup_task'))
        with self.assertRaisesRegex(ValueError, 'original collaboration.spawn_agent'):
            p.validate(self.root, self.review)

    def test_evidence_mutation_missing_symlink_and_path_escape_fail(self):
        path = self.root / 'fixture-dispatch.json'
        path.write_text(path.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            p.validate(self.root, self.review)
        self.review = synthetic_review(self.root)
        target = self.root / 'source.json'
        path.rename(target)
        with self.assertRaisesRegex(ValueError, 'missing'):
            p.validate(self.root, self.review)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'linked'):
            p.validate(self.root, self.review)
        for name in ('../source.json', '/tmp/source.json'):
            self.review['reviewer_provenance']['dispatch_evidence']['file'] = name
            with self.assertRaisesRegex(ValueError, 'basename'):
                p.validate(self.root, self.review)

    def test_unknown_or_inconsistent_receipt_fields_do_not_pass(self):
        for changes in ({'requested_model': 'unknown'}, {'requested_effort': 'high'},
                        {'version': True}, {'self_reported_model': ''}, {'reused_agent': 'false'},
                        {'agent_id': '/root/unknown'}, {'dispatch_evidence': None}):
            with self.subTest(changes=changes):
                review = copy.deepcopy(self.review)
                review['reviewer_provenance'].update(changes)
                with self.assertRaises(ValueError):p.validate(self.root, review)
        self.review['model'] = 'gpt-6-sol'
        with self.assertRaisesRegex(ValueError, 'compatibility labels'):
            p.validate(self.root, self.review)

    def test_frozen_policy_keeps_legacy_schema_and_rejects_partial_changes(self):
        self.assertFalse(p.enabled({}, {}))
        marked = {'reviewer_provenance_policy': copy.deepcopy(p.POLICY)}
        self.assertTrue(p.enabled(marked, marked))
        for task, meta in ((marked, {}), ({}, marked),
                           (marked, {'reviewer_provenance_policy': dict(p.POLICY, version=True)}),
                           (marked, {'reviewer_provenance_policy': dict(p.POLICY, requested_effort='high')})):
            with self.assertRaisesRegex(ValueError, 'policy changed'):p.enabled(task, meta)
