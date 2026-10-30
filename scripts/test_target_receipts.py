"""Behavioral tests for append-only target-test invocation receipts.

The synthetic run fixture reuses the real BridgeTests setup from the existing
bridge suite through a module alias; it never runs or inherits that suite.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import target_test_receipts as receipts


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


support = _module('target_receipts_bridge_support', 'test_team_dev.py')
m = support.m


def tree_bytes(run):
    run = Path(run)
    state = {}
    for path in sorted(run.rglob('*')):
        name = str(path.relative_to(run))
        if path.name == '.operation-lock':
            continue
        if path.is_dir():
            state[name + '/'] = b''
        elif path.is_file() and not path.is_symlink():
            state[name] = path.read_bytes()
    return state


def attempt_directory(run, attempt_id):
    return Path(run) / receipts.ATTEMPTS / attempt_id


class SyntheticBridge:
    """Owned temporary run; every process and file stays under its own root."""

    def __init__(self):
        self.case = support.BridgeTests(methodName='runTest')
        self.case.setUp()
        self.root = self.case.root
        self.repo = self.case.repo
        self.task = self.case.task

    def cleanup(self):
        self.case.doCleanups()

    def applied(self, commands=None):
        if commands is not None:
            self.case.task['test_commands'] = commands
        self.case.change()
        m.apply(self.case.run)
        return self.case.run


class TargetReceiptTests(unittest.TestCase):
    def bridge(self):
        fixture = SyntheticBridge()
        self.addCleanup(fixture.cleanup)
        return fixture

    def test_post_manifest_failure_retains_two_local_command_results_and_blocks_replay(self):
        bridge = self.bridge()
        commands = [[sys.executable, '-B', '-c', 'print("FIRST7"); raise SystemExit(7)'],
                    [sys.executable, '-B', '-c', 'print("SECOND9"); raise SystemExit(9)']]
        run = bridge.applied(commands)

        launches = []
        real_local = m.local_test

        def observe(argv, cwd, seconds):
            if len(launches) == 1:
                # The second launch must already observe the first durable receipt.
                recorded = receipts.test_receipts(run)['attempts']
                self.assertEqual(len(recorded), 1)
                directory = attempt_directory(run, recorded[0]['attempt_id'])
                self.assertTrue((directory / receipts.command_result_name(0)).is_file())
                self.assertEqual(recorded[0]['commands'][0]['exit_state'], 'known')
                # The second command's launch intent is durable before it starts.
                self.assertEqual(recorded[0]['commands'][1]['status'], 'intent_without_result')
            launches.append(list(argv))
            return real_local(argv, cwd, seconds)

        real_manifest = m.Repo.manifest
        manifests = []

        def manifest(repo, paths):
            manifests.append(list(paths))
            if len(manifests) == 2:
                raise RuntimeError('post-manifest fixture failure')
            return real_manifest(repo, paths)

        with mock.patch.object(m, 'local_test', side_effect=observe), \
                mock.patch.object(m.Repo, 'manifest', autospec=True, side_effect=manifest):
            with self.assertRaisesRegex(RuntimeError, 'post-manifest fixture failure'):
                m.test_target(run)

        self.assertEqual(len(launches), 2)
        state = receipts.test_receipts(run)
        self.assertEqual(len(state['attempts']), 1)
        attempt = state['attempts'][0]
        self.assertEqual(attempt['state'], 'incomplete')
        self.assertEqual([c['declared_argv'] for c in attempt['commands']], commands)
        self.assertEqual([c['declared_exit_code'] for c in attempt['commands']], [7, 9])
        self.assertEqual([c['exit_state'] for c in attempt['commands']], ['known', 'known'])
        for index, command in enumerate(commands):
            self.assertEqual(attempt['commands'][index]['actual_argv'],
                             ['env', 'PYTHONDONTWRITEBYTECODE=1'] + command)
        directory = attempt_directory(run, attempt['attempt_id'])
        intent = json.loads((directory / receipts.INTENT).read_bytes())
        self.assertEqual(intent['declared_commands'], commands)
        self.assertEqual(intent['task_sha256'], m.digest((Path(run) / 'task.json').read_bytes()))
        self.assertEqual(intent['applied_sha256'], m.digest((Path(run) / 'applied.json').read_bytes()))
        applied = m.load(Path(run) / 'applied.json')
        self.assertEqual(intent['applied_fingerprint'], applied['fingerprint'])
        self.assertEqual(intent['identity'], applied['source'])
        for index, marker in ((0, b'FIRST7'), (1, b'SECOND9')):
            log = directory / receipts.command_log_name(index, 'stdout')
            data = log.read_bytes()
            self.assertIn(marker, data)
            record = json.loads((directory / receipts.command_result_name(index)).read_bytes())
            self.assertEqual(record['stdout_sha256'], m.digest(data))
            self.assertEqual(record['stderr_sha256'],
                             m.digest((directory / receipts.command_log_name(index, 'stderr')).read_bytes()))
            self.assertEqual(record['declared_exit_code'], command_exit(index))
        self.assertFalse((directory / receipts.POST_MANIFEST).exists())
        self.assertFalse((directory / receipts.COMPLETED).exists())
        failure = json.loads((directory / receipts.FAILURE).read_bytes())
        self.assertEqual(failure['stage'], 'post_manifest')
        self.assertFalse((Path(run) / 'tests.json').exists())
        self.assertEqual(m.load(Path(run) / 'metadata.json')['delivery'], 'tests_incomplete')

        before = tree_bytes(run)
        with mock.patch.object(m, 'local_test', side_effect=AssertionError('must not replay target test')):
            with self.assertRaisesRegex(receipts.ReceiptError, 'unresolved'):
                m.test_target(run)
        self.assertEqual(tree_bytes(run), before)

    def test_post_identity_mismatch_keeps_manifest_stage_and_original_error(self):
        bridge = self.bridge()
        run = bridge.applied()
        real_identity = m.Repo.identity
        identities = []

        def identity(repo):
            value = dict(real_identity(repo))
            identities.append(value)
            if len(identities) == 2:
                value['head'] = value['head'][:-1] + ('0' if value['head'][-1] != '0' else '1')
            return value

        with mock.patch.object(m.Repo, 'identity', autospec=True, side_effect=identity):
            with self.assertRaisesRegex(RuntimeError, 'identity/HEAD changed'):
                m.test_target(run)

        self.assertEqual(len(identities), 2)
        attempt = receipts.test_receipts(run)['attempts'][0]
        self.assertEqual(attempt['state'], 'incomplete')
        self.assertEqual(attempt['commands'][0]['declared_exit_code'], 0)
        directory = attempt_directory(run, attempt['attempt_id'])
        self.assertTrue((directory / receipts.POST_MANIFEST).is_file())
        self.assertTrue((directory / receipts.POST_IDENTITY).is_file())
        after = json.loads((directory / receipts.POST_IDENTITY).read_bytes())['value']['identity']
        self.assertNotEqual(after['head'], m.load(Path(run) / 'applied.json')['source']['head'])
        self.assertEqual(json.loads((directory / receipts.FAILURE).read_bytes())['stage'], 'post_identity')
        self.assertFalse((Path(run) / 'tests.json').exists())
        self.assertEqual(m.load(Path(run) / 'metadata.json')['delivery'], 'tests_incomplete')
        before = tree_bytes(run)
        with mock.patch.object(m, 'local_test', side_effect=AssertionError('must not replay target test')):
            with self.assertRaisesRegex(receipts.ReceiptError, 'unresolved'):
                m.test_target(run)
        self.assertEqual(tree_bytes(run), before)

    def test_local_transport_timeout_withholds_exit_and_preserves_bytes(self):
        bridge = self.bridge()
        run = bridge.applied()

        def timed_out(argv, cwd, seconds):
            raise subprocess.TimeoutExpired(argv, seconds, output=b'LOCAL PARTIAL\n',
                                            stderr=b'local warning\n')

        with mock.patch.object(m, 'local_test', autospec=True, side_effect=timed_out):
            report = m.test_target(run)
        self.assertFalse(report['passed'])
        self.assertIsNone(report['tests'][0]['exit_code'])
        self.assertFalse(report['tests'][0]['transport_complete'])
        self.assertIsNone(report['tests'][0]['transport_returncode'])
        attempt = receipts.test_receipts(run)['attempts'][0]
        command = attempt['commands'][0]
        self.assertEqual(command['transport'], 'local')
        self.assertFalse(command['transport_complete'])
        self.assertIsNone(command['declared_exit_code'])
        self.assertIsNone(command['transport_returncode'])
        self.assertEqual(command['exit_state'], 'unknown')
        self.assertEqual((Path(run) / 'test-0.stdout').read_bytes(), b'LOCAL PARTIAL\n')
        self.assertTrue((Path(run) / 'test-0.stderr').read_bytes().startswith(b'local warning\n'))
        before = tree_bytes(run)
        with mock.patch.object(m, 'local_test', side_effect=AssertionError('must not replay target test')):
            with self.assertRaisesRegex(receipts.ReceiptError, 'unknown transport'):
                m.test_target(run)
        self.assertEqual(tree_bytes(run), before)

    def test_post_manifest_drift_reports_unchanged_false_without_pass(self):
        bridge = self.bridge()
        run = bridge.applied()
        real_local = m.local_test

        def mutate(argv, cwd, seconds):
            result = real_local(argv, cwd, seconds)
            (bridge.repo / 'code.py').write_text('changed while testing\n')
            return result

        with mock.patch.object(m, 'local_test', side_effect=mutate):
            report = m.test_target(run)
        self.assertFalse(report['unchanged'])
        self.assertFalse(report['passed'])
        self.assertTrue((Path(run) / 'tests.json').is_file())
        self.assertEqual(m.load(Path(run) / 'metadata.json')['delivery'], 'tests_failed')
        attempt = receipts.test_receipts(run)['attempts'][0]
        self.assertEqual(attempt['state'], 'completed')
        self.assertEqual(attempt['commands'][0]['exit_state'], 'known')
        directory = attempt_directory(run, attempt['attempt_id'])
        stage = json.loads((directory / receipts.POST_MANIFEST).read_bytes())
        self.assertFalse(stage['value']['unchanged'])

    def test_failure_before_intent_creates_no_receipt_and_blocks_legacy_replay(self):
        bridge = self.bridge()
        run = bridge.applied()
        (Path(run) / 'applied.json').unlink()
        with mock.patch.object(m, 'local_test', side_effect=AssertionError('must not execute target test')):
            with self.assertRaises(FileNotFoundError):
                m.test_target(run)
        self.assertFalse((Path(run) / receipts.ATTEMPTS).exists())
        self.assertFalse((Path(run) / 'tests.json').exists())
        reported = receipts.test_receipts(run)
        self.assertEqual(reported['attempts'], [])
        self.assertTrue(reported['legacy']['unresolved'])
        self.assertEqual(reported['legacy']['delivery'], 'testing')
        before = tree_bytes(run)
        with mock.patch.object(m, 'local_test', side_effect=AssertionError('must not replay target test')):
            with self.assertRaisesRegex(receipts.ReceiptError, 'Legacy target-test evidence'):
                m.test_target(run)
        self.assertEqual(tree_bytes(run), before)

    def test_interrupt_after_intent_records_first_result_and_unknown_second_launch(self):
        bridge = self.bridge()
        commands = [['/usr/bin/true'], ['/usr/bin/true']]
        run = bridge.applied(commands)
        launches = []
        real_local = m.local_test

        def interrupted(argv, cwd, seconds):
            launches.append(list(argv))
            if len(launches) == 2:
                raise KeyboardInterrupt
            return real_local(argv, cwd, seconds)

        with mock.patch.object(m, 'local_test', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                m.test_target(run)

        self.assertEqual(len(launches), 2)
        attempt = receipts.test_receipts(run)['attempts'][0]
        self.assertEqual(attempt['state'], 'incomplete')
        self.assertEqual(attempt['commands'][0]['exit_state'], 'known')
        self.assertEqual(attempt['commands'][1]['exit_state'], 'unknown')
        self.assertEqual(attempt['commands'][1]['status'], 'intent_without_result')
        directory = attempt_directory(run, attempt['attempt_id'])
        self.assertTrue((directory / receipts.command_intent_name(1)).is_file())
        self.assertFalse((directory / receipts.command_result_name(1)).exists())
        self.assertFalse((Path(run) / 'tests.json').exists())
        self.assertEqual(m.load(Path(run) / 'metadata.json')['delivery'], 'tests_incomplete')
        before = tree_bytes(run)
        with mock.patch.object(m, 'local_test', side_effect=AssertionError('must not replay target test')):
            with self.assertRaisesRegex(receipts.ReceiptError, 'unresolved'):
                m.test_target(run)
        self.assertEqual(tree_bytes(run), before)

    def test_ssh_255_and_timeout_are_transport_incomplete_unknown_exits(self):
        for mode, observed in (('ssh255', 255), ('timeout', None)):
            with self.subTest(mode=mode):
                bridge = self.bridge()
                bridge.case.task['target'].update(
                    transport='ssh', host='test-alias',
                    ssh_keepalive={'server_alive_interval': 5, 'server_alive_count_max': 2})

                def fixture_transport(repo, argv, **kwargs):
                    if argv[0] == 'timeout':
                        if mode == 'ssh255':
                            return subprocess.CompletedProcess(argv, 255, b'SSH PARTIAL\n',
                                                               b'transport failed\n')
                        raise subprocess.TimeoutExpired(argv, 1, output=b'TIMEOUT PARTIAL\n',
                                                        stderr=b'timeout warning\n')
                    return m.cmd(argv, cwd=repo.repo, **kwargs)

                with mock.patch.object(m.Repo, 'run', autospec=True, side_effect=fixture_transport):
                    run = bridge.applied()
                    report = m.test_target(run)

                self.assertFalse(report['passed'])
                self.assertIsNone(report['tests'][0]['exit_code'])
                self.assertFalse(report['tests'][0]['transport_complete'])
                self.assertEqual(report['tests'][0]['transport_returncode'], observed)
                attempt = receipts.test_receipts(run)['attempts'][0]
                self.assertEqual(attempt['state'], 'completed')
                command = attempt['commands'][0]
                self.assertFalse(command['transport_complete'])
                self.assertIsNone(command['declared_exit_code'])
                self.assertEqual(command['transport_returncode'], observed)
                self.assertEqual(command['exit_state'], 'unknown')
                directory = attempt_directory(run, attempt['attempt_id'])
                record = json.loads((directory / receipts.command_result_name(0)).read_bytes())
                self.assertIsNone(record['declared_exit_code'])
                self.assertEqual(record['transport_returncode'], observed)
                retained = ((directory / receipts.command_log_name(0, 'stdout')).read_bytes() +
                            (directory / receipts.command_log_name(0, 'stderr')).read_bytes())
                self.assertIn(b'PARTIAL', retained)
                # A completed attempt with unknown transport still may not be replayed.
                before = tree_bytes(run)
                with mock.patch.object(m.Repo, 'run', autospec=True,
                                       side_effect=AssertionError('must not replay target test')):
                    with self.assertRaisesRegex(receipts.ReceiptError, 'unknown transport'):
                        m.test_target(run)
                self.assertEqual(tree_bytes(run), before)

    def test_explicit_retry_after_completed_attempt_keeps_first_receipt_and_archives_logs(self):
        bridge = self.bridge()
        run = bridge.applied()
        self.assertTrue(m.test_target(run)['passed'])
        first = receipts.test_receipts(run)['attempts'][0]
        self.assertEqual(first['state'], 'completed')
        first_dir = attempt_directory(run, first['attempt_id'])
        first_bytes = {path.name: path.read_bytes()
                       for path in first_dir.iterdir() if path.is_file()}
        old_stdout = (Path(run) / 'test-0.stdout').read_bytes()

        self.assertTrue(m.test_target(run)['passed'])
        attempts = receipts.test_receipts(run)['attempts']
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0]['attempt_id'], attempts[1]['attempt_id'])
        self.assertEqual([item['sequence'] for item in attempts], [1, 2])
        self.assertEqual([item['state'] for item in attempts], ['completed', 'completed'])
        self.assertEqual({path.name: path.read_bytes()
                          for path in first_dir.iterdir() if path.is_file()}, first_bytes)
        archives = list(Path(run).glob('tests-before-*'))
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / 'test-0.stdout').read_bytes(), old_stdout)
        self.assertTrue((archives[0] / 'tests.json').is_file())

    def test_corrupt_or_missing_attempt_evidence_prevents_replay(self):
        cases = {
            'missing_log': lambda path: (path / receipts.command_log_name(0, 'stdout')).unlink(),
            'corrupt_log': lambda path: (path / receipts.command_log_name(0, 'stdout')).write_bytes(b'tampered'),
            'missing_result': lambda path: (path / receipts.command_result_name(0)).unlink(),
            'corrupt_intent': lambda path: (path / receipts.INTENT).write_bytes(b'{'),
            'missing_completed': lambda path: (path / receipts.COMPLETED).unlink(),
            'corrupt_binding': lambda path: (path / receipts.BINDING).write_bytes(b'{}'),
        }
        for name, damage in cases.items():
            with self.subTest(case=name):
                bridge = self.bridge()
                run = bridge.applied()
                m.test_target(run)
                attempt = receipts.test_receipts(run)['attempts'][0]
                self.assertEqual(attempt['state'], 'completed')
                directory = attempt_directory(run, attempt['attempt_id'])
                damage(directory)
                broken = receipts.test_receipts(run)['attempts'][0]
                self.assertIn(broken['state'], ('invalid', 'incomplete'))
                before = tree_bytes(run)
                with mock.patch.object(m, 'local_test',
                                       side_effect=AssertionError('must not replay target test')):
                    with self.assertRaises(receipts.ReceiptError):
                        m.test_target(run)
                self.assertEqual(tree_bytes(run), before)
                self.assertEqual(len(list((Path(run) / receipts.ATTEMPTS).iterdir())), 1)

    def test_receipt_writes_are_exclusive_and_intent_binds_run_bytes(self):
        bridge = self.bridge()
        run = bridge.applied()
        task = json.loads((Path(run) / 'task.json').read_bytes())
        applied = json.loads((Path(run) / 'applied.json').read_bytes())
        first = receipts.begin(run, task, applied)
        second = receipts.begin(run, task, applied)
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual(first.directory.parent, second.directory.parent)
        intent_path = first.directory / receipts.INTENT
        self.assertEqual(intent_path.stat().st_mode & 0o777, 0o600)
        original = intent_path.read_bytes()
        intent = json.loads(original)
        self.assertEqual(intent['declared_commands'], task['test_commands'])
        self.assertEqual(intent['task_sha256'], m.digest((Path(run) / 'task.json').read_bytes()))
        self.assertEqual(intent['applied_sha256'], m.digest((Path(run) / 'applied.json').read_bytes()))
        self.assertEqual(intent['applied_fingerprint'], applied['fingerprint'])
        self.assertEqual(intent['identity'], applied['source'])
        command = task['test_commands'][0]
        first.command_intent(0, command, ['env', 'PYTHONDONTWRITEBYTECODE=1'] + command)
        with self.assertRaises(FileExistsError):
            first.command_intent(0, command, ['env', 'PYTHONDONTWRITEBYTECODE=1'] + command)
        self.assertEqual(intent_path.read_bytes(), original)

    def test_inspection_and_cli_are_read_only_and_avoid_the_target(self):
        bridge = self.bridge()
        run = bridge.applied()
        m.test_target(run)
        before = tree_bytes(run)
        with mock.patch.object(m.Repo, '__init__', side_effect=AssertionError('no target access')):
            state = receipts.test_receipts(run)
        self.assertEqual([item['state'] for item in state['attempts']], ['completed'])
        self.assertEqual(tree_bytes(run), before)

        result = subprocess.run(
            [sys.executable, '-B', str(Path(m.__file__)), 'test-receipts', '--run', str(run)],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([item['attempt_id'] for item in payload['attempts']],
                         [item['attempt_id'] for item in state['attempts']])
        self.assertEqual(payload['attempts'][0]['commands'][0]['exit_state'], 'known')
        self.assertEqual(tree_bytes(run), before)

    def test_legacy_orphan_logs_are_preserved_and_never_replayed(self):
        bridge = self.bridge()
        run = bridge.applied()
        meta = m.load(Path(run) / 'metadata.json')
        meta['delivery'] = 'testing'
        m.dump(Path(run) / 'metadata.json', meta)
        orphan = Path(run) / 'test-0.stdout'
        orphan.write_bytes(b'legacy partial output\n')
        reported = receipts.test_receipts(run)
        self.assertEqual(reported['attempts'], [])
        self.assertTrue(reported['legacy']['unresolved'])
        self.assertEqual(reported['legacy']['test_logs'], ['test-0.stdout'])
        before = tree_bytes(run)
        with mock.patch.object(m, 'local_test', side_effect=AssertionError('must not replay legacy')):
            with self.assertRaisesRegex(receipts.ReceiptError, 'unresolved'):
                m.test_target(run)
        self.assertEqual(tree_bytes(run), before)
        self.assertFalse((Path(run) / 'tests.json').exists())
        self.assertFalse((Path(run) / receipts.ATTEMPTS).exists())


    def legacy_run(self, bridge, exits, strict=False):
        commands = [[sys.executable, '-B', '-c',
                     'print("LEGACY%d"); raise SystemExit(%d)' % (index, code)]
                    for index, code in enumerate(exits)]
        run = bridge.applied(commands)
        tests = []
        for index, (argv, code) in enumerate(zip(commands, exits)):
            result = m.local_test(['env', 'PYTHONDONTWRITEBYTECODE=1'] + argv, bridge.repo, 30)
            self.assertEqual(result.returncode, code)
            (Path(run) / ('test-%d.stdout' % index)).write_bytes(result.stdout)
            (Path(run) / ('test-%d.stderr' % index)).write_bytes(result.stderr)
            entry = {'argv': argv, 'exit_code': result.returncode,
                     'stdout': 'test-%d.stdout' % index,
                     'stderr': 'test-%d.stderr' % index,
                     'collection': {'passed': result.returncode == 0}}
            if strict:
                # Fields written by the pre-receipt strict writer; it did not
                # emit transport_returncode, so the fixture must not either.
                entry.update(stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
                             stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
                             transport_complete=True,
                             exit_scope='declared_test_process')
            tests.append(entry)
        applied = m.load(Path(run) / 'applied.json')
        report = {'host': applied['source'], 'fingerprint': applied['fingerprint'],
                  'unchanged': True, 'passed': all(code == 0 for code in exits), 'tests': tests}
        if strict:
            report['evidence_version'] = 1
        m.dump(Path(run) / 'tests.json', report)
        meta = m.load(Path(run) / 'metadata.json')
        meta['delivery'] = 'pending_review' if report['passed'] else 'tests_failed'
        m.dump(Path(run) / 'metadata.json', meta)
        return run, report

    def write_pass_review(self, bridge, run):
        from process_evidence import binding, async_enabled, live_output_enabled
        from test_reviewer_provenance import synthetic_review
        task = m.load(Path(run) / 'task.json')
        meta = m.load(Path(run) / 'metadata.json')
        applied = m.load(Path(run) / 'applied.json')
        review = dict(synthetic_review(Path(run)), verdict='pass',
                      fingerprint=applied['fingerprint'], findings=[],
                      process_review={
                          'verdict': 'pass',
                          'binding': binding(Path(run), async_required=async_enabled(task, meta),
                                             live_output_required=live_output_enabled(task, meta)),
                          'async_jobs': [],
                          'reason': 'Synthetic bridge fixture has zero Bash calls.'})
        m.dump(Path(run) / 'review.json', review)
        meta['delivery'] = 'pending_finalization'
        m.dump(Path(run) / 'metadata.json', meta)

    def test_complete_failure_blocks_review_and_finalize(self):
        bridge = self.bridge()
        run = bridge.applied()
        real_write = receipts._exclusive_write

        def fail_completion(directory, name, data):
            if name == receipts.COMPLETED:
                raise OSError('receipt completion failed')
            return real_write(directory, name, data)

        with mock.patch.object(receipts, '_exclusive_write', side_effect=fail_completion):
            with self.assertRaisesRegex(OSError, 'receipt completion failed'):
                m.test_target(run)
        # The report was published, but its capture never completed.
        self.assertTrue((Path(run) / 'tests.json').is_file())
        self.assertEqual(m.load(Path(run) / 'metadata.json')['delivery'], 'tests_incomplete')
        attempt = receipts.test_receipts(run)['attempts'][0]
        self.assertEqual(attempt['state'], 'incomplete')
        self.assertTrue((attempt_directory(run, attempt['attempt_id']) / receipts.REPORT).is_file())
        with self.assertRaisesRegex(receipts.ReceiptError, 'incomplete'):
            bridge.case.record_pass()
        self.assertFalse((Path(run) / 'review.json').exists())
        self.write_pass_review(bridge, run)
        with self.assertRaisesRegex(receipts.ReceiptError, 'incomplete'):
            m.finalize(run)
        self.assertNotEqual(m.status(run)['delivery'], 'verified')

    def test_incomplete_or_changed_current_receipt_blocks_finalize(self):
        def missing_stage(run):
            attempt = receipts.test_receipts(run)['attempts'][0]
            (attempt_directory(run, attempt['attempt_id']) / receipts.POST_IDENTITY).unlink()

        def changed_report(run):
            report = m.load(Path(run) / 'tests.json')
            report['note'] = 'changed after capture'
            m.dump(Path(run) / 'tests.json', report)

        def missing_attempts(run):
            # Remove the whole owned receipt tree without a recursive helper.
            root = Path(run) / receipts.ATTEMPTS
            for attempt in root.iterdir():
                for path in attempt.iterdir():
                    path.unlink()
                attempt.rmdir()
            root.rmdir()

        for name, damage in (('missing_stage', missing_stage), ('changed_report', changed_report),
                             ('missing_attempts', missing_attempts)):
            with self.subTest(case=name):
                bridge = self.bridge()
                run = bridge.applied()
                self.assertTrue(m.test_target(run)['passed'])
                bridge.case.record_pass()
                self.assertEqual(m.finalize(run)['delivery'], 'verified')
                damage(run)
                with self.assertRaisesRegex(receipts.ReceiptError, 'target-test'):
                    m.finalize(run)
                self.assertNotEqual(m.status(run)['delivery'], 'verified')
                if damage is missing_attempts:
                    self.assertFalse((Path(run) / receipts.ATTEMPTS).exists())
                    reported = receipts.test_receipts(run)
                    self.assertTrue(reported['modern_receipts_expected'])
                    self.assertTrue(reported['legacy']['unresolved'])
                    self.assertEqual(reported['legacy']['reason'],
                                     'modern_receipt_evidence_missing')
                    before = tree_bytes(run)
                    with mock.patch.object(
                            m, 'local_test',
                            side_effect=AssertionError('must not replay removed receipts')):
                        with self.assertRaisesRegex(receipts.ReceiptError,
                                                    'attempt records are missing'):
                            m.test_target(run)
                    self.assertEqual(tree_bytes(run), before)

    def test_untracked_status_change_keeps_receipt_valid(self):
        bridge = self.bridge()
        run = bridge.applied()
        (bridge.repo / 'untracked-output.txt').write_text('created after apply\n')
        self.assertTrue(m.test_target(run)['passed'])
        attempt = receipts.test_receipts(run)['attempts'][0]
        self.assertEqual(attempt['state'], 'completed')
        directory = attempt_directory(run, attempt['attempt_id'])
        before = json.loads((directory / receipts.BEFORE_IDENTITY).read_bytes())['value']['identity']
        applied = m.load(Path(run) / 'applied.json')
        self.assertNotEqual(before['status'], applied['source']['status'])
        for key in ('host', 'user', 'repo', 'head'):
            self.assertEqual(before[key], applied['source'][key])
        self.write_pass_review(bridge, run)
        self.assertEqual(m.finalize(run)['delivery'], 'verified')

    def test_completed_attempt_requires_every_record_before_replay(self):
        cases = {
            'intent': lambda directory: (directory / receipts.INTENT).unlink(),
            'command_intent': lambda directory: (directory / receipts.command_intent_name(0)).unlink(),
            'command_result': lambda directory: (directory / receipts.command_result_name(0)).unlink(),
            'before_identity': lambda directory: (directory / receipts.BEFORE_IDENTITY).unlink(),
            'before_manifest': lambda directory: (directory / receipts.BEFORE_MANIFEST).unlink(),
            'post_manifest': lambda directory: (directory / receipts.POST_MANIFEST).unlink(),
            'post_identity': lambda directory: (directory / receipts.POST_IDENTITY).unlink(),
            'report': lambda directory: (directory / receipts.REPORT).unlink(),
            'binding': lambda directory: (directory / receipts.BINDING).unlink(),
        }
        for name, damage in cases.items():
            with self.subTest(case=name):
                bridge = self.bridge()
                run = bridge.applied()
                self.assertTrue(m.test_target(run)['passed'])
                attempt = receipts.test_receipts(run)['attempts'][0]
                self.assertEqual(attempt['state'], 'completed')
                damage(attempt_directory(run, attempt['attempt_id']))
                damaged = receipts.test_receipts(run)['attempts'][0]
                self.assertIn(damaged['state'], ('invalid', 'incomplete'))
                self.assertTrue(damaged['unresolved'])
                before = tree_bytes(run)
                with mock.patch.object(m, 'local_test',
                                       side_effect=AssertionError('must not replay target test')):
                    with self.assertRaises(receipts.ReceiptError):
                        m.test_target(run)
                self.assertEqual(tree_bytes(run), before)

    def test_contradictory_current_receipt_blocks_replay(self):
        def rewrite(path, mutate):
            value = json.loads(path.read_bytes())
            mutate(value)
            path.write_bytes((json.dumps(value, sort_keys=True, indent=2) + '\n').encode())

        def wrong_task_hash(value):
            value['task_sha256'] = '0' * 64

        def wrong_actual_argv(value):
            value['actual_argv'] = ['/bin/true']

        def wrong_exit(value):
            value['declared_exit_code'] = 1

        cases = {
            'intent_task_hash': (receipts.INTENT, wrong_task_hash),
            'actual_argv': (receipts.command_intent_name(0), wrong_actual_argv),
            'result_exit_vs_report': (receipts.command_result_name(0), wrong_exit),
        }
        for name, (record, mutate) in cases.items():
            with self.subTest(case=name):
                bridge = self.bridge()
                run = bridge.applied()
                self.assertTrue(m.test_target(run)['passed'])
                attempt = receipts.test_receipts(run)['attempts'][0]
                rewrite(attempt_directory(run, attempt['attempt_id']) / record, mutate)
                damaged = receipts.test_receipts(run)['attempts'][0]
                self.assertEqual(damaged['state'], 'invalid')
                before = tree_bytes(run)
                with mock.patch.object(m, 'local_test',
                                       side_effect=AssertionError('must not replay target test')):
                    with self.assertRaises(receipts.ReceiptError):
                        m.test_target(run)
                self.assertEqual(tree_bytes(run), before)

    def test_completed_legacy_report_allows_explicit_retest_and_archives(self):
        for exits in ([0], [7]):
            with self.subTest(exits=exits):
                bridge = self.bridge()
                run, _report = self.legacy_run(bridge, exits)
                originals = {name: (Path(run) / name).read_bytes()
                             for name in ('tests.json', 'test-0.stdout', 'test-0.stderr')}
                state = receipts.test_receipts(run)
                self.assertEqual(state['attempts'], [])
                self.assertFalse(state['modern_receipts_expected'])
                self.assertFalse(state['legacy']['unresolved'])
                self.assertTrue(state['legacy']['tests_json_known'])
                self.assertEqual(state['legacy']['test_logs'], ['test-0.stderr', 'test-0.stdout'])
                self.assertEqual(m.test_target(run), m.load(Path(run) / 'tests.json'))
                attempts = receipts.test_receipts(run)['attempts']
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]['state'], 'completed')
                archives = list(Path(run).glob('tests-before-*'))
                self.assertEqual(len(archives), 1)
                for name, data in originals.items():
                    self.assertEqual((archives[0] / name).read_bytes(), data)

    def test_unresolved_legacy_report_blocks_retest_without_side_effects(self):
        def missing_log(run, report):
            (Path(run) / 'test-0.stdout').unlink()

        def unknown_exit(run, report):
            report['tests'][0]['exit_code'] = None
            report['tests'][0]['transport_complete'] = False
            report['tests'][0]['transport_returncode'] = 255
            report['passed'] = False
            m.dump(Path(run) / 'tests.json', report)

        def mismatched_argv(run, report):
            report['tests'][0]['argv'] = ['/usr/bin/true']
            m.dump(Path(run) / 'tests.json', report)

        def malformed_report(run, report):
            (Path(run) / 'tests.json').write_bytes(b'{')

        for name, damage in (('missing_log', missing_log), ('unknown_exit', unknown_exit),
                             ('mismatched_argv', mismatched_argv),
                             ('malformed_report', malformed_report)):
            with self.subTest(case=name):
                bridge = self.bridge()
                run, report = self.legacy_run(bridge, [0])
                damage(run, report)
                state = receipts.test_receipts(run)
                self.assertEqual(state['attempts'], [])
                self.assertTrue(state['legacy']['unresolved'])
                self.assertFalse(state['legacy']['tests_json_known'])
                before = tree_bytes(run)
                with mock.patch.object(m, 'local_test',
                                       side_effect=AssertionError('must not replay legacy evidence')):
                    with self.assertRaisesRegex(receipts.ReceiptError, 'Legacy target-test evidence'):
                        m.test_target(run)
                self.assertEqual(tree_bytes(run), before)
                self.assertFalse((Path(run) / receipts.ATTEMPTS).exists())

    def test_strict_legacy_report_allows_retest_with_every_captured_field(self):
        for exits in ([0], [7]):
            with self.subTest(exits=exits):
                bridge = self.bridge()
                run, _report = self.legacy_run(bridge, exits, strict=True)
                originals = {name: (Path(run) / name).read_bytes()
                             for name in ('tests.json', 'test-0.stdout', 'test-0.stderr')}
                state = receipts.test_receipts(run)
                self.assertEqual(state['attempts'], [])
                self.assertFalse(state['modern_receipts_expected'])
                self.assertTrue(state['legacy']['tests_json_known'])
                self.assertFalse(state['legacy']['unresolved'])
                self.assertEqual(m.test_target(run), m.load(Path(run) / 'tests.json'))
                attempts = receipts.test_receipts(run)['attempts']
                self.assertEqual([item['state'] for item in attempts], ['completed'])
                archives = list(Path(run).glob('tests-before-*'))
                self.assertEqual(len(archives), 1)
                for name, data in originals.items():
                    self.assertEqual((archives[0] / name).read_bytes(), data)

    def test_incomplete_strict_legacy_report_blocks_retest_without_side_effects(self):
        def mutate(mutator):
            def damage(run, report):
                mutator(report['tests'][0])
                m.dump(Path(run) / 'tests.json', report)
            return damage

        def orphan_log(run, report):
            (Path(run) / 'test-1.stdout').write_bytes(b'unexpected\n')

        def missing_log(run, report):
            (Path(run) / 'test-0.stdout').unlink()

        cases = {
            'missing_stdout_sha256': mutate(lambda result: result.pop('stdout_sha256')),
            'missing_stderr_sha256': mutate(lambda result: result.pop('stderr_sha256')),
            'stdout_hash_mismatch': mutate(lambda result: result.update(stdout_sha256='0' * 64)),
            'missing_transport_complete': mutate(lambda result: result.pop('transport_complete')),
            'incomplete_transport': mutate(lambda result: result.update(transport_complete=False)),
            'missing_exit_scope': mutate(lambda result: result.pop('exit_scope')),
            'wrong_exit_scope': mutate(lambda result: result.update(exit_scope='shell')),
            'unknown_exit': mutate(lambda result: result.update(exit_code=None)),
            'mismatched_argv': mutate(lambda result: result.update(argv=['/usr/bin/true'])),
            'wrong_log_reference': mutate(lambda result: result.update(stdout='test-9.stdout')),
            'orphan_log': orphan_log,
            'missing_log': missing_log,
        }
        for name, damage in cases.items():
            with self.subTest(case=name):
                bridge = self.bridge()
                run, report = self.legacy_run(bridge, [0], strict=True)
                damage(run, report)
                state = receipts.test_receipts(run)
                self.assertEqual(state['attempts'], [])
                self.assertTrue(state['legacy']['unresolved'])
                self.assertFalse(state['legacy']['tests_json_known'])
                before = tree_bytes(run)
                with mock.patch.object(m, 'local_test',
                                       side_effect=AssertionError('must not replay legacy evidence')):
                    with self.assertRaisesRegex(receipts.ReceiptError, 'Legacy target-test evidence'):
                        m.test_target(run)
                self.assertEqual(tree_bytes(run), before)
                self.assertFalse((Path(run) / receipts.ATTEMPTS).exists())

    def test_missing_modern_receipt_tree_is_reported_unresolved_not_legacy(self):
        def remove_tree(run):
            root = Path(run) / receipts.ATTEMPTS
            for attempt in root.iterdir():
                for path in attempt.iterdir():
                    path.unlink()
                attempt.rmdir()
            root.rmdir()

        def leave_unusable_root(run):
            remove_tree(run)
            root = Path(run) / receipts.ATTEMPTS
            root.mkdir()
            (root / 'partial-record').write_bytes(b'{}')

        for name, damage in (('missing_tree', remove_tree),
                             ('unusable_tree', leave_unusable_root)):
            with self.subTest(case=name):
                bridge = self.bridge()
                run = bridge.applied()
                self.assertTrue(m.test_target(run)['passed'])
                damage(run)
                state = receipts.test_receipts(run)
                self.assertEqual(state['attempts'], [])
                self.assertTrue(state['modern_receipts_expected'])
                self.assertTrue(state['legacy']['unresolved'])
                self.assertFalse(state['legacy']['tests_json_known'])
                self.assertEqual(state['legacy']['reason'], 'modern_receipt_evidence_missing')
                before = tree_bytes(run)
                expected = receipts.test_receipts(run)
                output = io.StringIO()
                with mock.patch.object(sys, 'argv', ['team_dev.py', 'test-receipts',
                                                     '--run', str(run)]), \
                        mock.patch.object(
                            m, 'Repo',
                            side_effect=AssertionError('no target access')) as repo_sentinel, \
                        mock.patch.object(
                            m, 'target_repo',
                            side_effect=AssertionError('no target access')) as target_sentinel, \
                        mock.patch.object(
                            m, 'worker_env',
                            side_effect=AssertionError('no provider access')) as env_sentinel, \
                        contextlib.redirect_stdout(output):
                    m.main()
                repo_sentinel.assert_not_called()
                target_sentinel.assert_not_called()
                env_sentinel.assert_not_called()
                payload = json.loads(output.getvalue())
                self.assertEqual(payload, expected)
                self.assertEqual(payload['attempts'], [])
                self.assertTrue(payload['modern_receipts_expected'])
                self.assertEqual(payload['legacy']['reason'], 'modern_receipt_evidence_missing')
                self.assertEqual(tree_bytes(run), before)

                # Parent sentinels cannot instrument a fresh interpreter, so this
                # separate real invocation only proves the CLI entry point agrees.
                result = subprocess.run(
                    [sys.executable, '-B', str(Path(m.__file__)), 'test-receipts',
                     '--run', str(run)],
                    capture_output=True, text=True, timeout=60,
                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(tree_bytes(run), before)
                with mock.patch.object(m, 'local_test',
                                       side_effect=AssertionError('must not replay removed receipts')):
                    with self.assertRaisesRegex(receipts.ReceiptError,
                                                'attempt records are missing'):
                        m.test_target(run)
                self.assertEqual(tree_bytes(run), before)

    def test_record_phase_order_matches_numeric_prefixes(self):
        bridge = self.bridge()
        run = bridge.applied()
        self.assertTrue(m.test_target(run)['passed'])
        attempt = receipts.test_receipts(run)['attempts'][0]
        directory = attempt_directory(run, attempt['attempt_id'])
        expected = [receipts.INTENT, receipts.BEFORE_IDENTITY, receipts.BEFORE_MANIFEST,
                    receipts.command_intent_name(0),
                    receipts.command_log_name(0, 'stdout'), receipts.command_log_name(0, 'stderr'),
                    receipts.command_result_name(0), receipts.POST_MANIFEST,
                    receipts.POST_IDENTITY, receipts.REPORT, receipts.BINDING, receipts.COMPLETED]
        self.assertEqual(sorted(path.name for path in directory.iterdir()), sorted(expected))
        ordered = sorted(expected)
        self.assertLess(ordered.index(receipts.BEFORE_MANIFEST),
                        ordered.index(receipts.command_intent_name(0)))
        self.assertLess(ordered.index(receipts.command_intent_name(0)),
                        ordered.index(receipts.command_result_name(0)))
        self.assertLess(ordered.index(receipts.command_result_name(0)),
                        ordered.index(receipts.POST_MANIFEST))


def command_exit(index):
    return 7 if index == 0 else 9


if __name__ == '__main__':
    unittest.main()
