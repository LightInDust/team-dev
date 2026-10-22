"""Acceptance tests for the read-only coordinator evidence tools.

Fixtures mirror the real team_dev.py run layout. Every fixture lives inside an
owned TemporaryDirectory; all subprocess consumers run inside that scope.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import evidence_tools as e

SOURCE = {'host': None, 'user': 'worker', 'repo': '/repo', 'head': 'a' * 40}
SNAPSHOT_COMMIT = 'c' * 40
MARKER = 'SECRET-MARKER-DO-NOT-READ'


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def write(root, name, value):
    (root / name).write_text(json.dumps(value) + '\n')


def write_manifest(root, value, name='manifest.json'):
    write(root, name, value)
    return root / name


def make_run(root, name='run-1'):
    run = root / name
    run.mkdir()
    patch = b'diff --git a/scripts/evidence_tools.py b/scripts/evidence_tools.py\n'
    (run / 'changes.patch').write_bytes(patch)
    (run / 'test-0.stdout').write_bytes(b'Ran 3 tests in 0.01s\n\nOK\n')
    (run / 'test-0.stderr').write_bytes(b'')
    fingerprint = sha256(b'applied-manifest')
    write(run, 'metadata.json', {
        'run': str(run), 'created_at': 1700000000.0, 'source': SOURCE,
        'paths': ['scripts/evidence_tools.py'], 'snapshot_commit': SNAPSHOT_COMMIT,
        'process': 'exited', 'delivery': 'pending_review', 'worker_model': 'model-id',
        'applied_fingerprint': fingerprint})
    write(run, 'task.json', {
        'target': {'transport': 'local', 'host': None, 'repo': '/repo'},
        'goal': 'Implement bounded read-only evidence tooling',
        'acceptance': ['A1', 'A2'],
        'interface_contracts': [{'name': 'recovery_index', 'source_paths': ['scripts/evidence_tools.py']}],
        'allowed_paths': ['scripts/evidence_tools.py'],
        'context_paths': ['scripts/team_dev.py'],
        'test_commands': [['/usr/bin/python3', '-B', '-m', 'unittest', 'discover', '-s', 'scripts']],
        'worker_guidance_policy': {'version': 1, 'sections': []}})
    write(run, 'proposal.json', {'paths': ['scripts/evidence_tools.py'],
                                 'patch_sha256': sha256(patch), 'expected': {}})
    write(run, 'applied.json', {'manifest': {'x': 1}, 'fingerprint': fingerprint,
                                'source': SOURCE, 'at': 1700000001.0})
    write(run, 'tests.json', {
        'host': SOURCE, 'fingerprint': fingerprint, 'unchanged': True, 'passed': True,
        'tests': [{'argv': ['/usr/bin/python3', '-B', '-m', 'unittest'],
                   'exit_code': 0, 'stdout': 'test-0.stdout', 'stderr': 'test-0.stderr',
                   'stdout_sha256': sha256((run / 'test-0.stdout').read_bytes()),
                   'stderr_sha256': sha256((run / 'test-0.stderr').read_bytes()),
                   'collection': {'passed': True}}]})
    write(run, 'review.json', {'verdict': 'pass', 'fingerprint': fingerprint, 'findings': [],
                               'process_evidence': {'worker_result_sha256': 'e' * 64},
                               'reviewer_provenance': {'version': 1}})
    write(run, 'worker-result.json', {'terminal_event': True, 'final_text': 'done'})
    return run


def _nonfinite(value):
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_nonfinite(item) for item in value.values())
    if isinstance(value, list):
        return any(_nonfinite(item) for item in value)
    return False


def _index_entry(root, run, name='manifest.json'):
    path = write_manifest(root, {'version': 1, 'coordinator_agent_id': '/root/astra',
                                 'slices': [{'id': 'slice-1', 'runs': [str(run)]}]}, name=name)
    return e.recovery_index(path)['slices'][0]['runs'][0]


def cli(*args):
    return subprocess.run([sys.executable, '-B', str(Path(e.__file__)), *args],
                          capture_output=True, text=True, cwd=Path.cwd(), timeout=60)


def tree_snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()}


class EvidenceToolBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def manifest_value(self, runs, sid='slice-1', agent='/root/astra', version=1):
        return {'version': version, 'coordinator_agent_id': agent,
                'slices': [{'id': sid, 'runs': [str(r) for r in runs]}]}


class RecoveryIndexTests(EvidenceToolBase):
    def test_index_reports_recorded_state_and_observed_hashes(self):
        run = make_run(self.root)
        path = write_manifest(self.root, self.manifest_value([run]))
        before = tree_snapshot(self.root)
        result = e.recovery_index(path)
        self.assertIs(result['current_liveness_verified'], False)
        self.assertIs(result['current_target_verified'], False)
        self.assertIs(result['acceptance_certified'], False)
        self.assertEqual(result['coordinator_agent_id'], '/root/astra')
        self.assertEqual(result['manifest']['sha256'], sha256(path.read_bytes()))
        entry = result['slices'][0]['runs'][0]
        self.assertEqual(entry['recorded']['process'], 'exited')
        self.assertEqual(entry['recorded']['delivery'], 'pending_review')
        self.assertEqual(entry['recorded']['source'], SOURCE)
        self.assertTrue(entry['evidence_complete'])
        files = {item['file']: item for item in entry['evidence']}
        self.assertEqual(files['metadata.json']['sha256'], sha256((run / 'metadata.json').read_bytes()))
        self.assertEqual(files['changes.patch']['sha256'], sha256((run / 'changes.patch').read_bytes()))
        self.assertEqual(tree_snapshot(self.root), before)

    def test_index_rejects_malformed_manifests(self):
        run = make_run(self.root)
        cases = {
            'not-object': [],
            'missing-version': {'coordinator_agent_id': 'a',
                                'slices': [{'id': 's', 'runs': [str(run)]}]},
            'wrong-version': self.manifest_value([run], version=2),
            'bool-version': self.manifest_value([run], version=True),
            'agent-blank': self.manifest_value([run], agent='  '),
            'slices-empty': {'version': 1, 'coordinator_agent_id': 'a', 'slices': []},
            'slice-id-blank': self.manifest_value([run], sid=''),
            'runs-empty': {'version': 1, 'coordinator_agent_id': 'a',
                           'slices': [{'id': 's', 'runs': []}]},
            'run-relative': {'version': 1, 'coordinator_agent_id': 'a',
                             'slices': [{'id': 's', 'runs': ['relative/run']}]},
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                path = write_manifest(self.root, value, name + '.json')
                with self.assertRaises(ValueError):
                    e.recovery_index(path)
                result = cli('index', '--manifest', str(path), '--output', str(self.root / 'out.json'))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertFalse((self.root / 'out.json').exists())

    def test_index_rejects_duplicate_slices_and_equivalent_runs(self):
        run = make_run(self.root)
        duplicate_id = {'version': 1, 'coordinator_agent_id': 'a',
                        'slices': [{'id': 'same', 'runs': [str(run)]},
                                   {'id': 'same', 'runs': [str(run)]}]}
        dotdot = {'version': 1, 'coordinator_agent_id': 'a',
                  'slices': [{'id': 's', 'runs': [str(run), str(run / '..' / 'run-1')]}]}
        link = self.root / 'link-run'
        link.symlink_to(run, target_is_directory=True)
        symlinked = {'version': 1, 'coordinator_agent_id': 'a',
                     'slices': [{'id': 's', 'runs': [str(run), str(link)]}]}
        for name, value in (('duplicate-id', duplicate_id), ('dotdot', dotdot), ('symlink', symlinked)):
            with self.subTest(name=name):
                path = write_manifest(self.root, value, name + '.json')
                with self.assertRaises(ValueError):
                    e.recovery_index(path)
                self.assertNotEqual(cli('index', '--manifest', str(path)).returncode, 0)

    def test_index_enforces_the_sixteen_run_limit(self):
        runs = [make_run(self.root, 'run-%d' % i) for i in range(17)]
        path = write_manifest(self.root, self.manifest_value(runs))
        with self.assertRaises(ValueError):
            e.recovery_index(path)
        self.assertEqual(cli('index', '--manifest', str(path)).returncode, 1)

    def test_running_and_verified_records_never_become_a_fresh_pass(self):
        running = make_run(self.root, 'run-running')
        write(running, 'metadata.json', dict(json.loads((running / 'metadata.json').read_text()),
                                             applied_fingerprint='0' * 64, process='running'))
        verified = make_run(self.root, 'run-verified')
        write(verified, 'metadata.json', dict(json.loads((verified / 'metadata.json').read_text()),
                                              delivery='verified'))
        result = e.recovery_index(write_manifest(self.root, self.manifest_value([running, verified])))
        first, second = result['slices'][0]['runs']
        self.assertFalse(first['evidence_complete'])
        self.assertTrue(any(i['kind'] == 'conflict' for i in first['issues']))
        self.assertTrue(any('duplicate' in s for s in first['suggested_next_inspection']))
        self.assertEqual(second['recorded']['delivery'], 'verified')
        self.assertTrue(any('never auto-skip' in s for s in second['suggested_next_inspection']))
        self.assertIs(result['acceptance_certified'], False)
        self.assertIs(result['current_target_verified'], False)
        self.assertIs(result['current_liveness_verified'], False)

    def test_failed_candidate_suggests_inspecting_the_preserved_candidate(self):
        run = make_run(self.root)
        write(run, 'failure.json', {'reason': 'tool_idle', 'process': 'failed'})
        write(run, 'metadata.json', dict(json.loads((run / 'metadata.json').read_text()),
                                         delivery='worker_failed'))
        entry = e.recovery_index(write_manifest(self.root, self.manifest_value([run])))['slices'][0]['runs'][0]
        self.assertTrue(any('preserved candidate' in s for s in entry['suggested_next_inspection']))
        self.assertEqual(entry['recorded']['delivery'], 'worker_failed')

    def test_symlinked_nonregular_and_oversized_evidence_stay_issues(self):
        run = make_run(self.root)
        (run / 'review.json').unlink()
        (run / 'review.json').symlink_to(run / 'proposal.json')
        (run / 'tests.json').unlink()
        (run / 'tests.json').mkdir()
        with (run / 'changes.patch').open('wb') as stream:
            stream.write(b'x' * (e.BLOB_LIMIT + 1))
        with (run / 'proposal.json').open('wb') as stream:
            stream.write(b'{' + b' ' * (e.JSON_LIMIT + 1) + b'}')
        entry = e.recovery_index(write_manifest(self.root, self.manifest_value([run])))['slices'][0]['runs'][0]
        details = ' '.join(i['detail'] for i in entry['issues'])
        self.assertIn('symlink', details)
        self.assertIn('nonregular', details)
        self.assertIn('oversized', details)
        self.assertFalse(entry['evidence_complete'])

    def test_credential_and_session_decoys_are_never_read(self):
        run = make_run(self.root)
        (run / 'credentials.json').write_text('{"opencode_api_key": "%s"}' % MARKER)
        (run / 'invocation.json').write_text('{"argv": ["%s"]}' % MARKER)
        (run / 'sessions').mkdir()
        (run / 'sessions' / 'session.json').write_text('{"transcript": "%s"}' % MARKER)
        result = e.recovery_index(write_manifest(self.root, self.manifest_value([run])))
        self.assertNotIn(MARKER, json.dumps(result))
        self.assertEqual((run / 'credentials.json').read_text(), '{"opencode_api_key": "%s"}' % MARKER)
        files = {i['file'] for i in result['slices'][0]['runs'][0]['evidence']}
        self.assertNotIn('credentials.json', files)
        self.assertNotIn('invocation.json', files)


class ReviewPackageTests(EvidenceToolBase):
    def test_bundle_exposes_task_tests_diff_and_pointer_report(self):
        run = make_run(self.root)
        before = tree_snapshot(self.root)
        package = e.review_package(run)
        self.assertTrue(package['complete'])
        self.assertEqual(package['task']['goal'], 'Implement bounded read-only evidence tooling')
        self.assertEqual(package['task']['test_commands'],
                         [['/usr/bin/python3', '-B', '-m', 'unittest', 'discover', '-s', 'scripts']])
        self.assertEqual(package['task']['allowed_paths'], ['scripts/evidence_tools.py'])
        self.assertEqual(package['run']['path'], str(run))
        self.assertEqual(package['target']['recorded_source'], SOURCE)
        self.assertEqual(package['diff']['sha256'], sha256((run / 'changes.patch').read_bytes()))
        self.assertEqual(package['tests']['receipts'][0]['stdout']['sha256'],
                         sha256((run / 'test-0.stdout').read_bytes()))
        self.assertEqual(package['review']['recorded_verdict'], 'pass')
        self.assertFalse(package['worker_final_report']['authoritative_output'])
        self.assertEqual(package['worker_final_report']['sha256'],
                         sha256((run / 'worker-result.json').read_bytes()))
        self.assertIs(package['current_liveness_verified'], False)
        self.assertIs(package['current_target_verified'], False)
        self.assertIs(package['acceptance_certified'], False)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_missing_metadata_or_task_yields_incomplete_bundle(self):
        for missing in ('metadata.json', 'task.json'):
            with self.subTest(missing=missing):
                run = make_run(self.root, 'run-' + missing.split('.')[0])
                (run / missing).unlink()
                package = e.review_package(run)
                self.assertFalse(package['complete'])
                self.assertIn(missing + ': missing', ' '.join(package['known_gaps']))
                result = cli('bundle', '--run', str(run))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(json.loads(result.stdout)['complete'])

    def test_invalid_task_shape_is_incomplete_and_reported(self):
        run = make_run(self.root)
        write(run, 'task.json', {'target': 'not-an-object', 'goal': '   ',
                                 'allowed_paths': [], 'test_commands': 'nope'})
        package = e.review_package(run)
        self.assertFalse(package['complete'])
        details = ' '.join(i['detail'] for i in package['issues'])
        for field in ('task.target', 'task.goal', 'task.allowed_paths', 'task.test_commands'):
            self.assertIn(field, details)

    def test_untrusted_test_log_names_are_not_read(self):
        run = make_run(self.root)
        (self.root / 'outside.log').write_text(MARKER)
        tests = json.loads((run / 'tests.json').read_text())
        tests['tests'][0]['stdout'] = '../outside.log'
        write(run, 'tests.json', tests)
        package = e.review_package(run)
        receipt = package['tests']['receipts'][0]
        self.assertEqual(receipt['stdout']['file'], '../outside.log')
        self.assertIn('issue', receipt['stdout'])
        self.assertNotIn(MARKER, json.dumps(package))
        self.assertTrue(any(i['kind'] == 'path' for i in package['issues']))

    def test_excess_recorded_tests_are_flagged_and_bounded(self):
        run = make_run(self.root)
        tests = json.loads((run / 'tests.json').read_text())
        entry = tests['tests'][0]
        tests['tests'] = [dict(entry, stdout='test-%d.stdout' % i, stderr='test-%d.stderr' % i)
                          for i in range(17)]
        write(run, 'tests.json', tests)
        package = e.review_package(run)
        self.assertTrue(any(i['kind'] == 'limit' for i in package['issues']))
        self.assertEqual(len(package['tests']['receipts']), 16)


class OutputBoundaryTests(EvidenceToolBase):
    def test_output_is_exclusive_private_and_never_clobbers(self):
        run = make_run(self.root)
        manifest = write_manifest(self.root, self.manifest_value([run]))
        out = self.root / 'index.json'
        result = cli('index', '--manifest', str(manifest), '--output', str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(result.stdout)
        self.assertEqual(saved['output'], str(out))
        self.assertEqual(saved['sha256'], sha256(out.read_bytes()))
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        content = out.read_bytes()
        again = cli('index', '--manifest', str(manifest), '--output', str(out))
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(out.read_bytes(), content)

    def test_bundle_output_is_written_exclusively(self):
        run = make_run(self.root)
        out = self.root / 'package.json'
        result = cli('bundle', '--run', str(run), '--output', str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        package = json.loads(out.read_text())
        self.assertTrue(package['complete'])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_output_cannot_replace_manifest_or_sit_inside_a_run(self):
        run = make_run(self.root)
        manifest = write_manifest(self.root, self.manifest_value([run]))
        replace = cli('index', '--manifest', str(manifest), '--output', str(manifest))
        self.assertNotEqual(replace.returncode, 0)
        self.assertEqual(json.loads(manifest.read_text())['version'], 1)
        inside = cli('bundle', '--run', str(run), '--output', str(run / 'package.json'))
        self.assertNotEqual(inside.returncode, 0)
        self.assertFalse((run / 'package.json').exists())

    def test_argument_errors_leave_no_output_file(self):
        out = self.root / 'never.json'
        missing = cli('index', '--manifest', str(self.root / 'missing-manifest.json'),
                      '--output', str(out))
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(out.exists())
        usage = cli('index', '--output', str(out))
        self.assertEqual(usage.returncode, 2)
        self.assertFalse(out.exists())


class BoundedEvidenceReadTests(EvidenceToolBase):
    def test_selected_evidence_uses_a_bounded_no_follow_reader(self):
        run = make_run(self.root)
        manifest = write_manifest(self.root, self.manifest_value([run]))
        expected = {name: sha256((run / name).read_bytes())
                    for name in ('metadata.json', 'task.json', 'changes.patch', 'test-0.stdout')}
        with mock.patch.object(Path, 'read_bytes',
                               side_effect=AssertionError('unbounded whole-file read')):
            index = e.recovery_index(manifest)
            package = e.review_package(run)
        entry = index['slices'][0]['runs'][0]
        self.assertTrue(entry['evidence_complete'], entry['issues'])
        observed = {item['file']: item['sha256'] for item in entry['evidence']}
        for name in ('metadata.json', 'task.json', 'changes.patch'):
            self.assertEqual(observed[name], expected[name])
        self.assertEqual(package['diff']['sha256'], expected['changes.patch'])
        self.assertEqual(package['tests']['receipts'][0]['stdout']['sha256'], expected['test-0.stdout'])

    def test_dangling_symlink_evidence_is_reported_as_a_link(self):
        run = make_run(self.root)
        (run / 'review.json').unlink()
        (run / 'review.json').symlink_to(self.root / 'absent-review.json')
        entry = e.recovery_index(write_manifest(self.root, self.manifest_value([run])))['slices'][0]['runs'][0]
        self.assertIn('symlink', ' '.join(i['detail'] for i in entry['issues']))
        self.assertFalse(entry['evidence_complete'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO evidence requires os.mkfifo')
    def test_fifo_evidence_is_rejected_without_blocking_a_reader(self):
        run = make_run(self.root)
        (run / 'review.json').unlink()
        os.mkfifo(run / 'review.json')
        result = cli('bundle', '--run', str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        package = json.loads(result.stdout)
        self.assertIn('nonregular', ' '.join(i['detail'] for i in package['issues']))
        self.assertFalse(package['complete'])


class RecordedLogDigestTests(EvidenceToolBase):
    def test_recorded_log_digest_mismatch_stays_visible(self):
        for kind in ('stdout', 'stderr'):
            with self.subTest(stream=kind):
                run = make_run(self.root, 'run-' + kind)
                tests = json.loads((run / 'tests.json').read_text())
                tests['tests'][0][kind + '_sha256'] = '0' * 64
                write(run, 'tests.json', tests)
                observed = sha256((run / ('test-0.' + kind)).read_bytes())
                package = e.review_package(run)
                receipt = package['tests']['receipts'][0][kind]
                self.assertEqual(receipt['expected_sha256'], '0' * 64)
                self.assertEqual(receipt['sha256'], observed)
                self.assertEqual(receipt['digest_status'], 'mismatch')
                self.assertFalse(package['complete'])
                self.assertTrue(any(i['kind'] == 'conflict' and kind in i['detail']
                                    for i in package['issues']))
                entry = _index_entry(self.root, run, name='manifest-%s.json' % kind)
                self.assertFalse(entry['evidence_complete'])

    def test_absent_legacy_log_digest_is_unknown_gap_not_a_mismatch(self):
        run = make_run(self.root)
        tests = json.loads((run / 'tests.json').read_text())
        tests['tests'][0].pop('stdout_sha256')
        write(run, 'tests.json', tests)
        package = e.review_package(run)
        receipt = package['tests']['receipts'][0]['stdout']
        self.assertEqual(receipt['expected_sha256'], e.UNKNOWN)
        self.assertEqual(receipt['digest_status'], e.UNKNOWN)
        self.assertEqual(receipt['sha256'], sha256((run / 'test-0.stdout').read_bytes()))
        self.assertTrue(any(i['kind'] == 'digest' and i['severity'] == 'gap' for i in package['issues']))
        self.assertFalse(any(i['kind'] == 'conflict' for i in package['issues']))
        self.assertFalse(package['complete'])
        self.assertFalse(_index_entry(self.root, run, name='legacy-manifest.json')['evidence_complete'])

    def test_malformed_log_digest_is_an_error(self):
        run = make_run(self.root)
        tests = json.loads((run / 'tests.json').read_text())
        tests['tests'][0]['stderr_sha256'] = 'not-a-digest'
        write(run, 'tests.json', tests)
        package = e.review_package(run)
        receipt = package['tests']['receipts'][0]['stderr']
        self.assertEqual(receipt['expected_sha256'], 'not-a-digest')
        self.assertEqual(receipt['digest_status'], 'malformed')
        self.assertTrue(any(i['kind'] == 'digest' and i['severity'] == 'error' for i in package['issues']))
        self.assertFalse(package['complete'])


class CanonicalLogNameTests(EvidenceToolBase):
    def test_zero_padded_and_stream_mismatched_names_are_not_read(self):
        cases = {'test-00.stdout': 'test-00.stdout', 'stream-mismatch': 'test-0.stderr',
                 'not-a-name': 'logs/test-0.stdout'}
        for label, name in cases.items():
            with self.subTest(case=label):
                run = make_run(self.root, 'run-' + label)
                (run / 'test-00.stdout').write_bytes(b'not evidence\n')
                tests = json.loads((run / 'tests.json').read_text())
                tests['tests'][0]['stdout'] = name
                write(run, 'tests.json', tests)
                package = e.review_package(run)
                receipt = package['tests']['receipts'][0]['stdout']
                self.assertEqual(receipt['file'], name)
                self.assertIn('issue', receipt)
                self.assertTrue(any(i['kind'] == 'path' for i in package['issues']))

    def test_hostile_index_digits_do_not_abort_the_bundle(self):
        run = make_run(self.root)
        tests = json.loads((run / 'tests.json').read_text())
        tests['tests'][0]['stdout'] = 'test-%s.stdout' % ('9' * 5000)
        write(run, 'tests.json', tests)
        package = e.review_package(run)
        receipt = package['tests']['receipts'][0]['stdout']
        self.assertIn('issue', receipt)
        self.assertTrue(any(i['kind'] == 'path' for i in package['issues']))
        json.dumps(package, ensure_ascii=False, allow_nan=False)
        result = cli('bundle', '--run', str(run))
        self.assertEqual(result.returncode, 0, result.stderr)


class NonFiniteEvidenceTests(EvidenceToolBase):
    def test_nonfinite_optional_evidence_is_invalid_and_output_stays_json_safe(self):
        for index, literal in enumerate(('NaN', 'Infinity', '-Infinity', '1e999')):
            with self.subTest(literal=literal):
                run = make_run(self.root, 'run-lit-%d' % index)
                text = (run / 'tests.json').read_text()
                self.assertIn('"passed": true', text)
                (run / 'tests.json').write_text(
                    text.replace('"passed": true', '"passed": %s' % literal, 1))
                package = e.review_package(run)
                self.assertFalse(package['complete'])
                self.assertTrue(any(i['kind'] == 'invalid' for i in package['issues']))
                json.dumps(package, ensure_ascii=False, allow_nan=False)
                result = cli('bundle', '--run', str(run))
                self.assertEqual(result.returncode, 0, result.stderr)
                parsed = json.loads(result.stdout)
                self.assertFalse(_nonfinite(parsed))
                self.assertFalse(parsed['complete'])

    def test_nonfinite_manifest_constants_are_rejected(self):
        run = make_run(self.root)
        for literal in ('NaN', '1e999'):
            with self.subTest(literal=literal):
                path = self.root / ('manifest-%s.json' % literal)
                path.write_text(json.dumps(self.manifest_value([run]))[:-1] +
                                ', "note": %s}' % literal)
                with self.assertRaises(ValueError):
                    e.recovery_index(path)
                result = cli('index', '--manifest', str(path), '--output', str(self.root / 'out.json'))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertFalse((self.root / 'out.json').exists())


class FingerprintRecoveryTests(EvidenceToolBase):
    def test_missing_intermediate_workflow_file_keeps_the_fingerprint_conflict_visible(self):
        run = make_run(self.root)
        (run / 'applied.json').unlink()
        meta = json.loads((run / 'metadata.json').read_text())
        meta['applied_fingerprint'] = 'a' * 64
        write(run, 'metadata.json', meta)
        tests = json.loads((run / 'tests.json').read_text())
        tests['fingerprint'] = 'b' * 64
        write(run, 'tests.json', tests)
        review = json.loads((run / 'review.json').read_text())
        review['fingerprint'] = 'b' * 64
        write(run, 'review.json', review)
        entry = _index_entry(self.root, run, name='conflict-manifest.json')
        self.assertFalse(entry['evidence_complete'])
        conflicts = [i['detail'] for i in entry['issues'] if i['kind'] == 'conflict']
        self.assertTrue(any('metadata.applied_fingerprint' in d and 'tests.fingerprint' in d
                            for d in conflicts), conflicts)
        self.assertTrue(any(i['kind'] == 'gap' and 'applied.json' in i['detail']
                            for i in entry['issues']))
        package = e.review_package(run)
        self.assertFalse(package['complete'])
        self.assertTrue(any(i['kind'] == 'conflict' for i in package['issues']))

    def test_missing_optional_workflow_gap_blocks_completeness(self):
        run = make_run(self.root)
        (run / 'proposal.json').unlink()
        entry = _index_entry(self.root, run, name='gap-manifest.json')
        self.assertFalse(entry['evidence_complete'])
        self.assertTrue(any(i['kind'] == 'gap' and 'proposal.json' in i['detail']
                            for i in entry['issues']))
        self.assertFalse(e.review_package(run)['complete'])


LEDGER_COMMAND = (
    'umask 077\n'
    'team_dev_log_dir=$(mktemp -d /run/worker-test-logs/check-XXXXXXXX) || exit 125\n'
    "printf 'WORKER_TEST_LOG_DIR=%s\\n' \"$team_dev_log_dir\" || exit 125\n"
    'if env PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m unittest discover -s scripts '
    '-p test_evidence_tools.py -v >"$team_dev_log_dir/stdout.log" '
    '2>"$team_dev_log_dir/stderr.log"; then\n'
    '  team_dev_test_exit=0\n'
    'else\n'
    '  team_dev_test_exit=$?\n'
    'fi\n'
    "printf '%s\\n' \"$team_dev_test_exit\" >\"$team_dev_log_dir/exit-code\" || exit 125\n"
    'exit "$team_dev_test_exit"')
LEDGER_HASH = sha256(LEDGER_COMMAND.strip().encode('utf-8', 'surrogatepass'))


def write_jsonl(root, name, records):
    (root / name).write_bytes(b''.join(
        (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode()
        for record in records))


def start_record(ident, command, **extra):
    args = {'command': command}
    args.update(extra)
    return {'version': 1, 'type': 'tool_execution_start', 'id': ident, 'args': args}


def end_record(ident, text=None, is_error=False, exit_code=None, details=None):
    payload = {'timeoutSeconds': 120, 'wallTimeMs': 41}
    if exit_code is not None:
        payload['exitCode'] = exit_code
    payload.update(details or {})
    return {'version': 1, 'type': 'tool_execution_end', 'id': ident, 'is_error': is_error,
            'result': {'content': [] if text is None else [{'type': 'text', 'text': text}],
                       'details': payload}}


def decision_record(ident, command, status='allowed', denials=0):
    return {'type': 'decision', 'id': ident, 'args': {'command': command}, 'status': status,
            'decision': 'allow' if status == 'allowed' else 'block', 'denials': denials}


def marker_line(check):
    return 'WORKER_TEST_LOG_DIR=%s\n' % check


class InvocationLedgerTests(EvidenceToolBase):
    def ledger_run(self, name='ledger-run'):
        run = self.root / name
        run.mkdir()
        return run

    def task_for(self, run, hashes=(LEDGER_HASH,), policy=True):
        value = {'target': {'transport': 'local', 'host': None, 'repo': '/repo'},
                 'goal': 'Build the read-only invocation ledger',
                 'acceptance': ['A1'], 'interface_contracts': [],
                 'allowed_paths': ['scripts/evidence_tools.py'],
                 'test_commands': [['/usr/bin/python3', '-B', '-m', 'unittest', 'discover',
                                    '-s', 'scripts']]}
        if policy:
            value['worker_bash_policy'] = {'version': 2, 'allowed_command_sha256': list(hashes)}
        write(run, 'task.json', value)

    def check(self, run, token='AAAABBBB', exit_code=0, stdout=b'OK\n', stderr=b''):
        root = run / 'worker-test-logs'
        root.mkdir(exist_ok=True)
        check = root / ('check-' + token)
        check.mkdir(exist_ok=True)
        (check / 'exit-code').write_bytes(('%d\n' % exit_code).encode())
        (check / 'stdout.log').write_bytes(stdout)
        (check / 'stderr.log').write_bytes(stderr)
        return check

    def test_invocations_public_function_and_cli_are_available(self):
        self.assertTrue(hasattr(e, 'invocation_ledger'), 'invocation_ledger public function is required')
        self.assertTrue(hasattr(e, 'LEDGER_JSONL_LIMIT'), 'ledger input bounds are required')
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run)
        write_jsonl(run, 'tool-evidence.jsonl',
                    [start_record('call-1', LEDGER_COMMAND), end_record('call-1', marker_line(check))])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('call-1', LEDGER_COMMAND)])
        result = cli('invocations', '--run', str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        ledger = json.loads(result.stdout)
        self.assertEqual(ledger['tool'], 'evidence_tools.invocation_ledger')
        self.assertEqual([row['tool_call_id'] for row in ledger['rows']], ['call-1'])
        self.assertIs(ledger['acceptance_certified'], False)
        self.assertEqual(e.invocation_ledger(run)['invocations'], 1)

    def test_identical_red_and_green_commands_keep_their_own_retained_exit(self):
        run = self.ledger_run()
        self.task_for(run)
        red = self.check(run, 'REDD1111', exit_code=1, stdout=b'Ran 17 tests\nFAILED (failures=1)\n')
        green = self.check(run, 'GREE2222', exit_code=0, stdout=b'Ran 17 tests\nOK\n')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-red', LEDGER_COMMAND),
            end_record('call-red', marker_line(red), is_error=True),
            start_record('call-green', LEDGER_COMMAND),
            end_record('call-green', marker_line(green))])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('call-red', LEDGER_COMMAND),
                                                    decision_record('call-green', LEDGER_COMMAND)])
        ledger = e.invocation_ledger(run)
        rows = ledger['rows']
        self.assertEqual([row['row_index'] for row in rows], [0, 1])
        self.assertEqual([row['tool_call_id'] for row in rows], ['call-red', 'call-green'])
        self.assertEqual([row['command_sha256'] for row in rows], [LEDGER_HASH, LEDGER_HASH])
        self.assertIs(rows[0]['command_match'], True)
        self.assertEqual([row['exit_code'] for row in rows], [1, 0])
        self.assertEqual([row['terminal_exit_code'] for row in rows], [e.UNKNOWN, e.UNKNOWN])
        self.assertIs(rows[0]['recorded_is_error'], True)
        self.assertEqual(rows[0]['log_dir'], str(red))
        self.assertEqual(rows[0]['logs']['stdout.log']['sha256'],
                         sha256(b'Ran 17 tests\nFAILED (failures=1)\n'))
        self.assertEqual(rows[1]['logs']['stdout.log']['sha256'], sha256(b'Ran 17 tests\nOK\n'))
        self.assertTrue(all(row['binding_complete'] for row in rows), [r['issues'] for r in rows])
        self.assertEqual(ledger['invocations'], 2)
        self.assertEqual(ledger['retained_requests'], 2)
        self.assertTrue(ledger['complete'], ledger['issues'])
        self.assertEqual(LEDGER_HASH, sha256(LEDGER_COMMAND.strip().encode('utf-8', 'surrogatepass')))

    def test_worker_narrative_is_never_consulted(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'NARR9999')
        write_jsonl(run, 'tool-evidence.jsonl',
                    [start_record('call-1', LEDGER_COMMAND), end_record('call-1', marker_line(check))])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('call-1', LEDGER_COMMAND)])
        write(run, 'worker-result.json', {'final_text': 'one invocation, every command matched %s' % MARKER,
                                          'command_audit': {'bash_calls': 7}})
        document = json.dumps(e.invocation_ledger(run))
        self.assertNotIn(MARKER, document)
        self.assertNotIn('worker-result.json', document)
        self.assertEqual(e.invocation_ledger(run)['invocations'], 1)
        result = cli('invocations', '--run', str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(MARKER, result.stdout)

    def test_denied_missing_orphan_duplicate_and_conflicting_rows_stay_explicit(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'DENY1111')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-dup', LEDGER_COMMAND),
            start_record('call-dup', LEDGER_COMMAND + ' extra'),
            end_record('call-dup', marker_line(check)),
            start_record('call-missing-end', LEDGER_COMMAND),
            end_record('call-orphan-end', marker_line(check)),
            start_record('call-conflict', LEDGER_COMMAND),
            end_record('call-conflict', marker_line(check))])
        write_jsonl(run, 'bash-gate-events.jsonl', [
            decision_record('call-denied', 'wc -l README.md', status='denied', denials=1),
            decision_record('call-dup', LEDGER_COMMAND),
            decision_record('call-missing-end', LEDGER_COMMAND),
            decision_record('call-orphan-end', LEDGER_COMMAND),
            decision_record('call-conflict', LEDGER_COMMAND + ' --suffix')])
        ledger = e.invocation_ledger(run)
        self.assertEqual([row['tool_call_id'] for row in ledger['rows']],
                         ['call-dup', 'call-missing-end', 'call-conflict', 'call-denied',
                          'call-orphan-end'])
        self.assertEqual([row['order_basis'] for row in ledger['rows']],
                         ['tool_journal_start', 'tool_journal_start', 'tool_journal_start',
                          'gate_journal_decision', 'gate_journal_decision'])
        rows = {row['tool_call_id']: row for row in ledger['rows']}
        denied = rows['call-denied']
        self.assertEqual(denied['gate_status'], 'denied')
        self.assertIs(denied['executed'], False)
        self.assertIs(denied['command_match'], False)
        self.assertEqual(denied['log_dir'], e.UNKNOWN)
        self.assertEqual(denied['exit_code'], e.UNKNOWN)
        self.assertTrue(denied['binding_complete'], denied['issues'])
        self.assertTrue(any(i['kind'] == 'denied' and i['severity'] == 'note' for i in denied['issues']))
        duplicate = rows['call-dup']
        self.assertTrue(any(i['kind'] == 'duplicate' for i in duplicate['issues']))
        self.assertEqual(duplicate['command_sha256'], LEDGER_HASH)
        self.assertFalse(duplicate['binding_complete'])
        self.assertEqual(duplicate['executed'], e.UNKNOWN)
        missing = rows['call-missing-end']
        self.assertIs(missing['end_present'], False)
        self.assertIs(missing['terminal_result_present'], False)
        self.assertTrue(any(i['kind'] == 'orphan' for i in missing['issues']))
        self.assertFalse(missing['binding_complete'])
        self.assertEqual(missing['executed'], e.UNKNOWN)
        orphan = rows['call-orphan-end']
        self.assertIs(orphan['start_present'], False)
        self.assertTrue(any(i['kind'] == 'orphan' for i in orphan['issues']))
        self.assertFalse(orphan['binding_complete'])
        self.assertEqual(orphan['executed'], e.UNKNOWN)
        conflict = rows['call-conflict']
        self.assertIs(conflict['args_agree'], False)
        self.assertTrue(any(i['kind'] == 'conflict' for i in conflict['issues']))
        self.assertFalse(conflict['binding_complete'])
        self.assertEqual(conflict['executed'], e.UNKNOWN)
        self.assertEqual(ledger['invocations'], 0)
        self.assertEqual(ledger['observed_starts'], 3)
        self.assertEqual(ledger['retained_requests'], 5)
        self.assertFalse(ledger['complete'])

    def test_async_shapes_are_unsupported_and_never_complete(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'ASYNC111')
        write_jsonl(run, 'tool-evidence.jsonl', [
            {'version': 1, 'type': 'async_result', 'message': {'job': MARKER}},
            start_record('call-async', LEDGER_COMMAND, **{'async': True}),
            end_record('call-async', marker_line(check), details={'jobId': 'bg_1'})])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('call-async', LEDGER_COMMAND)])
        ledger = e.invocation_ledger(run)
        self.assertFalse(ledger['complete'])
        self.assertIn('unsupported', [issue['kind'] for issue in ledger['issues']])
        row = ledger['rows'][0]
        self.assertTrue(any(i['kind'] == 'unsupported' for i in row['issues']))
        self.assertFalse(row['binding_complete'])
        self.assertNotIn(MARKER, json.dumps(ledger))

    def test_shared_log_directory_is_ambiguous_for_both_rows(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'SHAR1111')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-a', LEDGER_COMMAND), end_record('call-a', marker_line(check)),
            start_record('call-b', LEDGER_COMMAND), end_record('call-b', marker_line(check))])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('call-a', LEDGER_COMMAND),
                                                    decision_record('call-b', LEDGER_COMMAND)])
        ledger = e.invocation_ledger(run)
        self.assertEqual(len(ledger['rows']), 2)
        for row in ledger['rows']:
            self.assertTrue(any(i['kind'] == 'conflict' for i in row['issues']), row['issues'])
            self.assertFalse(row['binding_complete'])
            self.assertEqual(row['executed'], e.UNKNOWN)
        self.assertEqual([row['exit_code'] for row in ledger['rows']], [0, 0])
        self.assertFalse(ledger['complete'])

    def test_exit_conflict_and_ambiguous_markers_are_explicit(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'CONF1234', exit_code=3)
        other = self.check(run, 'MULT5678')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-conflict', LEDGER_COMMAND),
            end_record('call-conflict', marker_line(check), exit_code=0),
            start_record('call-markers', LEDGER_COMMAND),
            end_record('call-markers', marker_line(check) + marker_line(other))])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('call-conflict', LEDGER_COMMAND),
                                                    decision_record('call-markers', LEDGER_COMMAND)])
        ledger = e.invocation_ledger(run)
        conflict, ambiguous = ledger['rows']
        self.assertEqual(conflict['exit_code'], 3)
        self.assertEqual(conflict['terminal_exit_code'], 0)
        self.assertTrue(any(i['kind'] == 'conflict' for i in conflict['issues']))
        self.assertFalse(conflict['binding_complete'])
        self.assertEqual(ambiguous['log_dir'], e.UNKNOWN)
        self.assertEqual(ambiguous['log_status'], 'invalid')
        self.assertTrue(any(i['kind'] == 'ambiguous' for i in ambiguous['issues']))
        self.assertFalse(ambiguous['binding_complete'])
        self.assertFalse(ledger['complete'])

    def test_jsonl_bounds_and_strict_records_stay_incomplete(self):
        oversized = self.ledger_run('run-oversized')
        self.task_for(oversized)
        (oversized / 'tool-evidence.jsonl').write_bytes(
            b'{"type":"tool_execution_start"}' + b' ' * (e.LEDGER_JSONL_LIMIT + 1) + b'\n')
        write_jsonl(oversized, 'bash-gate-events.jsonl', [])
        ledger = e.invocation_ledger(oversized)
        self.assertEqual(ledger['rows'], [])
        self.assertFalse(ledger['complete'])
        self.assertTrue(any(i['kind'] == 'evidence' and 'oversized' in i['detail']
                            for i in ledger['issues']))

        strict = self.ledger_run('run-strict')
        self.task_for(strict)
        (strict / 'tool-evidence.jsonl').write_text(
            '{"type":"tool_execution_start","id":"a","id":"b"}\n'
            '{"type":"tool_execution_start","id":"c","args":{"command":NaN}}\n'
            '[1, 2, 3]\n'
            '{"type":"tool_execution_start"\n')
        write_jsonl(strict, 'bash-gate-events.jsonl', [])
        ledger = e.invocation_ledger(strict)
        self.assertEqual(ledger['rows'], [])
        self.assertFalse(ledger['complete'])
        details = ' '.join(i['detail'] for i in ledger['issues'])
        self.assertIn('strict JSON', details)
        self.assertIn('not a JSON object', details)

        limit = self.ledger_run('run-limit')
        self.task_for(limit)
        write_jsonl(limit, 'tool-evidence.jsonl',
                    [start_record('call-%04d' % index, LEDGER_COMMAND)
                     for index in range(e.LEDGER_MAX_RECORDS + 1)])
        write_jsonl(limit, 'bash-gate-events.jsonl',
                    [decision_record('call-%04d' % index, LEDGER_COMMAND)
                     for index in range(e.LEDGER_MAX_RECORDS + 1)])
        ledger = e.invocation_ledger(limit)
        self.assertEqual(len(ledger['rows']), e.LEDGER_MAX_INVOCATIONS)
        limits = ' '.join(i['detail'] for i in ledger['issues'] if i['kind'] == 'limit')
        self.assertIn('records', limits)
        self.assertIn('invocation IDs', limits)

    def test_log_directory_boundaries_reject_symlink_fifo_and_outside_paths(self):
        outside = self.root / 'outside'
        outside.mkdir()
        decoy = outside / 'check-AAAA1111'
        decoy.mkdir()
        (decoy / 'exit-code').write_bytes(b'0\n')
        (decoy / 'stdout.log').write_bytes(('leak %s\n' % MARKER).encode())
        (decoy / 'stderr.log').write_bytes(b'')

        linked = self.ledger_run('run-linked')
        self.task_for(linked)
        (linked / 'worker-test-logs').symlink_to(outside, target_is_directory=True)
        write_jsonl(linked, 'tool-evidence.jsonl', [
            start_record('call-linked', LEDGER_COMMAND),
            end_record('call-linked', marker_line(linked / 'worker-test-logs' / 'check-AAAA1111'))])
        write_jsonl(linked, 'bash-gate-events.jsonl', [decision_record('call-linked', LEDGER_COMMAND)])
        ledger = e.invocation_ledger(linked)
        row = ledger['rows'][0]
        self.assertTrue(any(i['kind'] == 'log' and 'symlink' in i['detail'] for i in row['issues']),
                        row['issues'])
        self.assertFalse(row['binding_complete'])
        self.assertNotIn(MARKER, json.dumps(ledger))

        inside = self.ledger_run('run-inside')
        self.task_for(inside)
        check = self.check(inside, 'LINK2222')
        (outside / 'decoy-exit').write_bytes(b'0\n')
        (check / 'exit-code').unlink()
        (check / 'exit-code').symlink_to(outside / 'decoy-exit')
        write_jsonl(inside, 'tool-evidence.jsonl', [
            start_record('call-leaf', LEDGER_COMMAND), end_record('call-leaf', marker_line(check))])
        write_jsonl(inside, 'bash-gate-events.jsonl', [decision_record('call-leaf', LEDGER_COMMAND)])
        row = e.invocation_ledger(inside)['rows'][0]
        self.assertEqual(row['logs']['exit-code']['status'], 'symlink')
        self.assertEqual(row['exit_code'], e.UNKNOWN)
        self.assertFalse(row['binding_complete'])

        runaway = self.ledger_run('run-runaway')
        self.task_for(runaway)
        write_jsonl(runaway, 'tool-evidence.jsonl', [
            start_record('call-away', LEDGER_COMMAND), end_record('call-away', marker_line(decoy))])
        write_jsonl(runaway, 'bash-gate-events.jsonl', [decision_record('call-away', LEDGER_COMMAND)])
        ledger = e.invocation_ledger(runaway)
        row = ledger['rows'][0]
        self.assertEqual(row['log_dir'], e.UNKNOWN)
        self.assertTrue(any(i['kind'] == 'path' for i in row['issues']))
        self.assertFalse(row['binding_complete'])
        self.assertNotIn(MARKER, json.dumps(ledger))

        huge = self.ledger_run('run-huge')
        self.task_for(huge)
        heavy = self.check(huge, 'HUGE1234')
        with (heavy / 'stdout.log').open('wb') as stream:
            stream.write(b'y' * (e.BLOB_LIMIT + 1))
        write_jsonl(huge, 'tool-evidence.jsonl', [
            start_record('call-huge', LEDGER_COMMAND), end_record('call-huge', marker_line(heavy))])
        write_jsonl(huge, 'bash-gate-events.jsonl', [decision_record('call-huge', LEDGER_COMMAND)])
        row = e.invocation_ledger(huge)['rows'][0]
        self.assertEqual(row['logs']['stdout.log']['status'], 'oversized')
        self.assertIs(row['logs']['stdout.log']['sha256'], None)
        self.assertEqual(row['exit_code'], 0)
        self.assertFalse(row['binding_complete'])

    def test_gate_ready_metadata_and_envelope_versions_stay_bounded(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'READY111')
        write_jsonl(run, 'tool-evidence.jsonl', [
            {'version': 2, 'type': 'tool_execution_start', 'id': 'call-version',
             'args': {'command': LEDGER_COMMAND}},
            start_record('call-1', LEDGER_COMMAND), end_record('call-1', marker_line(check))])
        write_jsonl(run, 'bash-gate-events.jsonl', [
            {'version': 1, 'type': 'ready', 'id': 'gate-ready', 'note': MARKER},
            decision_record('call-version', LEDGER_COMMAND),
            decision_record('call-1', LEDGER_COMMAND)])
        ledger = e.invocation_ledger(run)
        self.assertIs(ledger['gate_ready'], True)
        self.assertEqual([row['tool_call_id'] for row in ledger['rows']], ['call-1', 'call-version'])
        self.assertEqual([row['order_basis'] for row in ledger['rows']],
                         ['tool_journal_start', 'gate_journal_decision'])
        self.assertNotIn('gate-ready', [row['tool_call_id'] for row in ledger['rows']])
        self.assertIs(ledger['rows'][0]['binding_complete'], True)
        self.assertIs(ledger['rows'][1]['start_present'], False)
        self.assertIs(ledger['rows'][1]['binding_complete'], False)
        self.assertIn('unsupported', [issue['kind'] for issue in ledger['issues']])
        self.assertFalse(ledger['complete'])
        self.assertNotIn(MARKER, json.dumps(ledger))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO evidence requires os.mkfifo')
    def test_fifo_log_file_is_rejected_without_blocking(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'FIFO1234')
        (check / 'stdout.log').unlink()
        os.mkfifo(check / 'stdout.log')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-fifo', LEDGER_COMMAND), end_record('call-fifo', marker_line(check))])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('call-fifo', LEDGER_COMMAND)])
        result = cli('invocations', '--run', str(run))
        self.assertEqual(result.returncode, 0, result.stderr)
        ledger = json.loads(result.stdout)
        row = ledger['rows'][0]
        self.assertEqual(row['logs']['stdout.log']['status'], 'nonregular')
        self.assertFalse(row['binding_complete'])
        self.assertFalse(ledger['complete'])

    def test_ledger_is_read_only_and_output_stays_outside_the_run(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'STBL1111')
        write_jsonl(run, 'tool-evidence.jsonl',
                    [start_record('call-1', LEDGER_COMMAND), end_record('call-1', marker_line(check))])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('call-1', LEDGER_COMMAND)])
        before = tree_snapshot(self.root)
        run_before = tree_snapshot(run)
        first = e.invocation_ledger(run)
        self.assertTrue(first['complete'], first['issues'])
        self.assertEqual([source['file'] for source in first['sources']],
                         ['task.json', 'tool-evidence.jsonl', 'bash-gate-events.jsonl'])
        self.assertEqual(first['sources'][0]['sha256'], sha256((run / 'task.json').read_bytes()))
        self.assertEqual(first['sources'][1]['sha256'],
                         sha256((run / 'tool-evidence.jsonl').read_bytes()))
        self.assertEqual(first['sources'][2]['sha256'],
                         sha256((run / 'bash-gate-events.jsonl').read_bytes()))
        self.assertIs(first['current_liveness_verified'], False)
        self.assertIs(first['current_target_verified'], False)
        self.assertIs(first['acceptance_certified'], False)
        self.assertEqual(tree_snapshot(self.root), before)

        inside = cli('invocations', '--run', str(run), '--output', str(run / 'ledger.json'))
        self.assertNotEqual(inside.returncode, 0)
        self.assertEqual(inside.stdout, '')
        self.assertFalse((run / 'ledger.json').exists())
        replace = cli('invocations', '--run', str(run), '--output', str(run / 'task.json'))
        self.assertNotEqual(replace.returncode, 0)
        out = self.root / 'ledger.json'
        written = cli('invocations', '--run', str(run), '--output', str(out))
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(json.loads(written.stdout)['sha256'], sha256(out.read_bytes()))
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        content = out.read_bytes()
        again = cli('invocations', '--run', str(run), '--output', str(out))
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(out.read_bytes(), content)
        self.assertEqual(tree_snapshot(run), run_before)

    def test_allowed_binding_requires_marker_log_files_and_numeric_exit(self):
        run = self.ledger_run()
        self.task_for(run)
        complete = self.check(run, 'BIND1111')
        no_stderr = self.check(run, 'BIND2222')
        (no_stderr / 'stderr.log').unlink()
        no_exit = self.check(run, 'BIND3333')
        (no_exit / 'exit-code').unlink()
        bad_exit = self.check(run, 'BIND4444')
        (bad_exit / 'exit-code').write_bytes(b'green\n')
        spelled = self.check(run, 'BIND5555')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-complete', LEDGER_COMMAND),
            end_record('call-complete', marker_line(complete)),
            start_record('call-no-stderr', LEDGER_COMMAND),
            end_record('call-no-stderr', marker_line(no_stderr)),
            start_record('call-no-exit', LEDGER_COMMAND),
            end_record('call-no-exit', marker_line(no_exit)),
            start_record('call-bad-exit', LEDGER_COMMAND),
            end_record('call-bad-exit', marker_line(bad_exit)),
            start_record('call-no-marker', LEDGER_COMMAND),
            end_record('call-no-marker'),
            start_record('call-spelling', LEDGER_COMMAND),
            end_record('call-spelling', 'WORKER_TEST_LOG_DIR=%s (1.2s)\n' % spelled)])
        write_jsonl(run, 'bash-gate-events.jsonl',
                    [decision_record(ident, LEDGER_COMMAND)
                     for ident in ('call-complete', 'call-no-stderr', 'call-no-exit',
                                   'call-bad-exit', 'call-no-marker', 'call-spelling')])
        ledger = e.invocation_ledger(run)
        rows = {row['tool_call_id']: row for row in ledger['rows']}
        bound = rows['call-complete']
        self.assertEqual(bound['log_status'], 'bound')
        self.assertEqual({name: record['status'] for name, record in bound['logs'].items()},
                         {'exit-code': 'read', 'stdout.log': 'read', 'stderr.log': 'read'})
        self.assertEqual(bound['exit_code'], 0)
        self.assertIs(bound['executed'], True)
        self.assertTrue(bound['binding_complete'])
        for ident in ('call-no-stderr', 'call-no-exit', 'call-bad-exit', 'call-no-marker',
                      'call-spelling'):
            with self.subTest(ident=ident):
                row = rows[ident]
                self.assertIs(row['terminal_result_present'], True)
                self.assertEqual(row['executed'], e.UNKNOWN, row['issues'])
                self.assertFalse(row['binding_complete'])
                self.assertTrue(row['issues'], 'an incomplete binding must state an issue')
        self.assertEqual(rows['call-no-stderr']['logs']['stderr.log']['status'], 'missing')
        self.assertEqual(rows['call-no-exit']['exit_code'], e.UNKNOWN)
        self.assertEqual(rows['call-bad-exit']['exit_code'], e.UNKNOWN)
        self.assertEqual(rows['call-no-marker']['log_dir'], e.UNKNOWN)
        self.assertEqual(rows['call-spelling']['log_dir'], e.UNKNOWN)
        self.assertEqual(rows['call-spelling']['log_status'], 'invalid')
        self.assertTrue(any(i['kind'] == 'path' for i in rows['call-spelling']['issues']),
                        rows['call-spelling']['issues'])
        self.assertEqual(ledger['invocations'], 1)
        self.assertEqual(ledger['observed_starts'], 6)
        self.assertFalse(ledger['complete'])

    def test_execution_is_tri_state_and_counts_only_explicit_true(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'TRIS1111')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-bound', LEDGER_COMMAND), end_record('call-bound', marker_line(check)),
            start_record('call-open', LEDGER_COMMAND)])
        write_jsonl(run, 'bash-gate-events.jsonl', [
            decision_record('call-bound', LEDGER_COMMAND),
            decision_record('call-open', LEDGER_COMMAND),
            decision_record('call-denied', LEDGER_COMMAND, status='denied', denials=2)])
        ledger = e.invocation_ledger(run)
        rows = {row['tool_call_id']: row for row in ledger['rows']}
        self.assertIs(rows['call-bound']['executed'], True)
        self.assertEqual(rows['call-open']['executed'], e.UNKNOWN)
        self.assertIs(rows['call-open']['binding_complete'], False)
        self.assertIs(rows['call-denied']['executed'], False)
        self.assertIs(rows['call-denied']['binding_complete'], True)
        self.assertEqual({row['executed'] for row in ledger['rows']}, {True, False, e.UNKNOWN})
        self.assertEqual(ledger['invocations'], 1)
        self.assertEqual(ledger['observed_starts'], 2)
        self.assertEqual(ledger['retained_requests'], 3)
        self.assertFalse(ledger['complete'])

    def test_record_shapes_and_gate_coherence_gate_completeness(self):
        run = self.ledger_run()
        self.task_for(run)
        check = self.check(run, 'SHAP1111')
        write_jsonl(run, 'tool-evidence.jsonl', [
            {'version': True, 'type': 'tool_execution_start', 'id': 'call-bool-version',
             'args': {'command': LEDGER_COMMAND}},
            start_record('call-shape', LEDGER_COMMAND),
            end_record('call-shape', marker_line(check), is_error='yes'),
            start_record('call-empty', '   '),
            end_record('call-empty', marker_line(check)),
            start_record('call-nonstring', LEDGER_COMMAND),
            end_record('call-nonstring', marker_line(check), details={'exitCode': True})])
        write_jsonl(run, 'bash-gate-events.jsonl', [
            decision_record('call-bool-version', LEDGER_COMMAND),
            decision_record('call-shape', LEDGER_COMMAND),
            decision_record('call-empty', LEDGER_COMMAND),
            decision_record('call-nonstring', LEDGER_COMMAND),
            {'type': 'decision', 'id': 'call-pair', 'args': {'command': LEDGER_COMMAND},
             'status': 'allowed', 'decision': 'block', 'denials': 0},
            {'type': 'decision', 'id': 'call-denials', 'args': {'command': LEDGER_COMMAND},
             'status': 'denied', 'decision': 'block', 'denials': 'many'},
            {'type': 'decision', 'id': 'call-zero-denials', 'args': {'command': LEDGER_COMMAND},
             'status': 'denied', 'decision': 'block', 'denials': 0},
            {'type': 'decision', 'id': 'call-no-denials', 'args': {'command': LEDGER_COMMAND},
             'status': 'denied', 'decision': 'block'}])
        ledger = e.invocation_ledger(run)
        rows = {row['tool_call_id']: row for row in ledger['rows']}
        self.assertIn('unsupported', [issue['kind'] for issue in ledger['issues']])
        versioned = rows['call-bool-version']
        self.assertIs(versioned['start_present'], False)
        self.assertEqual(versioned['executed'], e.UNKNOWN)
        self.assertFalse(versioned['binding_complete'])
        shaped = rows['call-shape']
        self.assertEqual(shaped['recorded_is_error'], e.UNKNOWN)
        self.assertEqual(shaped['executed'], e.UNKNOWN, shaped['issues'])
        self.assertFalse(shaped['binding_complete'])
        self.assertTrue(any(i['kind'] == 'record' for i in shaped['issues']))
        empty = rows['call-empty']
        self.assertEqual(empty['command_sha256'], e.UNKNOWN)
        self.assertEqual(empty['command_source'], 'gate')
        self.assertIs(empty['args_agree'], False)
        self.assertEqual(empty['executed'], e.UNKNOWN)
        self.assertFalse(empty['binding_complete'])
        nonstring = rows['call-nonstring']
        self.assertEqual(nonstring['terminal_exit_code'], e.UNKNOWN)
        self.assertEqual(nonstring['exit_code'], 0)
        self.assertEqual(nonstring['executed'], e.UNKNOWN)
        self.assertFalse(nonstring['binding_complete'])
        self.assertEqual(rows['call-pair']['gate_status'], e.UNKNOWN)
        self.assertEqual(rows['call-denials']['gate_denials'], e.UNKNOWN)
        self.assertEqual(rows['call-no-denials']['gate_denials'], e.UNKNOWN)
        # A zero-count denial is now an incoherent pair, not a recognized block.
        self.assertEqual(rows['call-zero-denials']['gate_status'], e.UNKNOWN)
        self.assertEqual(rows['call-zero-denials']['gate_denials'], 0)
        for ident in ('call-pair', 'call-denials', 'call-zero-denials', 'call-no-denials'):
            with self.subTest(ident=ident):
                row = rows[ident]
                self.assertEqual(row['executed'], e.UNKNOWN, row['issues'])
                self.assertFalse(row['binding_complete'])
        self.assertFalse(ledger['complete'])
        self.assertNotIn(LEDGER_COMMAND, json.dumps(ledger))

    def test_async_native_and_malformed_markers_stay_unsupported(self):
        run = self.ledger_run()
        self.task_for(run)
        native_dir = self.check(run, 'ASYNC222')
        partial_dir = self.check(run, 'ASYNC333')
        string_dir = self.check(run, 'ASYNC444')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-native', LEDGER_COMMAND),
            end_record('call-native', marker_line(native_dir),
                       details={'async': {'jobId': 'bg_7', 'type': 'bash', 'state': 'running'}}),
            start_record('call-partial', LEDGER_COMMAND),
            end_record('call-partial', marker_line(partial_dir), details={'async': {'jobId': 'bg_8'}}),
            start_record('call-string', LEDGER_COMMAND),
            end_record('call-string', marker_line(string_dir), details={'async': 'bg_9'})])
        write_jsonl(run, 'bash-gate-events.jsonl',
                    [decision_record(ident, LEDGER_COMMAND)
                     for ident in ('call-native', 'call-partial', 'call-string')])
        ledger = e.invocation_ledger(run)
        self.assertEqual(len(ledger['rows']), 3)
        for row in ledger['rows']:
            with self.subTest(ident=row['tool_call_id']):
                self.assertTrue(any(i['kind'] == 'unsupported' for i in row['issues']), row['issues'])
                self.assertEqual(row['executed'], e.UNKNOWN)
                self.assertFalse(row['binding_complete'])
                self.assertEqual(row['exit_code'], 0)
        self.assertFalse(ledger['complete'])
        document = json.dumps(ledger)
        self.assertNotIn('bg_7', document)
        self.assertNotIn('bg_9', document)
        self.assertNotIn('jobId', document)

    def test_raw_and_policy_command_hashes_stay_separate(self):
        run = self.ledger_run()
        self.task_for(run)
        padded = '  %s  ' % LEDGER_COMMAND
        check = self.check(run, 'HASH1111')
        mismatch_dir = self.check(run, 'HASH2222')
        write_jsonl(run, 'tool-evidence.jsonl', [
            start_record('call-padded', padded), end_record('call-padded', marker_line(check)),
            start_record('call-mismatch', LEDGER_COMMAND),
            end_record('call-mismatch', marker_line(mismatch_dir))])
        write_jsonl(run, 'bash-gate-events.jsonl', [
            decision_record('call-padded', padded),
            decision_record('call-mismatch', LEDGER_COMMAND + ' --suffix'),
            decision_record('call-gate-only', padded, status='denied', denials=1)])
        ledger = e.invocation_ledger(run)
        rows = {row['tool_call_id']: row for row in ledger['rows']}
        padded_row = rows['call-padded']
        self.assertEqual(padded_row['command_sha256'],
                         sha256(padded.encode('utf-8', 'surrogatepass')))
        self.assertEqual(padded_row['policy_command_sha256'], LEDGER_HASH)
        self.assertNotEqual(padded_row['command_sha256'], padded_row['policy_command_sha256'])
        self.assertEqual(padded_row['command_source'], 'request')
        self.assertIs(padded_row['command_match'], True)
        self.assertIs(padded_row['executed'], True)
        self.assertTrue(padded_row['binding_complete'])
        mismatch = rows['call-mismatch']
        self.assertEqual(mismatch['command_source'], 'request')
        self.assertEqual(mismatch['command_sha256'], LEDGER_HASH)
        self.assertEqual(mismatch['policy_command_sha256'], LEDGER_HASH)
        self.assertIs(mismatch['args_agree'], False)
        self.assertFalse(mismatch['binding_complete'])
        gate_only = rows['call-gate-only']
        self.assertEqual(gate_only['command_source'], 'gate')
        self.assertEqual(gate_only['command_sha256'], e.UNKNOWN)
        self.assertEqual(gate_only['policy_command_sha256'], LEDGER_HASH)
        self.assertIs(gate_only['command_match'], True)
        self.assertIs(gate_only['executed'], False)
        self.assertTrue(gate_only['binding_complete'])
        self.assertNotIn(LEDGER_COMMAND, json.dumps(ledger))

    def test_native_gate_blocks_keep_wrapper_records_without_log_requirements(self):
        run = self.ledger_run()
        self.task_for(run)
        task = json.loads((run / 'task.json').read_text())
        task['worker_bash_gate'] = {'max_denials': 3}
        write(run, 'task.json', task)
        tools, gates = [], []
        fixtures = [
            ('undeclared', 'undeclared_command', 1, {'command': 'wc -l fixture'}),
            ('invalid', 'invalid_arguments', 2, {'command': LEDGER_COMMAND, 'timeout': 0}),
            ('exhausted', 'allowed', 3, {'command': LEDGER_COMMAND}),
        ]
        for ident, status, denials, args in fixtures:
            tools += [{'version': 1, 'type': 'tool_execution_start', 'id': ident, 'args': args},
                      end_record(ident, 'Command was NOT executed.', is_error=True, details={})]
            gates.append({'type': 'decision', 'id': ident, 'args': args, 'status': status,
                          'decision': 'block', 'denials': denials})
        write_jsonl(run, 'tool-evidence.jsonl', tools)
        write_jsonl(run, 'bash-gate-events.jsonl', gates)
        ledger = e.invocation_ledger(run)
        self.assertTrue(ledger['complete'], ledger['rows'])
        self.assertEqual(ledger['invocations'], 0)
        self.assertEqual(ledger['observed_starts'], 3)
        self.assertEqual([row['gate_status'] for row in ledger['rows']],
                         ['undeclared_command', 'invalid_arguments', 'allowed'])
        for row in ledger['rows']:
            self.assertIs(row['executed'], False)
            self.assertTrue(row['binding_complete'])
            self.assertEqual(row['logs'], {})
            self.assertEqual(row['exit_code'], e.UNKNOWN)

    def test_native_unavailable_request_retains_nonexecution_and_missing_argument_gap(self):
        run = self.ledger_run()
        self.task_for(run)
        write_jsonl(run, 'tool-evidence.jsonl', [
            {'version': 1, 'type': 'tool_execution_start', 'id': 'bad', 'args': {}},
            end_record('bad', 'Command was NOT executed.', is_error=True)])
        write_jsonl(run, 'bash-gate-events.jsonl', [
            {'type': 'decision', 'id': 'bad', 'args': {}, 'status': 'unavailable',
             'decision': 'block', 'denials': 1}])
        ledger = e.invocation_ledger(run)
        self.assertFalse(ledger['complete'])
        self.assertIs(ledger['rows'][0]['executed'], False)
        self.assertFalse(ledger['rows'][0]['binding_complete'])

    def test_gate_exhaustion_requires_the_saved_limit_and_consistent_decision(self):
        for index, (maximum, denials, decision) in enumerate([
                (None, 3, 'block'), (True, 3, 'block'), (3, 2, 'block'), (3, 3, 'allow')]):
            with self.subTest(maximum=maximum, denials=denials, decision=decision):
                run = self.ledger_run('exhaustion-%d' % index)
                self.task_for(run)
                task = json.loads((run / 'task.json').read_text())
                if maximum is not None:
                    task['worker_bash_gate'] = {'max_denials': maximum}
                write(run, 'task.json', task)
                check = self.check(run)
                write_jsonl(run, 'tool-evidence.jsonl', [start_record('a', LEDGER_COMMAND),
                            end_record('a', marker_line(check))])
                gate = decision_record('a', LEDGER_COMMAND, denials=denials)
                gate['decision'] = decision
                write_jsonl(run, 'bash-gate-events.jsonl', [gate])
                ledger = e.invocation_ledger(run)
                self.assertFalse(ledger['complete'])
                self.assertEqual(ledger['rows'][0]['executed'], e.UNKNOWN)

    def test_terminal_details_container_is_required_but_exit_code_is_optional(self):
        for index, value in enumerate(['missing', None, [], 'invalid', {}, {'exitCode': None}]):
            with self.subTest(details=value):
                run = self.ledger_run('details-%d' % index)
                self.task_for(run)
                check = self.check(run)
                end = end_record('a', marker_line(check))
                if value == 'missing':
                    del end['result']['details']
                else:
                    end['result']['details'] = value
                write_jsonl(run, 'tool-evidence.jsonl', [start_record('a', LEDGER_COMMAND), end])
                write_jsonl(run, 'bash-gate-events.jsonl', [decision_record('a', LEDGER_COMMAND)])
                ledger = e.invocation_ledger(run)
                row = ledger['rows'][0]
                valid = value == {}
                self.assertIs(ledger['complete'], valid)
                self.assertIs(row['binding_complete'], valid)
                self.assertEqual(row['executed'], True if valid else e.UNKNOWN)
                self.assertEqual(row['terminal_exit_code'], e.UNKNOWN)
                self.assertEqual(row['exit_code'], 0)

    def test_observed_starts_keep_tool_order_and_cross_journal_order_is_unknown(self):
        run = self.ledger_run()
        self.task_for(run)
        first = self.check(run, 'ORDER111')
        second = self.check(run, 'ORDER222')
        orphan_dir = self.check(run, 'ORDER333')
        write_jsonl(run, 'tool-evidence.jsonl', [
            end_record('call-orphan', marker_line(orphan_dir)),
            start_record('call-b', LEDGER_COMMAND), end_record('call-b', marker_line(first)),
            start_record('call-a', LEDGER_COMMAND), end_record('call-a', marker_line(second))])
        write_jsonl(run, 'bash-gate-events.jsonl', [
            decision_record('call-denied', LEDGER_COMMAND, status='denied', denials=1),
            decision_record('call-a', LEDGER_COMMAND),
            decision_record('call-b', LEDGER_COMMAND)])
        ledger = e.invocation_ledger(run)
        self.assertEqual([row['tool_call_id'] for row in ledger['rows']],
                         ['call-b', 'call-a', 'call-denied', 'call-orphan'])
        self.assertEqual([row['order_basis'] for row in ledger['rows']],
                         ['tool_journal_start', 'tool_journal_start', 'gate_journal_decision',
                          'tool_journal_end'])
        self.assertEqual(ledger['rows'][0]['log_dir'], str(first))
        self.assertEqual(ledger['rows'][1]['log_dir'], str(second))
        self.assertEqual(ledger['ordering']['cross_journal_chronology'], e.UNKNOWN)
        self.assertEqual(ledger['invocations'], 2)
        self.assertEqual(ledger['observed_starts'], 2)
        self.assertTrue(ledger['rows'][2]['binding_complete'])
        self.assertFalse(ledger['rows'][3]['binding_complete'])
        self.assertFalse(ledger['complete'])


if __name__ == '__main__':
    unittest.main()
