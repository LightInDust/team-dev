"""Acceptance tests for structured v2 async delivery in the read-only ledger.

Every fixture is synthetic: the async job is a recorded ACK plus a delivered
custom notice, never a real child process, network call, SSH session or model.
Compact rows are derived with the production ``async_evidence`` helpers so the
private/compact hashes and ordering match what a real run stores.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import async_evidence as a
import evidence_tools as e

COMMAND = ('env PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m unittest discover '
           '-s scripts -p test_invocation_async.py -v')
COMMAND_HASH = hashlib.sha256(COMMAND.strip().encode('utf-8', 'surrogatepass')).hexdigest()
MARKER = 'PRIVATE-ASYNC-BODY-DO-NOT-COPY'
ACK_TEXT = 'Command moved to the background.'


def write_jsonl(root, name, records):
    (root / name).write_bytes(b''.join(
        (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode()
        for record in records))


def ack(job, state='running', text=ACK_TEXT):
    return {'content': [{'type': 'text', 'text': text}],
            'details': {'async': {'jobId': job, 'type': 'bash', 'state': state}}}


def notice(job, check, code=0, content=None, jobs=None):
    text = 'Command exited with code %d\nWORKER_TEST_LOG_DIR=%s\n' % (code, check)
    return {'role': 'custom', 'customType': 'async-result',
            'content': content if content is not None else [{'type': 'text', 'text': text}],
            'details': {'jobs': jobs if jobs is not None else [{'jobId': job, 'type': 'bash'}]}}


def private_start(ident, command=COMMAND, **extra):
    args = {'command': command}
    args.update(extra)
    return {'version': 1, 'type': 'tool_execution_start', 'id': ident, 'args': args}


def private_end(ident, result, is_error=False):
    return {'version': 1, 'type': 'tool_execution_end', 'id': ident,
            'is_error': is_error, 'result': result}


def private_notice(message):
    return {'version': 1, 'type': 'async_result', 'message': message}


def compact_start(ident, command=COMMAND):
    return {'type': 'tool_execution_start', 'toolName': 'bash', 'toolCallId': ident,
            'args': {'command': command}}


def compact_end(ident, result, is_error=False):
    return {'type': 'tool_execution_end', 'toolName': 'bash', 'toolCallId': ident,
            'isError': is_error, 'asyncBash': a.bash_async(result)}


def compact_notice(message):
    return {'type': 'message_end', 'message': {'role': 'custom'},
            'asyncResult': a.async_notice(message)}


def decision(ident, command=COMMAND, **extra):
    args = {'command': command}
    args.update(extra)
    return {'type': 'decision', 'id': ident, 'args': args,
            'status': 'allowed', 'decision': 'allow', 'denials': 0}


def tree_snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob('*')) if path.is_file() and not path.is_symlink()}


def cli(*args):
    return subprocess.run([sys.executable, '-B', str(Path(e.__file__)), *args],
                          capture_output=True, text=True, cwd=Path.cwd(), timeout=60)


class AsyncLedgerTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.assertFalse(str(self.root.resolve()).startswith(str(Path.cwd().resolve())),
                         'scratch directory must live outside the snapshot')
        self.run = self.root / 'run'
        self.run.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def prepare(self, name='run', policy=None, metadata=True):
        run = self.root / name
        if run != self.run:
            run.mkdir()
        value = {'target': {'transport': 'local', 'host': None, 'repo': '/repo'},
                 'goal': 'Join structured v2 async delivery in the read-only ledger',
                 'acceptance': ['A1'], 'interface_contracts': [],
                 'allowed_paths': ['scripts/evidence_tools.py'],
                 'test_commands': [['/usr/bin/python3', '-B', '-m', 'unittest', 'discover',
                                    '-s', 'scripts']],
                 'worker_bash_policy': {'version': 2, 'allowed_command_sha256': [COMMAND_HASH]},
                 'process_evidence_policy': {'version': 2},
                 'async_execution_policy': dict(a.POLICY if policy is None else policy)}
        (run / 'task.json').write_text(json.dumps(value) + '\n')
        if metadata:
            marker = dict(a.POLICY if policy is None else policy)
            (run / 'metadata.json').write_text(
                json.dumps({'async_execution_policy': marker}) + '\n')
        return run

    def check(self, run, token, exit_code=0, stdout=b'OK\n', stderr=b''):
        root = run / 'worker-test-logs'
        root.mkdir(exist_ok=True)
        check = root / ('check-' + token)
        check.mkdir(exist_ok=True)
        (check / 'exit-code').write_bytes(('%d\n' % exit_code).encode())
        (check / 'stdout.log').write_bytes(stdout)
        (check / 'stderr.log').write_bytes(stderr)
        return check

    def journals(self, run, private, compact, decisions=None):
        write_jsonl(run, 'tool-evidence.jsonl', private)
        write_jsonl(run, 'events.jsonl', compact)
        if decisions is None:
            decisions = [decision(record['id']) for record in private
                         if record.get('type') == 'tool_execution_start']
        write_jsonl(run, 'bash-gate-events.jsonl', decisions)

    def assert_unsupported(self, run, reason=None):
        ledger = e.invocation_ledger(run)
        self.assertFalse(ledger['complete'])
        self.assertNotEqual(ledger['async']['state'], 'supported')
        if reason is not None:
            self.assertEqual(ledger['async']['reason'], reason)
        self.assertTrue(any(issue['kind'] == 'async' for issue in ledger['issues']),
                        ledger['issues'])
        for row in ledger['rows']:
            self.assertEqual(row['executed'], e.UNKNOWN)
            self.assertFalse(row['binding_complete'])
            self.assertIsNone(row['async_binding'])
        return ledger

    def test_single_v2_delivery_binds_notice_log_without_certification(self):
        run = self.prepare()
        check = self.check(run, 'ASY00001')
        result = ack('bg_1')
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = e.invocation_ledger(run)
        self.assertTrue(ledger['complete'], ledger['issues'])
        self.assertEqual(ledger['ledger_version'], 2)
        self.assertEqual(ledger['async']['state'], 'supported')
        self.assertEqual(ledger['async']['protocol_version'], 2)
        row = ledger['rows'][0]
        self.assertIs(row['executed'], True)
        self.assertEqual(row['exit_code'], 0)
        self.assertEqual(row['terminal_exit_code'], e.UNKNOWN)
        self.assertEqual(row['result_source'], 'async-notice')
        binding = row['async_binding']
        self.assertEqual(binding['job_id'], 'bg_1')
        self.assertEqual(binding['generation'], 1)
        self.assertEqual(binding['ack_state'], 'running')
        self.assertEqual(binding['result_sha256'], a.value_sha(result))
        self.assertEqual(binding['notice_sha256'], a.value_sha(message))
        self.assertIs(row['recorded_is_error'], False)
        self.assertEqual(row['log_dir'], str(check))
        self.assertEqual(row['logs']['stdout.log']['sha256'], hashlib.sha256(b'OK\n').hexdigest())
        self.assertIs(ledger['acceptance_certified'], False)
        self.assertIs(ledger['current_target_verified'], False)
        self.assertIs(ledger['current_liveness_verified'], False)
        self.assertNotIn(MARKER, json.dumps(ledger))

    def test_nonzero_retained_exit_is_complete_but_not_success(self):
        run = self.prepare()
        check = self.check(run, 'ASY00002', exit_code=3, stdout=b'FAILED (failures=1)\n')
        result = ack('bg_2')
        message = notice('bg_2', check, code=3)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = e.invocation_ledger(run)
        self.assertTrue(ledger['complete'], ledger['issues'])
        row = ledger['rows'][0]
        self.assertIs(row['executed'], True)
        self.assertEqual(row['exit_code'], 3)
        self.assertEqual(row['terminal_exit_code'], e.UNKNOWN)
        self.assertIs(ledger['acceptance_certified'], False)

    def test_string_and_multi_text_notice_content_bind_single_marker(self):
        cases = {
            'string': 'Command exited with code 0\nWORKER_TEST_LOG_DIR=%s\n',
            'blocks': [{'type': 'text', 'text': 'Command exited with code 0'},
                       {'type': 'text', 'text': 'WORKER_TEST_LOG_DIR=%s'}],
        }
        for index, (label, template) in enumerate(cases.items()):
            with self.subTest(label=label):
                run = self.prepare('content-' + label)
                check = self.check(run, 'CONT%04d' % index)
                content = (template % check if isinstance(template, str)
                           else [dict(block, text=(block['text'] % check
                                                   if '%s' in block['text'] else block['text']))
                                 for block in template])
                result = ack('bg_1')
                message = notice('bg_1', check, content=content)
                self.journals(run,
                              [private_start('call-1'), private_end('call-1', result),
                               private_notice(message)],
                              [compact_start('call-1'), compact_end('call-1', result),
                               compact_notice(message)],
                              [decision('call-1')])
                ledger = e.invocation_ledger(run)
                self.assertTrue(ledger['complete'], ledger['issues'])
                self.assertEqual(ledger['rows'][0]['log_dir'], str(check))

    def test_serial_generations_of_one_job_id_bind_distinct_logs(self):
        run = self.prepare()
        first = self.check(run, 'GEN10001', exit_code=0)
        second = self.check(run, 'GEN20002', exit_code=1)
        first_result, first_notice = ack('bg_1'), notice('bg_1', first)
        second_result, second_notice = ack('bg_1'), notice('bg_1', second, code=1)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', first_result),
                       private_notice(first_notice),
                       private_start('call-2'), private_end('call-2', second_result),
                       private_notice(second_notice)],
                      [compact_start('call-1'), compact_end('call-1', first_result),
                       compact_notice(first_notice),
                       compact_start('call-2'), compact_end('call-2', second_result),
                       compact_notice(second_notice)],
                      [decision('call-1'), decision('call-2')])
        ledger = e.invocation_ledger(run)
        self.assertTrue(ledger['complete'], ledger['issues'])
        rows = ledger['rows']
        self.assertEqual([row['async_binding']['generation'] for row in rows], [1, 2])
        self.assertEqual([row['exit_code'] for row in rows], [0, 1])
        self.assertEqual([row['log_dir'] for row in rows], [str(first), str(second)])
        self.assertTrue(all(row['executed'] is True for row in rows))

    def test_premature_job_id_reuse_stays_unknown(self):
        run = self.prepare()
        check = self.check(run, 'REUS0001')
        first, first_notice = ack('bg_1'), notice('bg_1', check)
        second = ack('bg_1')
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', first),
                       private_start('call-2'), private_end('call-2', second),
                       private_notice(first_notice)],
                      [compact_start('call-1'), compact_end('call-1', first),
                       compact_start('call-2'), compact_end('call-2', second),
                       compact_notice(first_notice)],
                      [decision('call-1'), decision('call-2')])
        self.assert_unsupported(run, 'async_delivery_unverified')

    def test_stale_replay_and_duplicate_notice_stay_unknown(self):
        cases = {
            'replay': ['start-1', 'end-1', 'notice-1', 'start-2', 'end-2', 'notice-1'],
            'duplicate': ['start-1', 'end-1', 'notice-1', 'notice-1'],
        }
        for index, (label, stream) in enumerate(cases.items()):
            with self.subTest(label=label):
                run = self.prepare('replay-' + label)
                check = self.check(run, 'STAL%04d' % index)
                result = ack('bg_1')
                message = notice('bg_1', check)
                private, compact = [], []
                for token in stream:
                    kind, _, ident = token.partition('-')
                    if kind == 'start':
                        private.append(private_start('call-' + ident))
                        compact.append(compact_start('call-' + ident))
                    elif kind == 'end':
                        private.append(private_end('call-' + ident, result))
                        compact.append(compact_end('call-' + ident, result))
                    else:
                        private.append(private_notice(message))
                        compact.append(compact_notice(message))
                self.journals(run, private, compact,
                              [decision('call-1'), decision('call-2')])
                self.assert_unsupported(run, 'async_delivery_unverified')

    def test_missing_notice_and_unmatched_job_stay_unknown(self):
        run = self.prepare('missing')
        check = self.check(run, 'MISS0001')
        result = ack('bg_1')
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result)],
                      [compact_start('call-1'), compact_end('call-1', result)],
                      [decision('call-1')])
        self.assert_unsupported(run, 'async_delivery_unverified')

        run = self.prepare('unmatched')
        check = self.check(run, 'UNMA0001')
        message = notice('bg_9', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        self.assert_unsupported(run, 'async_delivery_unverified')

    def test_multi_job_and_inline_and_malformed_shapes_stay_unsupported(self):
        run = self.prepare('multi')
        check = self.check(run, 'MULT0001')
        result = ack('bg_1')
        message = notice('bg_1', check, jobs=[{'jobId': 'bg_1', 'type': 'bash'},
                                              {'jobId': 'bg_2', 'type': 'bash'}])
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        self.assert_unsupported(run, 'async_notice_shape_unsupported')

        run = self.prepare('inline')
        check = self.check(run, 'INLI0001')
        result = ack('bg_1', state='completed')
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        self.assert_unsupported(run, 'async_ack_state_unsupported')

        run = self.prepare('malformed')
        check = self.check(run, 'MALF0001')
        result = {'content': [{'type': 'text', 'text': ACK_TEXT}],
                  'details': {'async': 'bg_1'}}
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        self.assert_unsupported(run)

    def test_legacy_markerless_and_conflicting_ack_marker_stay_unsupported(self):
        run = self.prepare('legacy')
        check = self.check(run, 'LEGA0001')
        result = {'content': [{'type': 'text', 'text': ACK_TEXT}],
                  'details': {'jobId': 'bg_1'}}
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = self.assert_unsupported(run, 'async_legacy_job_keys_unsupported')
        self.assertEqual(ledger['rows'][0]['exit_code'], e.UNKNOWN)

        run = self.prepare('ack-marker')
        ack_check = self.check(run, 'ACKM0001')
        check = self.check(run, 'ACKM0002')
        result = ack('bg_1', text='WORKER_TEST_LOG_DIR=%s\n' % ack_check)
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = self.assert_unsupported(run, 'async_ack_marker_conflict')
        self.assertEqual(ledger['rows'][0]['result_source'], 'terminal')

    def test_falsey_legacy_job_keys_never_look_synchronous(self):
        falsey = (('null', None), ('false', False), ('empty', ''), ('zero', 0))
        cases = [(key, label, value) for key in
                 ('jobId', 'job_id', 'backgroundJobId', 'asyncJobId')
                 for label, value in falsey]
        for index, (key, label, value) in enumerate(cases):
            with self.subTest(key=key, value=label):
                run = self.prepare('falsey-%s-%s' % (key, label))
                check = self.check(run, 'FALS%04d' % index)
                result = {'content': [{'type': 'text',
                                       'text': 'WORKER_TEST_LOG_DIR=%s\n' % check}],
                          'details': {key: value, 'exitCode': 0}}
                self.journals(run,
                              [private_start('call-1'), private_end('call-1', result)],
                              [compact_start('call-1'), compact_end('call-1', result)],
                              [decision('call-1')])
                ledger = self.assert_unsupported(run, 'async_legacy_job_keys_unsupported')
                row = ledger['rows'][0]
                self.assertEqual(row['log_status'], 'bound')
                self.assertEqual(row['exit_code'], 0)
                self.assertEqual(row['terminal_exit_code'], e.UNKNOWN)
                self.assertTrue(any(issue['kind'] == 'unsupported' for issue in row['issues']))

        run = self.prepare('async-null-legacy')
        check = self.check(run, 'ASNL0001')
        result = ack('bg_1')
        result['details']['jobId'] = None
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = self.assert_unsupported(run, 'async_legacy_job_keys_unsupported')
        self.assertEqual(ledger['rows'][0]['terminal_exit_code'], e.UNKNOWN)

        run = self.prepare('sync-clean')
        check = self.check(run, 'SYNC0002')
        result = {'content': [{'type': 'text',
                               'text': 'WORKER_TEST_LOG_DIR=%s\n' % check}],
                  'details': {'exitCode': 0}}
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result)],
                      [compact_start('call-1'), compact_end('call-1', result)],
                      [decision('call-1')])
        ledger = e.invocation_ledger(run)
        self.assertTrue(ledger['complete'], ledger['issues'])
        self.assertEqual(ledger['async']['state'], 'absent')
        row = ledger['rows'][0]
        self.assertIs(row['executed'], True)
        self.assertEqual(row['exit_code'], 0)
        self.assertEqual(row['terminal_exit_code'], 0)

    def test_async_ack_exit_code_never_becomes_terminal_exit(self):
        values = (('zero', 0), ('nonzero', 7), ('boolean', True), ('null', None),
                  ('string', '0'))
        for state in ('queued', 'running'):
            for index, (label, value) in enumerate(values):
                with self.subTest(state=state, exit=label):
                    run = self.prepare('ack-exit-%s-%s' % (state, label))
                    check = self.check(run, 'AEXI%02d%s' % (index, state[0].upper()))
                    result = ack('bg_1', state=state)
                    result['details']['exitCode'] = value
                    message = notice('bg_1', check)
                    self.journals(run,
                                  [private_start('call-1'), private_end('call-1', result),
                                   private_notice(message)],
                                  [compact_start('call-1'), compact_end('call-1', result),
                                   compact_notice(message)],
                                  [decision('call-1')])
                    ledger = self.assert_unsupported(run, 'async_ack_exit_code_unsupported')
                    self.assertEqual(ledger['async']['state'], 'unsupported')
                    row = ledger['rows'][0]
                    self.assertEqual(row['terminal_exit_code'], e.UNKNOWN)
                    self.assertEqual(row['exit_code'], e.UNKNOWN)
                    self.assertEqual(row['result_source'], 'terminal')

        for label, details, reason in (
                ('legacy', {'jobId': 'bg_1', 'exitCode': 0},
                 'async_legacy_job_keys_unsupported'),
                ('malformed', {'async': 'bg_1', 'exitCode': 0},
                 'async_ack_exit_code_unsupported')):
            with self.subTest(label=label):
                run = self.prepare('ack-exit-' + label)
                check = self.check(run, 'AEXI9%s' % label[0].upper())
                result = {'content': [{'type': 'text', 'text': ACK_TEXT}], 'details': details}
                message = notice('bg_1', check)
                self.journals(run,
                              [private_start('call-1'), private_end('call-1', result),
                               private_notice(message)],
                              [compact_start('call-1'), compact_end('call-1', result),
                               compact_notice(message)],
                              [decision('call-1')])
                ledger = self.assert_unsupported(run, reason)
                self.assertEqual(ledger['rows'][0]['terminal_exit_code'], e.UNKNOWN)

        for index, exit_code in enumerate((0, 7)):
            with self.subTest(absent='exitCode', exit=exit_code):
                run = self.prepare('ack-absent-%d' % exit_code)
                check = self.check(run, 'AEXA%04d' % index, exit_code=exit_code)
                result = ack('bg_1')
                self.assertNotIn('exitCode', result['details'])
                message = notice('bg_1', check, code=exit_code)
                self.journals(run,
                              [private_start('call-1'), private_end('call-1', result),
                               private_notice(message)],
                              [compact_start('call-1'), compact_end('call-1', result),
                               compact_notice(message)],
                              [decision('call-1')])
                ledger = e.invocation_ledger(run)
                self.assertTrue(ledger['complete'], ledger['issues'])
                row = ledger['rows'][0]
                self.assertEqual(row['exit_code'], exit_code)
                self.assertEqual(row['terminal_exit_code'], e.UNKNOWN)
                self.assertIs(row['executed'], True)

    def test_changed_policy_and_requested_async_flags_stay_unknown(self):
        run = self.prepare('policy', policy={'version': 1})
        check = self.check(run, 'POLI0001')
        result = ack('bg_1')
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        self.assert_unsupported(run, 'async_execution_policy_changed')

        run = self.prepare('mismatch')
        (run / 'metadata.json').write_text(
            json.dumps({'async_execution_policy': {'version': 1}}) + '\n')
        check = self.check(run, 'POLI0002')
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        self.assert_unsupported(run, 'async_execution_policy_changed')

        run = self.prepare('requested')
        check = self.check(run, 'REQU0001')
        result = ack('bg_1')
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1', **{'async': True}),
                       private_end('call-1', result), private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1', **{'async': True})])
        ledger = e.invocation_ledger(run)
        self.assertFalse(ledger['complete'])
        row = ledger['rows'][0]
        self.assertIsNotNone(row['async_binding'])
        self.assertIs(row['executed'], e.UNKNOWN)
        self.assertFalse(row['binding_complete'])
        self.assertTrue(any(issue['kind'] == 'unsupported' for issue in row['issues']))

    def test_compact_order_and_hash_mismatch_stay_unknown(self):
        run = self.prepare('order')
        check = self.check(run, 'ORDE0001')
        result = ack('bg_1')
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_end('call-1', result), compact_start('call-1'),
                       compact_notice(message)],
                      [decision('call-1')])
        self.assert_unsupported(run, 'async_delivery_unverified')

        run = self.prepare('hash')
        check = self.check(run, 'HASH0001')
        compact = compact_notice(message)
        compact['asyncResult']['message_sha256'] = '0' * 64
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result), compact],
                      [decision('call-1')])
        self.assert_unsupported(run, 'async_delivery_unverified')

    def test_notice_log_safety_rejects_bad_shared_and_ambiguous_paths(self):
        run = self.prepare('missing-exit')
        check = self.check(run, 'SAFE0001')
        (check / 'exit-code').unlink()
        result = ack('bg_1')
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = e.invocation_ledger(run)
        self.assertFalse(ledger['complete'])
        row = ledger['rows'][0]
        self.assertEqual(row['exit_code'], e.UNKNOWN)
        self.assertFalse(row['binding_complete'])
        self.assertIs(row['executed'], e.UNKNOWN)

        run = self.prepare('bad-exit')
        check = self.check(run, 'SAFE0002')
        (check / 'exit-code').write_bytes(b'not-a-status\n')
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = e.invocation_ledger(run)
        self.assertEqual(ledger['rows'][0]['exit_code'], e.UNKNOWN)
        self.assertFalse(ledger['complete'])

        run = self.prepare('ambiguous')
        first = self.check(run, 'SAFE0003')
        second = self.check(run, 'SAFE0004')
        content = [{'type': 'text', 'text': 'WORKER_TEST_LOG_DIR=%s\nWORKER_TEST_LOG_DIR=%s\n'
                                           % (first, second)}]
        message = notice('bg_1', first, content=content)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = e.invocation_ledger(run)
        row = ledger['rows'][0]
        self.assertEqual(row['log_status'], 'invalid')
        self.assertTrue(any(issue['kind'] == 'ambiguous' for issue in row['issues']))
        self.assertFalse(ledger['complete'])

        run = self.prepare('outside')
        outside = self.root / 'outside' / 'check-OUTS0001'
        outside.mkdir(parents=True)
        for name, raw in (('exit-code', b'0\n'), ('stdout.log', b''), ('stderr.log', b'')):
            (outside / name).write_bytes(raw)
        message = notice('bg_1', outside)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        ledger = e.invocation_ledger(run)
        self.assertEqual(ledger['rows'][0]['log_dir'], e.UNKNOWN)
        self.assertTrue(any(issue['kind'] == 'path' for issue in ledger['rows'][0]['issues']))
        self.assertFalse(ledger['complete'])

        run = self.prepare('shared')
        check = self.check(run, 'SHAR0001')
        first_result, first_notice = ack('bg_1'), notice('bg_1', check)
        second_result, second_notice = ack('bg_2'), notice('bg_2', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', first_result),
                       private_notice(first_notice),
                       private_start('call-2'), private_end('call-2', second_result),
                       private_notice(second_notice)],
                      [compact_start('call-1'), compact_end('call-1', first_result),
                       compact_notice(first_notice),
                       compact_start('call-2'), compact_end('call-2', second_result),
                       compact_notice(second_notice)],
                      [decision('call-1'), decision('call-2')])
        ledger = e.invocation_ledger(run)
        for row in ledger['rows']:
            self.assertTrue(any(issue['kind'] == 'conflict' for issue in row['issues']))
            self.assertIs(row['executed'], e.UNKNOWN)
            self.assertFalse(row['binding_complete'])
        self.assertFalse(ledger['complete'])

        run = self.prepare('leaf-link')
        check = self.check(run, 'LINK0001')
        target = self.root / 'decoy-exit'
        target.write_bytes(b'0\n')
        (check / 'exit-code').unlink()
        (check / 'exit-code').symlink_to(target)
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        row = e.invocation_ledger(run)['rows'][0]
        self.assertEqual(row['logs']['exit-code']['status'], 'symlink')
        self.assertFalse(row['binding_complete'])

    def test_async_evidence_bounds_and_truncation_fail_closed(self):
        cases = {}

        run = self.prepare('no-metadata', metadata=False)
        check = self.check(run, 'BOUN0001')
        result, message = ack('bg_1'), notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        cases['metadata-missing'] = run

        run = self.prepare('huge-metadata')
        (run / 'metadata.json').write_bytes(b'{' + b' ' * (e.JSON_LIMIT + 1) + b'}')
        check = self.check(run, 'BOUN0002')
        result, message = ack('bg_1'), notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        cases['metadata-oversized'] = run

        run = self.prepare('no-events')
        check = self.check(run, 'BOUN0003')
        result, message = ack('bg_1'), notice('bg_1', check)
        write_jsonl(run, 'tool-evidence.jsonl',
                    [private_start('call-1'), private_end('call-1', result),
                     private_notice(message)])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision('call-1')])
        cases['events-missing'] = run

        run = self.prepare('huge-events')
        check = self.check(run, 'BOUN0004')
        result, message = ack('bg_1'), notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        (run / 'events.jsonl').write_bytes(
            b'{"type":"x"}' + b' ' * (e.LEDGER_JSONL_LIMIT + 1) + b'\n')
        cases['events-oversized'] = run

        run = self.prepare('many-events')
        check = self.check(run, 'BOUN0005')
        result, message = ack('bg_1'), notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        write_jsonl(run, 'events.jsonl', [{'type': 'x'}] * 32769)
        cases['events-record-limit'] = run

        run = self.prepare('truncated-events')
        check = self.check(run, 'BOUN0006')
        other = self.check(run, 'BOUN0008')
        first_result, first_notice = ack('bg_1'), notice('bg_1', check)
        second_result, second_notice = ack('bg_2'), notice('bg_2', other)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', first_result),
                       private_notice(first_notice),
                       private_start('call-2'), private_end('call-2', second_result),
                       private_notice(second_notice)],
                      [compact_start('call-1'), compact_end('call-1', first_result),
                       compact_notice(first_notice),
                       compact_start('call-2'), compact_end('call-2', second_result),
                       compact_notice(second_notice)],
                      [decision('call-1'), decision('call-2')])
        (run / 'events.jsonl').write_bytes(
            (run / 'events.jsonl').read_bytes() + b'{"type":"partial"')
        cases['events-truncated'] = run

        run = self.prepare('metadata-symlink')
        (self.root / 'decoy-metadata.json').write_text('{}\n')
        (run / 'metadata.json').unlink()
        (run / 'metadata.json').symlink_to(self.root / 'decoy-metadata.json')
        check = self.check(run, 'BOUN0009')
        result, message = ack('bg_1'), notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        cases['metadata-symlink'] = run

        run = self.prepare('events-symlink')
        check = self.check(run, 'BOUN0010')
        result, message = ack('bg_1'), notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        (self.root / 'decoy-events.jsonl').write_text('{}\n')
        (run / 'events.jsonl').unlink()
        (run / 'events.jsonl').symlink_to(self.root / 'decoy-events.jsonl')
        cases['events-symlink'] = run

        run = self.prepare('duplicate-events')
        check = self.check(run, 'BOUN0007')
        result, message = ack('bg_1'), notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        (run / 'events.jsonl').write_bytes(b'{"type":"a","type":"b"}\n')
        cases['events-duplicate-keys'] = run

        for label, run in cases.items():
            with self.subTest(label=label):
                ledger = self.assert_unsupported(run)
                self.assertIsNone(ledger['rows'][0]['async_binding'])
                if label == 'events-truncated':
                    self.assertEqual(len(ledger['rows']), 2)

    def test_malformed_required_journals_cannot_leave_a_partial_async_success(self):
        def valid(run, token):
            check = self.check(run, token)
            result, message = ack('bg_1'), notice('bg_1', check)
            self.journals(run,
                          [private_start('call-1'), private_end('call-1', result),
                           private_notice(message)],
                          [compact_start('call-1'), compact_end('call-1', result),
                           compact_notice(message)],
                          [decision('call-1')])

        def append(run, name, raw):
            with (run / name).open('ab') as stream:
                stream.write(raw)

        cases = {}
        run = self.prepare('blank-tool-row')
        valid(run, 'PART0001')
        append(run, 'tool-evidence.jsonl', b'\n')
        cases['tool-blank-row'] = run

        run = self.prepare('truncated-tool-row')
        valid(run, 'PART0002')
        append(run, 'tool-evidence.jsonl', b'{"type":"tool_execution_start"')
        cases['tool-truncated'] = run

        run = self.prepare('tool-record-limit')
        valid(run, 'PART0003')
        append(run, 'tool-evidence.jsonl', b''.join(b'{"type":"x"}\n' for _ in range(2049)))
        cases['tool-record-limit'] = run

        run = self.prepare('tool-envelope')
        valid(run, 'PART0004')
        append(run, 'tool-evidence.jsonl',
               b'{"version":2,"type":"tool_execution_start","id":"call-2",'
               b'"args":{"command":"true"}}\n')
        cases['tool-envelope-version'] = run

        run = self.prepare('truncated-gate-row')
        valid(run, 'PART0005')
        append(run, 'bash-gate-events.jsonl', b'{"type":"decision"')
        cases['gate-truncated'] = run

        run = self.prepare('gate-unsupported-type')
        valid(run, 'PART0006')
        append(run, 'bash-gate-events.jsonl', b'{"type":"mystery","id":"call-9"}\n')
        cases['gate-unsupported-type'] = run

        run = self.prepare('gate-duplicate')
        valid(run, 'PART0007')
        append(run, 'bash-gate-events.jsonl',
               b'{"type":"decision","id":"call-1","args":{"command":%s},'
               b'"status":"allowed","decision":"allow","denials":0}\n'
               % json.dumps(COMMAND).encode())
        cases['gate-duplicate-decision'] = run

        for label, run in cases.items():
            with self.subTest(label=label):
                ledger = e.invocation_ledger(run)
                self.assertFalse(ledger['complete'])
                self.assertEqual(ledger['async']['state'], 'unsupported')
                self.assertEqual(ledger['async']['reason'], 'async_inputs_incomplete')
                self.assertTrue(any(issue['kind'] == 'async' for issue in ledger['issues']))
                for row in ledger['rows']:
                    self.assertIsNone(row['async_binding'])
                    self.assertEqual(row['executed'], e.UNKNOWN)
                    self.assertFalse(row['binding_complete'])

        run = self.prepare('sync-corruption')
        check = self.check(run, 'PART0008')
        write_jsonl(run, 'tool-evidence.jsonl', [
            {'version': 1, 'type': 'tool_execution_start', 'id': 'call-1',
             'args': {'command': COMMAND}},
            {'version': 1, 'type': 'tool_execution_end', 'id': 'call-1', 'is_error': False,
             'result': {'content': [{'type': 'text',
                                     'text': 'WORKER_TEST_LOG_DIR=%s\n' % check}],
                        'details': {'exitCode': 0}}}])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision('call-1')])
        append(run, 'tool-evidence.jsonl', b'\n')
        ledger = e.invocation_ledger(run)
        self.assertEqual(ledger['async']['state'], 'absent')
        self.assertFalse(ledger['complete'])
        self.assertIs(ledger['rows'][0]['executed'], True)

    def test_async_join_is_read_only_and_cli_reports_v2(self):
        run = self.prepare()
        check = self.check(run, 'READ0001')
        result = ack('bg_1')
        message = notice('bg_1', check)
        self.journals(run,
                      [private_start('call-1'), private_end('call-1', result),
                       private_notice(message)],
                      [compact_start('call-1'), compact_end('call-1', result),
                       compact_notice(message)],
                      [decision('call-1')])
        before = tree_snapshot(self.root)
        ledger = e.invocation_ledger(run)
        self.assertTrue(ledger['complete'], ledger['issues'])
        self.assertEqual(tree_snapshot(self.root), before)

        completed = cli('invocations', '--run', str(run))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        parsed = json.loads(completed.stdout)
        self.assertEqual(parsed['ledger_version'], 2)
        self.assertEqual(parsed['async']['state'], 'supported')
        self.assertIs(parsed['rows'][0]['executed'], True)
        self.assertIs(parsed['acceptance_certified'], False)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_non_async_run_never_reads_metadata_or_events(self):
        run = self.prepare()
        (run / 'metadata.json').write_text(json.dumps({'leak': MARKER}) + '\n')
        (run / 'events.jsonl').write_text(json.dumps({'leak': MARKER}) + '\n')
        check = self.check(run, 'SYNC0001')
        write_jsonl(run, 'tool-evidence.jsonl', [
            {'version': 1, 'type': 'tool_execution_start', 'id': 'call-1',
             'args': {'command': COMMAND}},
            {'version': 1, 'type': 'tool_execution_end', 'id': 'call-1', 'is_error': False,
             'result': {'content': [{'type': 'text',
                                     'text': 'WORKER_TEST_LOG_DIR=%s\n' % check}],
                        'details': {'exitCode': 0}}}])
        write_jsonl(run, 'bash-gate-events.jsonl', [decision('call-1')])
        ledger = e.invocation_ledger(run)
        self.assertTrue(ledger['complete'], ledger['issues'])
        self.assertEqual([source['file'] for source in ledger['sources']],
                         ['task.json', 'tool-evidence.jsonl', 'bash-gate-events.jsonl'])
        self.assertNotIn(MARKER, json.dumps(ledger))
        self.assertEqual(ledger['async']['state'], 'absent')
        self.assertEqual(ledger['rows'][0]['result_source'], 'terminal')
        self.assertIsNone(ledger['rows'][0]['async_binding'])


if __name__ == '__main__':
    unittest.main()
