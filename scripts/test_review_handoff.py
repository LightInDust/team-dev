import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import review_handoff as h
import reviewer_provenance as p
from test_reviewer_provenance import synthetic_review


class ReviewHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.review = dict(synthetic_review(self.root), verdict='pass', findings=[])
        self.payload = {'implementer': 'astra', 'case': 'SYNTHETIC ONLY',
                        'acceptance': 'coordinator declaration, not validated by this fixture'}
        self.write('review.json', self.review)
        self.write('draft.json', self.payload)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value) + '\n')

    def digest(self, name):
        return hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def publish(self, **kwargs):
        return h.write_receipt(self.root, 'review.json', kwargs.pop('expected', self.digest('review.json')),
                               kwargs.pop('draft', 'draft.json'), kwargs.pop('output', 'completion.json'))

    def assert_no_receipt(self):
        self.assertFalse((self.root / 'completion.json').exists())
        self.assertEqual(list(self.root.glob('.review-receipt-*')), [])

    def test_template_uses_exact_keys_from_actual_dispatch_without_a_pass_verdict(self):
        before = {x.name: x.read_bytes() for x in self.root.iterdir()}
        fields = h.reviewer_fields(self.root, 'fixture-dispatch.json')
        self.assertEqual(fields, {k: self.review[k] for k in ('model', 'effort', 'reviewer_provenance')})
        self.assertNotIn('verdict', fields)
        p.validate(self.root, fields)
        self.assertEqual(before, {x.name: x.read_bytes() for x in self.root.iterdir()})
        reused = h.reviewer_fields(self.root, 'fixture-dispatch.json', reused_agent=True)
        self.assertTrue(reused['reviewer_provenance']['reused_agent'])
        self.assertEqual(reused['reviewer_provenance']['agent_id'], '/root/fixture_review')
        p.validate(self.root, reused)

    def test_template_rejects_wrong_route_and_non_original_dispatch(self):
        record = json.loads((self.root / 'fixture-dispatch.json').read_text())
        for change in ('model', 'effort', 'tool', 'response', 'source'):
            bad = copy.deepcopy(record)
            if change == 'model': bad['request']['model'] = 'other'
            elif change == 'effort': bad['request']['reasoning_effort'] = 'high'
            elif change == 'tool': bad['tool'] = 'collaboration.followup_task'
            elif change == 'response': bad['response'] = {'task_name': '/root/other'}
            else: bad['source']['kind'] = 'self_report'
            self.write('fixture-dispatch.json', bad)
            with self.subTest(change=change), self.assertRaises(ValueError):
                h.reviewer_fields(self.root, 'fixture-dispatch.json')

    def test_historical_alias_shape_is_rejected_without_publishing_then_v2_can_pass(self):
        bad = dict(self.review, reviewer_provenance={
            'requested_model': 'gpt-5.6-sol', 'requested_reasoning_effort': 'max',
            'actual_model': 'unknown', 'dispatch_evidence': {'path': 'fixture-dispatch.json'}})
        self.write('review.json', bad)
        with self.assertRaisesRegex(ValueError, 'Require reviewer_provenance version 1'):
            self.publish()
        self.assert_no_receipt()
        self.write('review.json', self.review)
        self.publish()
        receipt = json.loads((self.root / 'completion.json').read_text())
        self.assertEqual(receipt['coordinator_payload'], self.payload)
        self.assertEqual(receipt['review_validation']['review']['sha256'], self.digest('review.json'))
        self.assertEqual(receipt['review_validation']['scope'], 'review_verdict_and_dispatch_provenance_only')
        self.assertEqual(receipt['review_validation']['result']['actual_verified_model'], 'unknown')
        self.assertNotIn('state', receipt)
        self.assertEqual(stat.S_IMODE((self.root / 'completion.json').stat().st_mode), 0o600)

    def test_nonpass_verdicts_and_stale_pinned_review_never_publish(self):
        for verdict in ('changes_requested', 'unknown', None, True, 'PASS'):
            self.write('review.json', dict(self.review, verdict=verdict))
            with self.subTest(verdict=verdict), self.assertRaises(ValueError): self.publish()
            self.assert_no_receipt()
        self.write('review.json', self.review)
        with self.assertRaisesRegex(ValueError, 'Review hash changed'): self.publish(expected='0' * 64)
        self.assert_no_receipt()

    def test_validation_exception_prevents_even_temporary_receipt_creation(self):
        with mock.patch.object(h, 'validate', side_effect=RuntimeError('injected validator failure')):
            with self.assertRaises(RuntimeError): self.publish()
        self.assert_no_receipt()

    def test_inputs_changing_during_validation_block_publication(self):
        for filename in ('review.json', 'draft.json', 'fixture-dispatch.json'):
            self.review = dict(synthetic_review(self.root), verdict='pass')
            self.write('review.json', self.review)
            self.write('draft.json', self.payload)
            original_validate = p.validate
            def changed(*args):
                result = original_validate(*args)
                path = self.root / filename
                path.write_bytes(path.read_bytes() + b' ')
                return result
            with self.subTest(filename=filename), mock.patch.object(h, 'validate', side_effect=changed):
                with self.assertRaises(ValueError): self.publish()
            self.assert_no_receipt()

    def test_final_validation_failure_removes_staging_file_without_publication(self):
        real = p.validate
        calls = []
        def fail_second(*args):
            calls.append(True)
            if len(calls) == 2: raise ValueError('final validation failed')
            return real(*args)
        with mock.patch.object(h, 'validate', side_effect=fail_second):
            with self.assertRaises(ValueError): self.publish()
        self.assertEqual(len(calls), 2)
        self.assert_no_receipt()

    def test_only_final_validation_input_mutation_is_checked_before_publication(self):
        for filename in ('review.json', 'draft.json', 'fixture-dispatch.json'):
            self.review = dict(synthetic_review(self.root), verdict='pass')
            self.write('review.json', self.review)
            self.write('draft.json', self.payload)
            calls = []
            def changed(*args):
                result = p.validate(*args)
                calls.append(True)
                if len(calls) == 2:
                    path = self.root / filename
                    path.write_bytes(path.read_bytes() + b' ')
                return result
            with self.subTest(filename=filename), mock.patch.object(h, 'validate', side_effect=changed):
                with self.assertRaises(ValueError): self.publish()
            self.assertEqual(len(calls), 2)
            self.assert_no_receipt()

    def test_existing_receipt_or_symlink_is_never_overwritten(self):
        self.write('completion.json', {'original': True})
        original = (self.root / 'completion.json').read_bytes()
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual((self.root / 'completion.json').read_bytes(), original)
        (self.root / 'completion.json').unlink()
        (self.root / 'completion.json').symlink_to(self.root / 'missing.json')
        with self.assertRaises(ValueError): self.publish()
        self.assertTrue((self.root / 'completion.json').is_symlink())

    def test_racing_output_creation_is_preserved_and_staging_is_cleaned(self):
        real_link = os.link
        def racing_link(src, dest):
            Path(dest).write_bytes(b'original competing receipt')
            return real_link(src, dest)
        with mock.patch.object(h.os, 'link', side_effect=racing_link):
            with self.assertRaises(FileExistsError): self.publish()
        self.assertEqual((self.root / 'completion.json').read_bytes(), b'original competing receipt')
        self.assertEqual(list(self.root.glob('.review-receipt-*')), [])

    def test_bad_input_shapes_paths_links_and_duplicate_keys_do_not_publish(self):
        for value in ([], None, {}, 'PRIVATE'):
            self.write('draft.json', value)
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError): self.publish()
            self.assert_no_receipt()
        self.write('draft.json', self.payload)
        for name in ('../elsewhere.json', '/tmp/elsewhere.json', 'not-json.txt'):
            with self.subTest(name=name), self.assertRaises(ValueError): self.publish(output=name)
        self.write('linked-source.json', self.payload)
        (self.root / 'draft.json').unlink()
        (self.root / 'draft.json').symlink_to(self.root / 'linked-source.json')
        with self.assertRaises(ValueError): self.publish()
        (self.root / 'draft.json').unlink()
        (self.root / 'draft.json').write_text('{"claim": false, "claim": true}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'): self.publish()
        self.assert_no_receipt()

    def test_cli_shell_continuation_cannot_make_rejected_writer_publish(self):
        # Reproduce the original missing-version error, then execute a successful
        # shell command. The shell status is zero, but no completion is published.
        broken = copy.deepcopy(self.review)
        broken['reviewer_provenance'].pop('version')
        self.write('review.json', broken)
        args = [sys.executable, '-B', str(Path(h.__file__)), 'receipt',
                '--evidence-root', str(self.root), '--review', 'review.json',
                '--expected-review-sha256', self.digest('review.json'),
                '--draft', 'draft.json', '--output', 'completion.json']
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        continued = subprocess.run(['/bin/sh', '-c', '"$@"\ntrue', 'fixture', *args],
                                   capture_output=True, text=True)
        self.assertEqual(continued.returncode, 0)
        self.assert_no_receipt()

    def test_cli_template_rejects_non_basename_dispatch_without_echoing_the_path(self):
        base = [sys.executable, '-B', str(Path(h.__file__))]
        before = {x.name: x.read_bytes() for x in self.root.iterdir()}
        absolute = str(self.root / 'fixture-dispatch.json')
        result = subprocess.run(base + ['template', '--evidence-root', str(self.root),
            '--dispatch', absolute], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('--dispatch needs a JSON basename under --evidence-root', result.stderr)
        self.assertIn('--dispatch reviewer-dispatch.json', result.stderr)
        self.assertNotIn(absolute, result.stderr)
        self.assertNotIn(str(self.root), result.stderr)
        self.assertEqual(before, {x.name: x.read_bytes() for x in self.root.iterdir()})
        # A valid basename still recovers on the same evidence root.
        result = subprocess.run(base + ['template', '--evidence-root', str(self.root),
            '--dispatch', 'fixture-dispatch.json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        p.validate(self.root, json.loads(result.stdout))

    def test_cli_template_and_receipt_share_the_exact_validator_contract(self):
        base = [sys.executable, '-B', str(Path(h.__file__))]
        result = subprocess.run(base + ['template', '--evidence-root', str(self.root),
            '--dispatch', 'fixture-dispatch.json', '--reused-agent'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = json.loads(result.stdout)
        p.validate(self.root, fields)
        self.write('review.json', dict(fields, verdict='pass'))
        result = subprocess.run(base + ['receipt', '--evidence-root', str(self.root),
            '--review', 'review.json', '--expected-review-sha256', self.digest('review.json'),
            '--draft', 'draft.json', '--output', 'completion.json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['receipt_sha256'], self.digest('completion.json'))


if __name__ == '__main__':
    unittest.main()
