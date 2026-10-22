import importlib.util
import copy
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('team_dev', Path(__file__).with_name('team_dev.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class AcceptanceInventoryTests(unittest.TestCase):
    def test_indexes_without_parsing_rewriting_or_dropping_original_clauses(self):
        task = {'acceptance': ['验证所有输入；失败保留证据。',
                               {'id': 'original/name', 'clauses': ['bytes', 'paths']},
                               None, '', {}]}
        original = copy.deepcopy(task)
        inventory = m.acceptance_inventory(task)
        self.assertEqual(inventory, {'version': 1, 'source_state': 'present', 'rows': [
            {'id': 'A1', 'source_pointer': '/acceptance/0'},
            {'id': 'A2', 'source_pointer': '/acceptance/1'},
            {'id': 'A3', 'source_pointer': '/acceptance/2'},
            {'id': 'A4', 'source_pointer': '/acceptance/3'},
            {'id': 'A5', 'source_pointer': '/acceptance/4'}]})
        # The inventory makes no semantic claim about compound or malformed entries.
        inventory['rows'][1]['id'] = 'edited report label'
        self.assertEqual(task, original)

    def test_legacy_acceptance_shapes_and_missing_values_remain_distinct(self):
        for value in ('contract.md: sections 1 and 2', {'a/b': ['x', 'y']}, False, 0):
            with self.subTest(value=value):
                task = {'acceptance': value}
                original = copy.deepcopy(task)
                self.assertEqual(m.acceptance_inventory(task), {
                    'version': 1, 'source_state': 'present',
                    'rows': [{'id': 'A1', 'source_pointer': '/acceptance'}]})
                self.assertEqual(task, original)
        for value in (None, [], {}, '', ' \t\n'):
            with self.subTest(empty=value):
                self.assertEqual(m.acceptance_inventory({'acceptance': value}), {
                    'version': 1, 'source_state': 'empty', 'rows': []})
        self.assertEqual(m.acceptance_inventory({}), {
            'version': 1, 'source_state': 'absent', 'rows': []})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        m.RUNS = self.root / 'runs'
        (self.repo / 'code.py').write_text('original\n')
        (self.repo / 'remove.txt').write_text('delete me\n')
        (self.repo / 'context.txt').write_text('user context\n')
        (self.repo / 'binary.dat').write_bytes(b'\x00\x01original')
        m.git(self.repo, 'init', '-q')
        m.git(self.repo, 'add', '-A')
        m.git(self.repo, '-c', 'user.name=Tests', '-c', 'user.email=tests@localhost',
              'commit', '-qm', 'baseline')
        (self.repo / 'context.txt').write_text('user dirty context\n')
        (self.repo / 'outside.txt').write_text('untracked user data\n')
        self.task = {'target': {'transport': 'local', 'repo': str(self.repo)},
                     'goal': 'test fixture', 'allowed_paths': ['code.py', 'new.txt', 'remove.txt', 'binary.dat'],
                     'context_paths': ['context.txt'], 'test_commands': [['/usr/bin/true']]}

    def prepare(self, worker_evidence=True):
        task = self.root / 'task.json'
        task.write_text(json.dumps(self.task))
        self.run = Path(m.prepare(task)['run'])
        if worker_evidence:
            # Explicit synthetic success fixture for bridge-only unit tests; no API is called.
            meta = m.load(self.run / 'metadata.json')
            meta.update(process='exited', exit_code=0)
            m.dump(self.run / 'metadata.json', meta)
            m.dump(self.run / 'worker-result.json', {'models': [m.MODEL.split('/', 1)[1]],
                                                   'errors': [], 'tools': ['read'], 'final_text': 'unit-test fixture'})
            # Synthetic zero-Bash process evidence for bridge-only gate tests.
            (self.run / 'events.jsonl').write_text(json.dumps({'type':'agent_end','isTerminal':True})+'\n')
            (self.run / 'tool-evidence.jsonl').write_text('')
            if 'worker_bash_gate' in meta:
                # Explicit synthetic registration for zero-Bash bridge tests.
                # Actual OMP registration/execution is tested separately.
                import bash_gate
                task = m.load(self.run / 'task.json')
                bash_gate.launch(self.run, task, meta, {}, [], '18.2.8')
                config = m.load(self.run / bash_gate.CONFIG)
                (self.run / bash_gate.JOURNAL).write_text(json.dumps(dict(type='ready', version=1,
                    nonce=config['nonce'], policy_sha256=config['policy_sha256']))+'\n')
        return self.run / 'snapshot'

    def change(self):
        snapshot = self.prepare()
        (snapshot / 'code.py').write_text('changed\n')
        m.propose(self.run)
        return snapshot

    def test_worker_requires_nonblank_string_completion_even_after_terminal_exit_zero(self):
        for final in ('\n\n', ' \t\r\n', {'text': 'not a string'}, ' valid completion\n'):
            with self.subTest(final_type=type(final).__name__, valid=isinstance(final, str) and bool(final.strip())):
                self.prepare(worker_evidence=False)
                m.dump(self.run/'progress.json', {'terminal_event': True})
                result = {'models': [m.MODEL.split('/', 1)[1]], 'errors': [], 'tools': ['read'],
                          'final_text': final}
                with mock.patch.object(m, 'worker_env', return_value={}), \
                     mock.patch.object(m.shutil, 'which', return_value='/fake/omp'), \
                     mock.patch.object(m, 'cmd', return_value=SimpleNamespace(stdout=b'18.2.8\n')), \
                     mock.patch('rpc_worker.execute', return_value=('exited', 0)), \
                     mock.patch.object(m, 'summarize_events', return_value=result):
                    meta = m.worker(self.run)
                self.assertEqual(m.load(self.run/'worker-result.json')['final_text'], final)
                valid = isinstance(final, str) and bool(final.strip())
                self.assertEqual(meta['process'], 'exited' if valid else 'failed')
                self.assertEqual(meta['delivery'], 'pending_review' if valid else 'worker_failed')
                if not valid:
                    self.assertEqual(m.load(self.run/'failure.json')['reason'], 'worker_evidence_invalid')

    def test_proposal_rejects_blank_or_nonstring_final_response_without_changing_evidence(self):
        snapshot = self.prepare()
        (snapshot/'code.py').write_text('candidate\n')
        for final in ('\n\n', ' \t', None, ['invalid'], {'text': 'invalid'}):
            with self.subTest(final_type=type(final).__name__):
                result = m.load(self.run/'worker-result.json')
                result['final_text'] = final
                m.dump(self.run/'worker-result.json', result)
                before = (self.run/'worker-result.json').read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'Worker success'):
                    m.propose(self.run)
                self.assertEqual((self.run/'worker-result.json').read_bytes(), before)
                self.assertFalse((self.run/'proposal.json').exists())
                self.assertEqual((self.repo/'code.py').read_text(), 'original\n')

    def test_candidate_and_report_after_auto_retry_cannot_clear_prior_model_error(self):
        snapshot = self.prepare(worker_evidence=False)
        (snapshot/'code.py').write_text('candidate\n')
        m.dump(self.run/'progress.json', {'terminal_event': True, 'changed_paths': ['code.py']})
        result = {'models': [m.MODEL.split('/', 1)[1]], 'tools': ['read', 'edit'],
                  'errors': ['The socket connection was closed unexpectedly.'],
                  'final_text': 'Candidate prepared; target dependency NOT VERIFIED',
                  'event_types': {'auto_retry_start': 1, 'auto_retry_end': 1}}
        with mock.patch.object(m, 'worker_env', return_value={}), \
             mock.patch.object(m.shutil, 'which', return_value='/fake/omp'), \
             mock.patch.object(m, 'cmd', return_value=SimpleNamespace(stdout=b'18.2.8\n')), \
             mock.patch('rpc_worker.execute', return_value=('exited', 0)), \
             mock.patch.object(m, 'summarize_events', return_value=result):
            meta = m.worker(self.run)
        self.assertEqual(meta['process'], 'failed')
        self.assertEqual(meta['delivery'], 'worker_failed')
        self.assertEqual(m.load(self.run/'failure.json')['reason'], 'worker_evidence_invalid')
        before = (self.run/'worker-result.json').read_bytes()
        # Even a claimed successful exit cannot bypass the independent error check.
        meta.update(process='exited', exit_code=0)
        with self.assertRaisesRegex(RuntimeError, 'Worker success'):
            m.require_worker(self.run, meta)
        with self.assertRaisesRegex(RuntimeError, 'Worker success'):
            m.propose(self.run)
        self.assertFalse((self.run/'proposal.json').exists())
        self.assertEqual((self.run/'worker-result.json').read_bytes(), before)
        self.assertEqual((snapshot/'code.py').read_text(), 'candidate\n')
        self.assertEqual((self.repo/'code.py').read_text(), 'original\n')

    def test_new_dispatch_includes_exact_integration_contract(self):
        self.task['acceptance'] = [{'requirement': 'identity equals independent pin',
                                   'public_path': 'load -> validate', 'negative_case': 'alter supplied identity'}]
        self.task.update(task_type='integration', interface_contracts=[{
            'name':'selected_arguments', 'inputs':'validated selection dictionary',
            'returns':'list[str] argv; never a receipt dictionary', 'source_paths':['context.txt']}],
            integration_test_commands=self.task['test_commands'])
        self.prepare(worker_evidence=False)
        prompt = (self.run/'prompt.md').read_text()
        payload = json.loads(prompt.split('\n\n', 1)[1])
        self.assertEqual(payload['test_commands'], self.task['test_commands'])
        self.assertEqual(payload['interface_contracts'], self.task['interface_contracts'])
        self.assertEqual(payload['integration_test_commands'], self.task['integration_test_commands'])
        self.assertEqual(payload['acceptance'], self.task['acceptance'])
        self.assertEqual(payload['worker_closeout_budget'], m.load(self.run/'metadata.json')['execution_policy']['closeout'])
        self.assertEqual(m.load(self.run/'task.json')['task_contract_version'], 2)

    def test_strict_prompt_catalog_contains_exact_ready_to_submit_bash_arguments(self):
        self.task['worker_bash_mode'] = 'declared_tests_only'
        self.prepare(worker_evidence=False)
        from worker_bash import inspect
        from worker_test_catalog import catalog
        task = m.load(self.run/'task.json')
        payload = json.loads((self.run/'prompt.md').read_text().split('\n\n', 1)[1])
        entries = payload['worker_test_invocations']
        self.assertEqual(entries, catalog(task, self.run))
        self.assertEqual([item['id'] for item in entries], ['T1.direct', 'T1.logged'])
        self.assertTrue(all(inspect(item['arguments'], task['worker_bash_policy'])['status'] == 'allowed'
                            for item in entries))
        self.assertEqual(entries[0]['arguments']['command'], payload['worker_direct_test_commands'][0])
        self.assertEqual(entries[1]['arguments']['command'], payload['worker_logged_test_commands'][0])
        self.assertEqual(task['worker_bash_gate']['version'], 2)
        self.assertIn('lookup labels, not executable commands', (self.run/'prompt.md').read_text())

    def test_prepare_indexes_original_acceptance_and_does_not_rewrite_earlier_run(self):
        self.task['acceptance'] = ['保持输入字节；拒绝无效路径',
                                   {'id': 'contract-9', 'file': 'context.txt', 'clauses': ['one', 'two']}]
        original = copy.deepcopy(self.task['acceptance'])
        self.prepare(worker_evidence=False)
        prior = self.run
        frozen = {name: (prior/name).read_bytes() for name in ('task.json', 'prompt.md')}
        payload = json.loads((prior/'prompt.md').read_text().split('\n\n', 1)[1])
        self.assertEqual(payload['acceptance'], original)
        self.assertEqual(m.load(prior/'task.json')['acceptance'], original)
        self.assertEqual(payload['worker_acceptance_inventory'], {
            'version': 1, 'source_state': 'present', 'rows': [
                {'id': 'A1', 'source_pointer': '/acceptance/0'},
                {'id': 'A2', 'source_pointer': '/acceptance/1'}]})
        # A fresh preparation can have different local IDs without touching prior evidence.
        self.task['acceptance'] = {'remaining': original[1]}
        self.prepare(worker_evidence=False)
        next_payload = json.loads((self.run/'prompt.md').read_text().split('\n\n', 1)[1])
        self.assertEqual(next_payload['acceptance'], self.task['acceptance'])
        self.assertEqual(next_payload['worker_acceptance_inventory']['rows'], [
            {'id': 'A1', 'source_pointer': '/acceptance'}])
        self.assertEqual({name: (prior/name).read_bytes() for name in frozen}, frozen)

    def test_new_prompt_keeps_forbidden_model_guard_and_fault_reachability_conditional(self):
        self.task['constraints'] = ['No real model construction or inference during tests.']
        self.prepare(worker_evidence=False)
        prompt = (self.run/'prompt.md').read_text()
        self.assertIn('When this task forbids real model calls', prompt)
        self.assertIn('Latch forbidden attempts and assert none in fixture cleanup', prompt)
        self.assertIn('assert the intended error/result and expected call count/arguments', prompt)
        self.assertIn('all-skipped local tests remain NOT VERIFIED', prompt)
        payload = json.loads(prompt.split('\n\n', 1)[1])
        self.assertEqual(payload['constraints'], self.task['constraints'])
        self.assertEqual(payload['test_commands'], self.task['test_commands'])

    def test_worker_commands_preserve_argv_and_prevent_bytecode_for_real_import(self):
        (self.repo/'code.py').write_text('VALUE = 7\n')
        self.task['test_commands'] = [[sys.executable, '-c', 'import code; print(code.VALUE)']]
        snapshot = self.prepare(worker_evidence=False)
        payload = json.loads((self.run/'prompt.md').read_text().split('\n\n', 1)[1])
        argv = payload['worker_test_commands'][0]
        self.assertEqual(argv, ['env', 'PYTHONDONTWRITEBYTECODE=1'] + self.task['test_commands'][0])
        self.assertEqual(m.load(self.run/'task.json')['test_commands'], self.task['test_commands'])
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='0')
        result = subprocess.run(argv, cwd=snapshot, env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), '7')
        self.assertFalse((snapshot/'__pycache__').exists())

    def test_prepared_logged_test_keeps_full_streams_failure_and_each_attempt(self):
        code = 'import os,sys; os.write(1,b"A"*200000+b"\\x00END\\n"); os.write(2,b"B"*180000+b"ERR\\n"); sys.exit(7)'
        self.task['test_commands'] = [[sys.executable, '-B', '-c', code]]
        snapshot = self.prepare(worker_evidence=False)
        payload = json.loads((self.run/'prompt.md').read_text().split('\n\n', 1)[1])
        log_root = Path(payload['worker_test_log_root'])
        self.assertFalse(log_root.is_relative_to(snapshot))
        self.assertEqual(log_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(m.load(self.run/'task.json')['test_commands'], self.task['test_commands'])
        snapshots = []
        for _ in range(2):
            result = subprocess.run(['/bin/sh', '-ec', payload['worker_logged_test_commands'][0]],
                                    cwd=snapshot, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertLess(len(result.stdout), 1024)
            directory = Path(result.stdout.decode().strip().split('=', 1)[1])
            self.assertEqual(directory.parent, log_root)
            self.assertEqual((directory/'stdout.log').read_bytes(), b'A'*200000+b'\x00END\n')
            self.assertEqual((directory/'stderr.log').read_bytes(), b'B'*180000+b'ERR\n')
            self.assertEqual((directory/'exit-code').read_text(), '7\n')
            templates = payload['worker_test_log_read_templates']
            self.assertEqual(len(templates), 3)
            resolved = []
            for template in templates:
                self.assertEqual(template['tool'], 'read')
                self.assertEqual(set(template['arguments']), {'path'})
                request_path = template['arguments']['path'].replace('<WORKER_TEST_LOG_DIR>', str(directory))
                source_path = Path(request_path.removesuffix(':1-200'))
                self.assertEqual(source_path.parent, directory)
                self.assertTrue(source_path.is_file())
                resolved.append(source_path.name)
            self.assertEqual(resolved, ['exit-code', 'stdout.log', 'stderr.log'])
            self.assertEqual(templates[0]['arguments']['path'].split('/')[-1], 'exit-code')
            self.assertTrue(all(t['arguments']['path'].endswith(':1-200') for t in templates[1:]))
            for path in directory.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            snapshots.append({p: p.read_bytes() for p in directory.iterdir()})
        self.assertEqual(len(list(log_root.iterdir())), 2)
        self.assertEqual(snapshots[0], {p: p.read_bytes() for p in snapshots[0]})
        self.assertEqual(m.git(snapshot, 'status', '--porcelain'), b'')

    def test_logged_test_quotes_argv_and_paths_without_shell_expansion(self):
        log_root = self.root / "log root ' literal"
        log_root.mkdir(mode=0o700)
        literal = "literal $(touch unintended) `touch unintended2`; ' \" \n"
        argv = [sys.executable, '-B', '-c', 'import json,sys; print(json.dumps(sys.argv[1:]))', literal]
        command = m.logged_worker_test_command(argv, log_root)
        result = subprocess.run(['/bin/sh', '-c', command], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = Path(result.stdout.decode().strip().split('=', 1)[1])
        self.assertEqual(json.loads((directory/'stdout.log').read_text()), [literal])
        self.assertEqual((directory/'exit-code').read_text(), '0\n')
        self.assertFalse((self.root/'unintended').exists())
        self.assertFalse((self.root/'unintended2').exists())

    def test_logged_test_allocation_failure_does_not_execute_test(self):
        marker = self.root/'must-not-run'
        command = m.logged_worker_test_command(
            [sys.executable, '-B', '-c', 'from pathlib import Path; Path("must-not-run").touch()'],
            self.root/'missing-parent')
        result = subprocess.run(['/bin/sh', '-c', command], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 125)
        self.assertFalse(marker.exists())

    def test_resume_preserves_previous_command_audit(self):
        self.failed_rpc()
        previous = {'version': 1, 'bash_calls': 1, 'compliance': 'not_assessed', 'review_required': True}
        m.dump(self.run/'command-audit.json', previous)
        saved = json.dumps({'version':1,'type':'tool_execution_start','id':'failed-read',
                            'args':{'command':'cat code.py'}})+'\n'
        (self.run/'tool-evidence.jsonl').write_text(saved)
        with mock.patch.object(m, 'start', return_value={'started': True}):
            m.resume(self.run, self.recovery)
        self.assertEqual(m.load(self.run/'attempt-1/command-audit.json'), previous)
        self.assertFalse((self.run/'command-audit.json').exists())
        self.assertEqual((self.run/'attempt-1/tool-evidence.jsonl').read_text(),saved)
        self.assertFalse((self.run/'tool-evidence.jsonl').exists())

    def test_strict_gate_zero_bash_stream_failure_requires_fresh_run_without_mutation(self):
        self.task['worker_bash_mode'] = 'declared_tests_only'
        self.prepare()
        meta = m.load(self.run/'metadata.json')
        meta.update(process='failed', delivery='worker_failed')
        m.dump(self.run/'metadata.json', meta)
        m.dump(self.run/'failure.json', {'reason': 'worker_evidence_invalid'})
        m.dump(self.run/'worker-result.json', {'models': [m.MODEL.split('/', 1)[1]], 'tools': ['read'],
            'terminal_event': True, 'final_text': '\n\n',
            'errors': ['The socket connection was closed unexpectedly.']})
        evidence = ['metadata.json', 'failure.json', 'worker-result.json', 'events.jsonl',
                    'tool-evidence.jsonl', 'bash-gate-events.jsonl']
        before = {name: (self.run/name).read_bytes() for name in evidence}
        with mock.patch.object(m, 'start') as start:
            with self.assertRaisesRegex(RuntimeError, 'even with zero Bash calls or denials'):
                m.resume(self.run, self.run/'unused-recovery-message')
            start.assert_not_called()
        self.assertFalse((self.run/'attempt-1').exists())
        self.assertEqual(before, {name: (self.run/name).read_bytes() for name in evidence})

    def test_process_constraint_failure_cannot_resume_and_reset_evidence(self):
        self.failed_rpc()
        m.dump(self.run/'failure.json',{'reason':'process_constraint_request'})
        (self.run/'tool-evidence.jsonl').write_text('preserved failed request\n')
        before=(self.run/'metadata.json').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'fresh task or independent takeover'):
            m.resume(self.run,self.recovery)
        self.assertEqual((self.run/'metadata.json').read_bytes(),before)
        self.assertFalse((self.run/'attempt-1').exists())
        self.assertEqual((self.run/'tool-evidence.jsonl').read_text(),'preserved failed request\n')

    def test_new_prepare_rejects_shell_test_wrapper_before_capture(self):
        self.task['test_commands']=[['sh','-c','python3 -m unittest | tail -60']]
        with mock.patch.object(m.Repo,'capture') as capture:
            with self.assertRaisesRegex(ValueError,'requires direct argv'):
                self.prepare(worker_evidence=False)
            capture.assert_not_called()

    def test_async_attempt_cannot_resume_and_forget_owned_resources(self):
        from async_evidence import bash_async
        result={'details':{'async':{'jobId':'bg_3','state':'running','type':'bash'}}}
        for name,event in (('tool-evidence.jsonl',{'version':1,'type':'tool_execution_end','id':'one','result':result}),
                           ('events.jsonl',{'type':'tool_execution_end','toolName':'bash','toolCallId':'one',
                                            'asyncBash':bash_async(result)})):
            with self.subTest(name=name):
                self.failed_rpc();(self.run/name).write_text(json.dumps(event)+'\n')
                before=(self.run/'metadata.json').read_bytes()
                with mock.patch.object(m,'start') as start:
                    with self.assertRaisesRegex(RuntimeError,'coordinator resource inspection'):
                        m.resume(self.run,self.recovery)
                    start.assert_not_called()
                self.assertEqual((self.run/'metadata.json').read_bytes(),before)
                self.assertFalse((self.run/'attempt-1').exists())

    def test_cancel_cannot_resume_observed_prohibited_request_in_either_journal(self):
        from command_audit import inspect_bash
        args = {'command':'env PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_code -v 2>&1 | tail -60'}
        compact = {'type':'tool_execution_start','toolName':'bash','toolCallId':'pipe',
                   'commandAudit':inspect_bash(args)}
        private = {'version':1,'type':'tool_execution_start','id':'pipe','args':args}
        for journals in (('events.jsonl',),('tool-evidence.jsonl',),('events.jsonl','tool-evidence.jsonl')):
            with self.subTest(journals=journals):
                self.failed_rpc()
                meta=m.load(self.run/'metadata.json');meta['process']='cancelled';m.dump(self.run/'metadata.json',meta)
                m.dump(self.run/'failure.json',{'reason':'cancel_requested'})
                for name in journals:
                    (self.run/name).write_text(json.dumps(compact if name=='events.jsonl' else private)+'\n')
                before={p.name:p.read_bytes() for p in self.run.iterdir() if p.is_file()}
                with mock.patch.object(m,'start') as start:
                    with self.assertRaisesRegex(RuntimeError,'fresh task or independent takeover'):
                        m.resume(self.run,self.recovery)
                    start.assert_not_called()
                for name,data in before.items():self.assertEqual((self.run/name).read_bytes(),data)
                self.assertFalse((self.run/'attempt-1').exists())
                self.assertEqual(m.load(self.run/'failure.json')['reason'],'cancel_requested')

    def test_resume_rejects_malformed_existing_process_journal_without_mutation(self):
        for name in ('events.jsonl','tool-evidence.jsonl'):
            with self.subTest(name=name):
                self.failed_rpc()
                (self.run/name).write_text('{"partial":')
                before=(self.run/'metadata.json').read_bytes()
                with mock.patch.object(m,'start') as start:
                    with self.assertRaisesRegex(RuntimeError,'Incomplete process evidence'):
                        m.resume(self.run,self.recovery)
                    start.assert_not_called()
                self.assertEqual((self.run/'metadata.json').read_bytes(),before)
                self.assertFalse((self.run/'attempt-1').exists())

    def test_integration_without_explicit_contract_is_rejected(self):
        self.task['task_type'] = 'integration'
        with self.assertRaisesRegex(ValueError, 'Integration tasks require'):
            self.prepare(worker_evidence=False)

    def test_interface_source_must_be_captured_and_exist(self):
        self.task['interface_contracts'] = [{'name':'api','inputs':'x','returns':'y','source_paths':['missing.py']}]
        with self.assertRaisesRegex(ValueError, 'selected context'):
            self.prepare(worker_evidence=False)
        self.task['interface_contracts'][0]['source_paths'] = ['new.txt']
        with self.assertRaisesRegex(ValueError, 'must exist'):
            self.prepare(worker_evidence=False)

    def test_stub_builder_in_test_rejected_before_proposal(self):
        self.task['allowed_paths'].append('tests/test_integration.py')
        snapshot = self.prepare()
        (snapshot/'tests').mkdir()
        (snapshot/'tests/test_integration.py').write_text(
            'import sys\ndef install(name):\n    sys.modules[name] = object()\ninstall("real_module")\n')
        with self.assertRaisesRegex(ValueError, 'test_isolation_violation'):
            m.propose(self.run)
        self.assertFalse((self.run/'proposal.json').exists())

    def test_real_zero_collection_cannot_pass(self):
        self.task['allowed_paths'] = ['test_unit.py']
        self.task['test_commands'] = [[sys.executable, '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_unit.py', '-v']]
        snapshot = self.prepare()
        (snapshot/'test_unit.py').write_text('def test_never_collected():\n    assert False\n')
        m.propose(self.run); m.apply(self.run)
        result = m.test_target(self.run)
        # Python 3.14 returns 5 for empty discovery; earlier runtimes return 0.
        # The explicit collection gate must reject it on either runtime.
        self.assertIn(result['tests'][0]['exit_code'], (0, 5))
        self.assertEqual(result['tests'][0]['collection']['count'], 0)
        self.assertFalse(result['passed'])
        self.assertEqual(m.status(self.run)['delivery'], 'tests_failed')

    def test_default_unittest_filename_variants_cannot_bypass_isolation(self):
        self.task['allowed_paths'] = ['testfoo.py', 'tests.py']
        snapshot = self.prepare()
        for name in self.task['allowed_paths']:
            with self.subTest(path=name):
                candidate = snapshot/name
                candidate.write_text('import sys\nsys.modules["real_project"] = object()\n')
                with self.assertRaisesRegex(ValueError, 'test_isolation_violation'):
                    m.propose(self.run)
                candidate.unlink()
        self.assertFalse((self.run/'proposal.json').exists())

    def test_interpreter_flags_empty_discovery_cannot_pass(self):
        self.task['allowed_paths'] = ['testfoo.py']
        self.task['test_commands'] = [[sys.executable, '-I', '-B', '-m', 'unittest', 'discover', '-s', '.', '-p', 'testfoo.py']]
        snapshot = self.prepare()
        (snapshot/'testfoo.py').write_text('def test_not_collected_by_unittest():\n    assert False\n')
        m.propose(self.run); m.apply(self.run)
        result = m.test_target(self.run)
        self.assertEqual(result['tests'][0]['collection']['reason'], 'no_tests_collected')
        self.assertFalse(result['passed'])
        self.assertEqual(m.status(self.run)['delivery'], 'tests_failed')

    def test_real_unittest_and_non_test_command_can_pass(self):
        self.task['allowed_paths'] = ['test_unit.py']
        self.task['test_commands'] = [[sys.executable, '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_unit.py', '-v'], ['/usr/bin/true']]
        snapshot = self.prepare()
        (snapshot/'test_unit.py').write_text('import unittest\nclass Tests(unittest.TestCase):\n    def test_truth(self):\n        self.assertEqual(2+2,4)\n')
        m.propose(self.run); m.apply(self.run)
        result = m.test_target(self.run)
        self.assertTrue(result['passed'])
        self.assertEqual(result['tests'][0]['collection']['count'], 1)
        self.assertIsNone(result['tests'][1]['collection']['framework'])

    def test_legacy_contract_does_not_retroactively_scan_files(self):
        with mock.patch.object(m.Repo, 'file', side_effect=AssertionError('must not scan legacy run')):
            m.check_test_isolation(m.Repo(self.task['target']), {}, ['tests/test_old.py'])

    def test_prepare_freezes_assessed_auto_budget(self):
        self.task.update(worker_thinking='high', budget_mode='auto',
                         effort_assessment={'by': 'astra', 'reason': 'Bounded concurrency change'},
                         progress={'stall_seconds': 180}, timeout_seconds=600)
        self.prepare(worker_evidence=False)
        saved = m.load(self.run / 'task.json')
        policy = saved['execution_policy']
        self.assertEqual(policy['progress'], {'warn_seconds': 240, 'stall_seconds': 600,
                                              'first_write_seconds': 900})
        self.assertEqual(policy['timeout_seconds'], 1800)
        self.assertEqual(policy['effort_assessment'], self.task['effort_assessment'])
        self.assertEqual(policy, m.load(self.run / 'metadata.json')['execution_policy'])
        self.assertEqual(saved['progress'], policy['progress'])

    def test_prepare_resolves_and_freezes_ssh_keepalive_without_remote_runtime(self):
        self.task['target'].update(transport='ssh', host='test-alias',
                                  ssh_keepalive={'server_alive_interval': 5})
        # Exercise real capture on a local fixture; replace only the SSH boundary.
        def fixture_transport(repo, argv, **kwargs):
            return m.cmd(argv, cwd=repo.repo, **kwargs)
        with mock.patch.object(m.Repo, 'run', autospec=True, side_effect=fixture_transport):
            self.prepare(worker_evidence=False)
        saved = m.load(self.run / 'task.json')
        meta = m.load(self.run / 'metadata.json')
        expected = {'server_alive_interval': 5, 'server_alive_count_max': 2}
        self.assertEqual(saved['target']['ssh_keepalive'], expected)
        self.assertEqual(saved['transport_policy'], meta['transport_policy'])
        self.assertEqual(m.status(self.run)['transport_policy']['ssh_keepalive'], expected)
        repo = m.target_repo(saved, meta)
        with mock.patch.object(m, 'cmd') as execute:
            repo.run(['git', 'status'])
        self.assertIn('ServerAliveInterval=5', execute.call_args.args[0])

    def test_invalid_keepalive_rejected_before_source_commands(self):
        self.task['target'].update(transport='ssh', host='test-alias',
                                  ssh_keepalive={'server_alive_interval': '5'})
        with mock.patch.object(m.Repo, 'run') as execute:
            with self.assertRaises(ValueError):
                self.prepare(worker_evidence=False)
        execute.assert_not_called()
        self.assertFalse(m.RUNS.exists())

    def test_changed_transport_policy_blocks_start_before_credentials(self):
        self.prepare(worker_evidence=False)
        task = m.load(self.run / 'task.json')
        task['transport_policy']['transport'] = 'ssh'
        m.dump(self.run / 'task.json', task)
        with mock.patch.object(m, 'worker_env') as env:
            with self.assertRaisesRegex(ValueError, 'Transport policy changed'):
                m.start(self.run)
            env.assert_not_called()

    def test_changed_transport_policy_blocks_worker_before_credentials(self):
        self.prepare(worker_evidence=False)
        task = m.load(self.run / 'task.json')
        task.pop('transport_policy')
        m.dump(self.run / 'task.json', task)
        with mock.patch.object(m, 'worker_env') as env:
            with self.assertRaisesRegex(ValueError, 'Transport policy changed'):
                m.worker(self.run)
            env.assert_not_called()

    def test_changed_transport_policy_blocks_delivery_before_target_commands(self):
        self.change(); m.apply(self.run); m.test_target(self.run); self.record_pass()
        task = m.load(self.run / 'task.json')
        task['transport_policy']['transport'] = 'ssh'
        m.dump(self.run / 'task.json', task)
        before = (self.run / 'metadata.json').read_bytes()
        tests = (self.run / 'tests.json').read_bytes()
        for operation in (m.apply, m.test_target, m.finalize):
            with self.subTest(operation=operation.__name__), mock.patch.object(m.Repo, 'run') as execute:
                with self.assertRaisesRegex(ValueError, 'Transport policy changed'):
                    operation(self.run)
                execute.assert_not_called()
                self.assertEqual((self.run / 'metadata.json').read_bytes(), before)
                self.assertEqual((self.run / 'tests.json').read_bytes(), tests)

    def test_changed_transport_policy_blocks_resume_without_archiving(self):
        self.failed_rpc()
        task = m.load(self.run / 'task.json')
        task['transport_policy']['transport'] = 'ssh'
        m.dump(self.run / 'task.json', task)
        before = (self.run / 'metadata.json').read_bytes()
        with mock.patch.object(m.Repo, 'run') as execute:
            with self.assertRaisesRegex(ValueError, 'Transport policy changed'):
                m.resume(self.run, self.recovery)
            execute.assert_not_called()
        self.assertFalse((self.run / 'attempt-1').exists())
        self.assertEqual((self.run / 'metadata.json').read_bytes(), before)

    def test_cancel_and_status_available_with_bad_transport_policy(self):
        self.prepare(worker_evidence=False)
        task = m.load(self.run / 'task.json'); task['transport_policy'] = None
        m.dump(self.run / 'task.json', task)
        meta = m.load(self.run / 'metadata.json'); meta.update(process='running', worker_mode='rpc')
        m.dump(self.run / 'metadata.json', meta)
        with mock.patch.object(m, 'cmd') as execute:
            self.assertIn('transport_policy', m.status(self.run))
            self.assertTrue(m.cancel(self.run)['cancel_requested'])
            execute.assert_not_called()
        self.assertTrue((self.run / 'cancel-requested').exists())

    def test_custom_saved_budget_does_not_revert_to_profile(self):
        self.task.update(worker_thinking='max', budget_mode='custom', timeout_seconds=60,
                         progress={'warn_seconds': 3, 'stall_seconds': 8, 'first_write_seconds': 10})
        self.prepare(worker_evidence=False)
        saved = m.load(self.run / 'task.json')
        original = copy.deepcopy(saved)
        m.validate_worker_options(saved, expected_policy=original['execution_policy'])
        self.assertEqual(saved, original)

    def test_frozen_auto_policy_uses_explicit_numbers_on_launch(self):
        from execution_policy import resolve_policy
        self.prepare(worker_evidence=False)
        saved = m.load(self.run / 'task.json')
        original = copy.deepcopy(saved)
        def resolve_without_new_defaults(task, **kwargs):
            self.assertEqual(task['budget_mode'], 'custom')
            self.assertEqual(task['progress'], original['progress'])
            self.assertEqual(task['timeout_seconds'], original['timeout_seconds'])
            return resolve_policy(task, **kwargs)
        with mock.patch('execution_policy.resolve_policy', side_effect=resolve_without_new_defaults):
            m.validate_worker_options(saved)
        self.assertEqual(saved, original)

    def test_frozen_policy_changes_rejected_before_credentials(self):
        self.prepare(worker_evidence=False)
        saved = m.load(self.run / 'task.json')
        for key, value in (('worker_thinking', 'max'), ('timeout_seconds', 3600),
                           ('progress', {'warn_seconds': 1, 'stall_seconds': 2, 'first_write_seconds': 3}),
                           ('execution_policy', None)):
            with self.subTest(key=key):
                changed = copy.deepcopy(saved)
                changed[key] = value
                m.dump(self.run / 'task.json', changed)
                with mock.patch.object(m, 'worker_env') as env:
                    with self.assertRaisesRegex(ValueError, 'Execution policy changed'):
                        m.worker(self.run)
                    env.assert_not_called()

    def test_legacy_options_keep_historical_defaults_and_overrides(self):
        from progress_monitor import ProgressMonitor
        legacy = {}
        m.validate_worker_options(legacy)
        self.assertEqual(legacy, {'worker_thinking': 'low'})
        monitor = ProgressMonitor(0, **legacy.get('progress', {}))
        self.assertEqual((monitor.warn_seconds, monitor.stall_seconds, monitor.first_write_seconds),
                         (120, 180, 240))
        self.assertEqual(legacy.get('timeout_seconds', 600), 600)

    def test_missing_or_null_execution_metadata_blocks_start_and_worker_before_credentials(self):
        for action in (m.start, m.worker):
            for missing in (True, False):
                with self.subTest(action=action.__name__, missing=missing):
                    self.prepare(worker_evidence=False)
                    meta = m.load(self.run/'metadata.json')
                    if missing:
                        meta.pop('execution_policy')
                    else:
                        meta['execution_policy'] = None
                    m.dump(self.run/'metadata.json', meta)
                    with mock.patch.object(m, 'worker_env') as env, mock.patch.object(m.subprocess, 'Popen') as launch:
                        with self.assertRaisesRegex(ValueError, 'Execution policy changed'):
                            action(self.run)
                        env.assert_not_called(); launch.assert_not_called()
                    self.assertFalse((self.run/'invocation.json').exists())
                    if action is m.start:
                        self.assertEqual(m.load(self.run/'metadata.json')['process'], 'not_started')

    def test_missing_or_null_execution_metadata_blocks_resume_without_archive_or_launch(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.failed_rpc()
                meta = m.load(self.run/'metadata.json')
                if missing:
                    meta.pop('execution_policy')
                else:
                    meta['execution_policy'] = None
                m.dump(self.run/'metadata.json', meta)
                before = {p.name:p.read_bytes() for p in self.run.iterdir() if p.is_file()}
                with mock.patch.object(m, 'start') as start, mock.patch.object(m, 'target_repo') as target:
                    with self.assertRaisesRegex(ValueError, 'Execution policy changed'):
                        m.resume(self.run, self.recovery)
                    start.assert_not_called(); target.assert_not_called()
                self.assertFalse((self.run/'attempt-1').exists())
                self.assertEqual(before, {p.name:p.read_bytes() for p in self.run.iterdir()
                                          if p.is_file() and p.name in before})
        legacy = {'worker_thinking': 'high', 'timeout_seconds': 777,
                  'progress': {'warn_seconds': 2, 'stall_seconds': 5, 'first_write_seconds': 9}}
        original = copy.deepcopy(legacy)
        m.validate_worker_options(legacy)
        self.assertEqual(legacy, original)

    def test_policy_cli_is_read_only_and_does_not_need_target(self):
        task = self.root / 'policy-only.json'
        task.write_text(json.dumps({'worker_thinking': 'max', 'budget_mode': 'auto'}))
        before = task.read_bytes()
        result = subprocess.run([sys.executable, str(Path(m.__file__)), 'policy', '--task', str(task)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['timeout_seconds'], 2400)
        self.assertEqual(task.read_bytes(), before)
        self.assertFalse(m.RUNS.exists())

    def test_binary_new_delete_mode_and_dirty_preservation(self):
        self.task['allowed_deletions'] = ['remove.txt']
        snapshot = self.prepare()
        (snapshot / 'code.py').write_text('changed\n')
        (snapshot / 'code.py').chmod(0o755)
        (snapshot / 'new.txt').write_text('新增\n')
        (snapshot / 'remove.txt').unlink()
        (snapshot / 'binary.dat').write_bytes(b'\0\xffupdated')
        before_index = m.git(self.repo, 'write-tree')
        proposal = m.propose(self.run)
        self.assertEqual(set(proposal['paths']), set(self.task['allowed_paths']))
        m.apply(self.run)
        self.assertEqual((self.repo / 'new.txt').read_text(), '新增\n')
        self.assertFalse((self.repo / 'remove.txt').exists())
        self.assertTrue((self.repo / 'code.py').stat().st_mode & 0o111)
        self.assertEqual((self.repo / 'binary.dat').read_bytes(), b'\0\xffupdated')
        self.assertEqual((self.repo / 'context.txt').read_text(), 'user dirty context\n')
        self.assertEqual((self.repo / 'outside.txt').read_text(), 'untracked user data\n')
        self.assertEqual(before_index, m.git(self.repo, 'write-tree'))

    def test_new_snapshot_guard_and_deletions_are_frozen(self):
        self.task['allowed_deletions'] = ['remove.txt']
        self.prepare(worker_evidence=False)
        meta = m.load(self.run/'metadata.json'); task = m.load(self.run/'task.json')
        self.assertEqual(meta['snapshot_guard'], {'version':1, 'allowed_deletions':['remove.txt']})
        self.assertEqual(task['snapshot_guard_version'], 1)
        payload = json.loads((self.run/'prompt.md').read_text().split('\n\n', 1)[1])
        self.assertEqual(payload['allowed_deletions'], ['remove.txt'])
        task['allowed_deletions'] = []
        with self.assertRaisesRegex(ValueError, 'snapshot_guard_policy_changed'):
            m.snapshot_changes(self.run, meta, task)

    def test_invalid_or_absent_source_deletion_permission_rejected(self):
        for paths in (['context.txt'], ['new.txt'], 'remove.txt'):
            self.task['allowed_deletions'] = paths
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, 'allowed_deletions'):
                self.prepare(worker_evidence=False)

    def test_undeclared_deletion_cannot_be_proposed(self):
        snapshot = self.prepare()
        (snapshot/'code.py').unlink()
        with self.assertRaisesRegex(ValueError, 'snapshot_undeclared_deletion'):
            m.propose(self.run)
        self.assertFalse((self.run/'proposal.json').exists())
        self.assertEqual((self.repo/'code.py').read_text(), 'original\n')

    def test_missing_or_null_guard_metadata_cannot_downgrade_proposal(self):
        for corrupt in ('missing', 'null'):
            with self.subTest(corrupt=corrupt):
                snapshot = self.prepare()
                meta = m.load(self.run/'metadata.json')
                if corrupt == 'missing': meta.pop('snapshot_guard')
                else: meta['snapshot_guard'] = None
                m.dump(self.run/'metadata.json', meta)
                (snapshot/'code.py').unlink()
                with self.assertRaisesRegex(ValueError, 'snapshot_guard_policy_changed'):
                    m.propose(self.run)
                self.assertFalse((self.run/'proposal.json').exists())

    def test_missing_guard_metadata_fails_worker_before_omp_launch(self):
        self.prepare(worker_evidence=False)
        meta = m.load(self.run/'metadata.json'); meta.pop('snapshot_guard')
        m.dump(self.run/'metadata.json', meta)
        with mock.patch.object(m, 'worker_env', return_value={}), \
             mock.patch.object(m.shutil, 'which', return_value='/fake/omp'), \
             mock.patch.object(m, 'cmd', return_value=SimpleNamespace(stdout=b'fake version\n')), \
             mock.patch('subprocess.Popen') as launch:
            result = m.worker(self.run)
            launch.assert_not_called()
        self.assertEqual(result['process'], 'failed')
        self.assertEqual(result['failure_reason'], 'snapshot_guard_policy_changed')
        self.assertEqual(m.load(self.run/'worker-result.json')['stop_reason'], 'snapshot_guard_policy_changed')

    def test_prepared_legacy_run_keeps_old_live_scan(self):
        snapshot = self.prepare()
        meta = m.load(self.run/'metadata.json'); task = m.load(self.run/'task.json')
        meta.pop('snapshot_guard'); task.pop('snapshot_guard_version'); task.pop('allowed_deletions')
        (snapshot/'context.txt').unlink()
        (snapshot/'code.py').write_text('partial\n')
        self.assertEqual(m.snapshot_changes(self.run, meta, task), ['code.py'])

    def test_concurrent_target_change_rejected(self):
        self.change()
        (self.repo / 'code.py').write_text('new user edit\n')
        with self.assertRaisesRegex(RuntimeError, 'baseline_conflict'):
            m.apply(self.run)
        self.assertEqual((self.repo / 'code.py').read_text(), 'new user edit\n')

    def test_context_change_rejected(self):
        self.change()
        (self.repo / 'context.txt').write_text('changed context\n')
        with self.assertRaisesRegex(RuntimeError, 'baseline_conflict'):
            m.apply(self.run)

    def test_out_of_scope_new_file_rejected(self):
        snapshot = self.prepare()
        (snapshot / 'secret.txt').write_text('unexpected')
        with self.assertRaisesRegex(ValueError, 'scope_violation'):
            m.propose(self.run)

    def test_symlink_candidate_rejected(self):
        snapshot = self.prepare()
        (snapshot / 'code.py').unlink()
        (snapshot / 'code.py').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            m.propose(self.run)

    def test_traversal_rejected(self):
        for path in ('../a', '/tmp/a', 'a/../b', '.git/config', 'a//b', 'a\nb'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                m.checked_path(path)

    def test_patch_tampering_rejected(self):
        self.change()
        with (self.run / 'changes.patch').open('ab') as f:
            f.write(b'tampering\n')
        with self.assertRaisesRegex(ValueError, 'Patch changed'):
            m.apply(self.run)

    def test_repeated_apply_is_idempotent(self):
        self.change()
        m.apply(self.run)
        self.assertTrue(m.apply(self.run)['already_applied'])

    def test_recover_applied_but_lost_local_ack(self):
        self.change()
        m.apply(self.run)
        meta = m.load(self.run / 'metadata.json')
        meta.pop('applied')
        m.dump(self.run / 'metadata.json', meta)
        self.assertTrue(m.apply(self.run)['already_applied'])

    def test_failed_test_not_verified(self):
        self.task['test_commands'] = [['/usr/bin/false']]
        self.change()
        m.apply(self.run)
        tests = m.test_target(self.run)
        self.assertFalse(tests['passed'])
        with self.assertRaisesRegex(RuntimeError, 'Target test exit'):
            self.record_pass()
        self.assertNotEqual(m.status(self.run)['delivery'], 'verified')

    def record_pass(self):
        from process_evidence import binding, async_enabled, live_output_enabled
        from test_reviewer_provenance import synthetic_review
        applied = m.load(self.run / 'applied.json')
        review = self.root / 'review.json'
        review.write_text(json.dumps(dict(synthetic_review(self.run), **{'verdict': 'pass',
                                      'fingerprint': applied['fingerprint'], 'findings': [],
                                      'process_review': {'verdict':'pass','binding':binding(self.run,async_required=async_enabled(m.load(self.run/'task.json'),m.load(self.run/'metadata.json')),live_output_required=live_output_enabled(m.load(self.run/'task.json'),m.load(self.run/'metadata.json'))),
                                                         'async_jobs':[],
                                                         'reason':'Synthetic bridge fixture has zero Bash calls.'}})))
        m.record_review(self.run, review)

    def test_direct_test_failure_keeps_complete_logs_and_real_exit(self):
        self.task['test_commands']=[[sys.executable,'-B','-c',
            'import sys; print("FIRST"); print("line\\n"*200); print("LAST"); sys.exit(7)']]
        self.change();m.apply(self.run)
        result=m.test_target(self.run)
        self.assertFalse(result['passed'])
        self.assertEqual(result['tests'][0]['exit_code'],7)
        data=(self.run/'test-0.stdout').read_bytes()
        self.assertTrue(data.startswith(b'FIRST\n'))
        self.assertTrue(data.endswith(b'LAST\n'))
        self.assertGreater(data.count(b'\n'),200)
        self.assertEqual(result['tests'][0]['stdout_sha256'],m.digest(data))
        with self.assertRaisesRegex(RuntimeError,'Target test exit'):self.record_pass()

    def test_changed_test_log_cannot_retain_verified_delivery(self):
        self.change();m.apply(self.run);m.test_target(self.run);self.record_pass();m.finalize(self.run)
        with (self.run/'test-0.stdout').open('ab') as log:log.write(b'tampered\n')
        with self.assertRaisesRegex(RuntimeError,'log missing or changed'):m.finalize(self.run)
        self.assertNotEqual(m.status(self.run)['delivery'],'verified')

    def test_remote_transport_timeout_retains_received_bytes_without_exit_claim(self):
        self.task['target'].update(transport='ssh',host='offline-fixture',
                                  ssh_keepalive={'server_alive_interval':5,'server_alive_count_max':2})
        def fixture_transport(repo, argv, **kwargs):
            if argv[0]=='timeout':
                raise subprocess.TimeoutExpired(argv,1,output=b'FIRST RECEIVED\n',stderr=b'WARN RECEIVED\n')
            return m.cmd(argv,cwd=repo.repo,**kwargs)
        with mock.patch.object(m.Repo,'run',autospec=True,side_effect=fixture_transport):
            self.change();m.apply(self.run);result=m.test_target(self.run)
        self.assertFalse(result['passed'])
        self.assertIsNone(result['tests'][0]['exit_code'])
        self.assertFalse(result['tests'][0]['transport_complete'])
        self.assertEqual((self.run/'test-0.stdout').read_bytes(),b'FIRST RECEIVED\n')
        self.assertTrue((self.run/'test-0.stderr').read_bytes().startswith(b'WARN RECEIVED\n'))

    def test_legacy_run_keeps_prior_review_schema_without_new_capture(self):
        self.change();m.apply(self.run)
        for name in ('task.json','metadata.json'):
            value=m.load(self.run/name);value.pop('process_evidence_policy');value.pop('reviewer_provenance_policy');value.pop('async_execution_policy');m.dump(self.run/name,value)
        (self.run/'tool-evidence.jsonl').unlink()
        m.test_target(self.run)
        review=self.root/'legacy-review.json'
        m.dump(review,{'model':'gpt-5.6-sol','verdict':'pass',
                       'fingerprint':m.load(self.run/'applied.json')['fingerprint']})
        m.record_review(self.run,review)
        self.assertEqual(m.finalize(self.run)['delivery'],'verified')

    def test_new_prepare_freezes_reviewer_provenance_policy(self):
        from reviewer_provenance import POLICY
        self.prepare(worker_evidence=False)
        self.assertEqual(m.load(self.run/'task.json')['reviewer_provenance_policy'],POLICY)
        self.assertEqual(m.load(self.run/'metadata.json')['reviewer_provenance_policy'],POLICY)

    def test_new_prepare_freezes_async_call_identity_policy(self):
        from async_evidence import POLICY, POLICY_KEY, version_for_run
        self.prepare(worker_evidence=False)
        self.assertEqual(m.load(self.run/'task.json')[POLICY_KEY], POLICY)
        self.assertEqual(m.load(self.run/'metadata.json')[POLICY_KEY], POLICY)
        self.assertEqual(version_for_run(self.run), 2)

    def test_self_label_only_pass_rejected_but_prior_record_is_preserved(self):
        self.change();m.apply(self.run);m.test_target(self.run);self.record_pass()
        prior=(self.run/'review.json').read_bytes()
        review=m.load(self.run/'review.json');review.pop('reviewer_provenance')
        submitted=self.root/'self-label-only.json';m.dump(submitted,review)
        with self.assertRaisesRegex(ValueError,'Require reviewer_provenance'):
            m.record_review(self.run,submitted)
        self.assertEqual((self.run/'review.json').read_bytes(),prior)

    def test_dispatch_evidence_drift_revokes_verified_state_at_finalize(self):
        self.change();m.apply(self.run);m.test_target(self.run);self.record_pass();m.finalize(self.run)
        with (self.run/'fixture-dispatch.json').open('ab') as stream:stream.write(b' ')
        with self.assertRaisesRegex(ValueError,'Dispatch evidence hash changed'):
            m.finalize(self.run)
        self.assertEqual(m.status(self.run)['delivery'],'verification_pending')

    def test_stale_review_cannot_finalize(self):
        self.change()
        m.apply(self.run)
        self.assertTrue(m.test_target(self.run)['passed'])
        self.record_pass()
        (self.repo / 'code.py').write_text('changed after review\n')
        with self.assertRaisesRegex(RuntimeError, 'Verification gates'):
            m.finalize(self.run)

    def test_matching_evidence_can_finalize(self):
        self.change()
        m.apply(self.run)
        m.test_target(self.run)
        self.record_pass()
        self.assertEqual(m.finalize(self.run)['delivery'], 'verified')

    def test_all_delivery_gates_reject_missing_worker(self):
        self.prepare(worker_evidence=False)
        review = self.root / 'review.json'
        review.write_text('{}')
        for action in (m.propose, m.apply, m.test_target, m.finalize, lambda r: m.record_review(r, review)):
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, 'Worker success'):
                action(self.run)

    def test_wrong_model_rejected(self):
        self.prepare()
        m.dump(self.run / 'worker-result.json', {'models': ['other-model'], 'errors': [], 'final_text': 'fixture'})
        with self.assertRaisesRegex(RuntimeError, 'expected model'):
            m.propose(self.run)

    def test_missing_tool_use_rejected(self):
        self.prepare()
        evidence = m.load(self.run / 'worker-result.json')
        evidence['tools'] = []
        m.dump(self.run / 'worker-result.json', evidence)
        with self.assertRaisesRegex(RuntimeError, 'Worker success'):
            m.propose(self.run)

    def test_preflight_failure_does_not_leave_launching(self):
        self.prepare(worker_evidence=False)
        with mock.patch.object(m, 'worker_env', side_effect=FileNotFoundError('test missing credential')):
            with self.assertRaises(FileNotFoundError):
                m.start(self.run)
        self.assertEqual(m.status(self.run)['process'], 'not_started')

    def test_worker_launch_failure_is_terminal(self):
        self.prepare(worker_evidence=False)
        with mock.patch.object(m, 'worker_env', side_effect=FileNotFoundError('test missing credential')):
            with self.assertRaises(FileNotFoundError):
                m.worker(self.run)
        self.assertEqual(m.status(self.run)['process'], 'launch_failed')

    def test_dead_launch_supervisor_is_reported_unknown(self):
        self.prepare(worker_evidence=False)
        meta = m.load(self.run / 'metadata.json')
        meta['process'] = 'launching'
        m.dump(self.run / 'metadata.json', meta)
        self.assertEqual(m.status(self.run)['process'], 'unknown_supervisor_state')

    def test_head_change_rejected_by_test_finalize_and_reapply(self):
        self.change()
        m.apply(self.run)
        m.test_target(self.run)
        self.record_pass()
        m.git(self.repo, '-c', 'user.name=Tests', '-c', 'user.email=tests@localhost',
              'commit', '--allow-empty', '-qm', 'new head same selected files')
        # Retesting retires its old report even on preflight failure; check it last.
        for action in (m.finalize, m.apply, m.test_target):
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, 'identity/HEAD'):
                action(self.run)

    def test_local_deadline_is_enforced(self):
        self.task.update(test_commands=[['/bin/sleep', '2']], test_timeout_seconds=1)
        self.change()
        m.apply(self.run)
        result = m.test_target(self.run)
        self.assertFalse(result['passed'])
        self.assertEqual(result['tests'][0]['exit_code'], 124)

    def test_local_deadline_cleans_child_after_leader_exit(self):
        child = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(30)'
        leader = ('import subprocess,sys;p=subprocess.Popen([sys.executable,"-c",' + repr(child) +
                  '],stdout=subprocess.PIPE,text=True);p.stdout.readline();print(p.pid,flush=True)')
        result = m.local_test([sys.executable, '-c', leader], str(self.repo), 1)
        self.assertEqual(result.returncode, 124)
        child_pid = int(result.stdout.strip())
        r = subprocess.run(['ps', '-p', str(child_pid), '-o', 'stat='], capture_output=True, text=True)
        self.assertTrue(r.returncode != 0 or r.stdout.strip().startswith('Z'))

    def test_option_like_filename_is_supported(self):
        self.task['allowed_paths'].append('-n')
        (self.repo / '-n').write_text('before\n')
        snapshot = self.prepare()
        (snapshot / '-n').write_text('after\n')
        m.propose(self.run)
        m.apply(self.run)
        self.assertEqual((self.repo / '-n').read_text(), 'after\n')

    def test_nested_repository_is_rejected(self):
        nested = self.repo / 'sub'
        nested.mkdir()
        m.git(nested, 'init', '-q')
        (nested / 'inside.txt').write_text('nested content\n')
        self.task['allowed_paths'] = ['sub/inside.txt']
        with self.assertRaisesRegex(ValueError, 'nested Git'):
            self.prepare()

    def test_cancel_kills_term_ignoring_descendant_after_leader_reaped(self):
        child = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(60)'
        leader = ('import subprocess,sys,time;p=subprocess.Popen([sys.executable,"-c",' + repr(child) +
                  '],stdout=subprocess.PIPE,text=True);p.stdout.readline();print(p.pid,flush=True);time.sleep(60)')
        p = subprocess.Popen([sys.executable, '-c', leader], stdout=subprocess.PIPE,
                             text=True, start_new_session=True)
        child_pid = int(p.stdout.readline())
        stamp = m.process_stamp(p.pid)
        reaper = threading.Thread(target=p.wait)
        reaper.start()
        try:
            m.stop_group(p.pid, stamp)
            time.sleep(0.1)
            r = subprocess.run(['ps', '-p', str(child_pid), '-o', 'stat='], capture_output=True, text=True)
            self.assertTrue(r.returncode != 0 or r.stdout.strip().startswith('Z'))
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            reaper.join(timeout=3)
            p.stdout.close()


    def failed_rpc(self):
        self.prepare(worker_evidence=False)
        session = self.run / 'saved-session.jsonl'
        session.write_text('{}\n')
        meta = m.load(self.run / 'metadata.json')
        meta.update(process='stalled', worker_mode='rpc', session_file=str(session))
        m.dump(self.run / 'metadata.json', meta)
        self.recovery = self.root / 'recovery.txt'
        self.recovery.write_text('Complete only the original bounded change')

    def test_resume_preserves_snapshot_archives_and_caps_recovery(self):
        self.failed_rpc()
        (self.run / 'snapshot/code.py').write_text('partial candidate\n')
        (self.run / 'events.jsonl').write_text('{"type":"agent_start"}\n')
        m.dump(self.run / 'failure.json', {'reason':'listener_error'})
        m.dump(self.run / 'termination.json', {'reason':'listener_error'})
        m.dump(self.run / 'startup.json', {'version':1,'milestones':{'rpc_ready':.1}})
        with mock.patch.object(m, 'start', return_value={'started': True}):
            m.resume(self.run, self.recovery)
        self.assertEqual((self.run / 'snapshot/code.py').read_text(), 'partial candidate\n')
        self.assertTrue((self.run / 'attempt-1/events.jsonl').exists())
        self.assertFalse((self.run / 'events.jsonl').exists())
        self.assertTrue((self.run / 'attempt-1/failure.json').exists())
        self.assertFalse((self.run / 'failure.json').exists())
        self.assertEqual(m.load(self.run/'attempt-1/startup.json')['milestones']['rpc_ready'], .1)
        self.assertFalse((self.run/'startup.json').exists())
        meta = m.load(self.run / 'metadata.json')
        self.assertEqual(meta['attempt'], 2)
        meta['process'] = 'stalled'; m.dump(self.run / 'metadata.json', meta)
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            m.resume(self.run, self.recovery)

    def test_v3_cancelled_dependency_probe_cannot_resume_from_raw_request_only(self):
        self.failed_rpc()
        meta=m.load(self.run/'metadata.json');meta['process']='cancelled';m.dump(self.run/'metadata.json',meta)
        m.dump(self.run/'failure.json',{'reason':'cancel_requested'})
        (self.run/'events.jsonl').write_text('')
        raw={'version':1,'type':'tool_execution_start','id':'probe','args':{
            'command':'python3 -B -c "import missing_dependency" 2>&1 | tail -1'}}
        (self.run/'tool-evidence.jsonl').write_text(json.dumps(raw)+'\n')
        with mock.patch.object(m,'start') as start:
            with self.assertRaisesRegex(RuntimeError,'Preserve prohibited-request'):
                m.resume(self.run,self.recovery)
            start.assert_not_called()
        self.assertEqual(m.load(self.run/'failure.json')['reason'],'cancel_requested')
        self.assertFalse((self.run/'attempt-1').exists())

    def test_resume_rejects_changed_target(self):
        self.failed_rpc()
        (self.repo / 'code.py').write_text('new user change\n')
        with self.assertRaisesRegex(RuntimeError, 'Target changed'):
            m.resume(self.run, self.recovery)
        self.assertEqual((self.repo / 'code.py').read_text(), 'new user change\n')

    def test_resume_rejects_damaged_snapshot_without_archiving_evidence(self):
        self.failed_rpc()
        (self.run/'snapshot/context.txt').unlink()
        before = (self.run/'metadata.json').read_bytes()
        with mock.patch.object(m, 'start') as start:
            with self.assertRaisesRegex(ValueError, 'snapshot_scope_violation'):
                m.resume(self.run, self.recovery)
            start.assert_not_called()
        self.assertEqual((self.run/'metadata.json').read_bytes(), before)
        self.assertFalse((self.run/'attempt-1').exists())

    def test_resume_missing_guard_metadata_preserves_failed_evidence(self):
        self.failed_rpc()
        meta = m.load(self.run/'metadata.json'); meta.pop('snapshot_guard')
        m.dump(self.run/'metadata.json', meta)
        before = (self.run/'metadata.json').read_bytes()
        with mock.patch.object(m, 'start') as start:
            with self.assertRaisesRegex(ValueError, 'snapshot_guard_policy_changed'):
                m.resume(self.run, self.recovery)
            start.assert_not_called()
        self.assertEqual((self.run/'metadata.json').read_bytes(), before)
        self.assertFalse((self.run/'attempt-1').exists())

    def test_rpc_requires_terminal_event_evidence(self):
        self.prepare()
        meta = m.load(self.run / 'metadata.json'); meta['worker_mode'] = 'rpc'
        m.dump(self.run / 'metadata.json', meta)
        with self.assertRaisesRegex(RuntimeError, 'Terminal RPC'):
            m.propose(self.run)

    def test_thinking_is_validated_at_prepare(self):
        self.task['worker_thinking'] = 'off'
        with self.assertRaisesRegex(ValueError, 'worker_thinking'):
            self.prepare(worker_evidence=False)


    def test_resume_copy_failure_leaves_original_evidence_usable(self):
        self.failed_rpc()
        before = (self.run / 'metadata.json').read_bytes()
        with mock.patch.object(m.shutil, 'copy2', side_effect=OSError('injected copy failure')):
            with self.assertRaisesRegex(OSError, 'injected copy'):
                m.resume(self.run, self.recovery)
        self.assertEqual((self.run / 'metadata.json').read_bytes(), before)
        self.assertFalse((self.run / 'attempt-1').exists())
        self.assertEqual(m.status(self.run)['process'], 'stalled')

    def test_resume_metadata_failure_rolls_back_prompt_and_logs(self):
        self.failed_rpc()
        (self.run / 'events.jsonl').write_text('{"type":"agent_start"}\n')
        (self.run / 'supervisor.log').write_text('old supervisor')
        m.dump(self.run/'startup.json', {'version':1,'milestones':{'rpc_ready':.1}})
        before = {name:(self.run/name).read_bytes() for name in
                  ('metadata.json','prompt.md','events.jsonl','supervisor.log','startup.json')}
        with mock.patch.object(m, 'dump', side_effect=OSError('injected metadata failure')):
            with self.assertRaisesRegex(OSError, 'injected metadata'):
                m.resume(self.run, self.recovery)
        for name,data in before.items(): self.assertEqual((self.run/name).read_bytes(), data)
        self.assertFalse((self.run / 'attempt-1').exists())
        with mock.patch.object(m, 'start', return_value={'started':True}):
            self.assertTrue(m.resume(self.run, self.recovery)['started'])
        self.assertEqual((self.run/'attempt-1/supervisor.log').read_text(), 'old supervisor')


    def test_retracted_review_invalidates_previous_verification(self):
        self.change(); m.apply(self.run); m.test_target(self.run); self.record_pass(); m.finalize(self.run)
        review = m.load(self.run / 'review.json'); review['verdict'] = 'changes_requested'
        path = self.root / 'retraction.json'; m.dump(path, review); m.record_review(self.run, path)
        self.assertEqual(m.status(self.run)['delivery'], 'changes_requested')
        with self.assertRaisesRegex(RuntimeError, 'Verification gates'):
            m.finalize(self.run)
        self.assertEqual(m.status(self.run)['delivery'], 'changes_requested')
        self.assertEqual(m.load(self.run/'review.json')['verdict'], 'changes_requested')


    def test_failed_retest_invalidates_verified_state(self):
        self.change(); m.apply(self.run); m.test_target(self.run); self.record_pass(); m.finalize(self.run)
        failed = subprocess.CompletedProcess(['/usr/bin/true'], 1, b'', b'new failure')
        with mock.patch.object(m, 'local_test', return_value=failed):
            self.assertFalse(m.test_target(self.run)['passed'])
        self.assertEqual(m.status(self.run)['delivery'], 'tests_failed')
        with self.assertRaisesRegex(RuntimeError, 'Target test exit'): m.finalize(self.run)
        self.assertEqual(m.status(self.run)['delivery'], 'tests_failed')

    def test_interrupted_retest_cannot_reuse_old_passing_report(self):
        self.change(); m.apply(self.run); m.test_target(self.run); self.record_pass(); m.finalize(self.run)
        with mock.patch.object(m, 'local_test', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'): m.test_target(self.run)
        self.assertNotEqual(m.status(self.run)['delivery'], 'verified')
        with self.assertRaises(FileNotFoundError): m.finalize(self.run)


    def test_passing_retest_requires_a_fresh_review_and_archives_logs(self):
        self.change(); m.apply(self.run); m.test_target(self.run); self.record_pass(); m.finalize(self.run)
        old = (self.run / 'test-0.stdout').read_bytes()
        m.test_target(self.run)
        archives = list(self.run.glob('tests-before-*'))
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / 'test-0.stdout').read_bytes(), old)
        with self.assertRaisesRegex(RuntimeError, 'Verification gates'): m.finalize(self.run)
        self.record_pass(); self.assertEqual(m.finalize(self.run)['delivery'], 'verified')


class TransportPolicyTests(unittest.TestCase):
    def target(self, **extra):
        return dict(transport='ssh', host='test-alias', repo="/repo with space/it's quoted", **extra)

    def test_remote_command_preserves_fixed_options_quoting_and_deadline(self):
        for settings, interval, count in (({}, 15, 2), ({'server_alive_interval': 5}, 5, 2),
                                          ({'server_alive_interval': 300, 'server_alive_count_max': 10}, 300, 10)):
            with self.subTest(settings=settings), mock.patch.object(m, 'cmd') as execute:
                repo = m.Repo(self.target(ssh_keepalive=settings))
                argv = ['python3', 'some file.py', 'literal;$(do not run)']
                repo.run(argv, data=b'input', timeout=77, check=False)
                sent = execute.call_args.args[0]
                self.assertEqual(sent, ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                       '-o', 'ClearAllForwardings=yes', '-o', 'ServerAliveInterval=%d' % interval,
                                       '-o', 'ServerAliveCountMax=%d' % count, 'test-alias',
                                       'cd -- ' + shlex.quote(repo.repo) + ' && exec ' + shlex.join(argv)])
                self.assertEqual(execute.call_args.kwargs, {'data': b'input', 'timeout': 77, 'check': False})

    def test_malformed_unbounded_and_injected_settings_rejected(self):
        invalid = [None, [], '5', {'ServerAliveInterval': 5}, {'-oProxyCommand': 'bad'}]
        for key, maximum in (('server_alive_interval', 300), ('server_alive_count_max', 10)):
            invalid.extend({key: value} for value in (True, False, 0, -1, maximum + 1, 5.0, '5', None))
        for value in invalid:
            with self.subTest(value=value), mock.patch.object(m, 'cmd') as execute:
                with self.assertRaises(ValueError): m.Repo(self.target(ssh_keepalive=value))
                execute.assert_not_called()

    def test_local_transport_rejects_ssh_settings_and_keeps_native_command(self):
        local = {'transport': 'local', 'repo': '/local/repo'}
        with self.assertRaises(ValueError): m.Repo(dict(local, ssh_keepalive={}))
        with mock.patch.object(m, 'cmd') as execute:
            m.Repo(local).run(['git', 'status'])
        self.assertEqual(execute.call_args.args[0], ['git', 'status'])
        self.assertEqual(execute.call_args.kwargs['cwd'], '/local/repo')

    def test_legacy_defaults_and_no_hot_retrofit(self):
        task = {'target': self.target()}
        with mock.patch.object(m, 'cmd') as execute:
            m.target_repo(task, {}).run(['true'])
        self.assertIn('ServerAliveInterval=15', execute.call_args.args[0])
        self.assertIn('ServerAliveCountMax=2', execute.call_args.args[0])
        task['target']['ssh_keepalive'] = {}
        with self.assertRaisesRegex(ValueError, 'Legacy runs keep historical'):
            m.target_repo(task, {})

    def test_frozen_policy_rejects_missing_mismatched_or_changed_values(self):
        target = self.target(ssh_keepalive={'server_alive_interval': 5, 'server_alive_count_max': 2})
        policy = m.transport_policy(target)
        task = {'target': target, 'transport_policy': copy.deepcopy(policy)}
        meta = {'transport_policy': copy.deepcopy(policy)}
        self.assertEqual(m.validate_transport_policy(task, meta), policy)
        mutations = [({'target': target}, meta), (task, {}), (dict(task, transport_policy=None), meta),
                     (task, {'transport_policy': dict(policy, version=2)})]
        changed = copy.deepcopy(task); changed['target']['ssh_keepalive']['server_alive_interval'] = 6
        mutations.append((changed, meta))
        removed = copy.deepcopy(task); removed['target'].pop('ssh_keepalive')
        mutations.append((removed, meta))
        for changed_task, changed_meta in mutations:
            with self.subTest(task=changed_task, meta=changed_meta):
                with self.assertRaisesRegex(ValueError, 'Transport policy changed'):
                    m.target_repo(changed_task, changed_meta)


if __name__ == '__main__':
    unittest.main(verbosity=2)
