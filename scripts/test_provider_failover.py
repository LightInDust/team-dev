import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import team_dev as b
from provider_failover import recoverable_fault
from worker_model import DEFAULT_MODEL, SECONDARY_MODEL


class FailoverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        config = self.root/'config'
        config.mkdir()
        for key, value in [('CONFIG', config), ('RUNS', self.root/'runs')]:
            patch = mock.patch.object(b, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        b.dump(config/'runtime.json', {'opencode_network_billing_fallback': True})
        self.repo = self.root/'repo'
        self.repo.mkdir()
        (self.repo/'main.py').write_text('x = 1\n')
        b.git(self.repo, 'init', '-q')
        b.git(self.repo, 'add', '.')
        b.git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-qm', 'base')
        self.req = {'goal':'fixture', 'target':{'transport':'local','repo':str(self.repo)},
                    'worker_model':DEFAULT_MODEL, 'allowed_paths':['main.py'],
                    'test_commands':[['/usr/bin/true']], 'worker_bash_mode':'declared_tests_only'}
        self.meta = b.prepare_task(self.req)
        self.run = Path(self.meta['run'])
        self.task = b.load(self.run/'task.json')
        self.meta.update(process='failed', failure_reason='worker_evidence_invalid', delivery='worker_failed')
        self.result = {'errors':['402 Payment Required'], 'client_errors':[]}
        self.message = {'type':'message_end', 'message':{'role':'assistant','model':DEFAULT_MODEL.split('/')[1],
                        'stopReason':'error','errorMessage':self.result['errors'][0]}}
        self.events(self.message)
        (self.run/'tool-evidence.jsonl').write_bytes(b'')

    def events(self, *events):
        (self.run/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))

    def handoff(self):
        b.launch_provider_fallback(self.run,self.meta,self.task,self.result)

    def test_recaptures_same_input_and_launches_secondary_once(self):
        with mock.patch.object(b,'start') as start:
            self.handoff()
            self.handoff()
        receipt=b.load(self.run/'provider-fallback.json')
        child=Path(receipt['run'])
        start.assert_called_once_with(child)
        self.assertEqual(receipt['state'],'started')
        self.assertEqual(receipt['reason'],'billing')
        _, meta, task=b.run_state(child)
        self.assertEqual((meta['attempt'],meta['recovery_from'],meta['worker_model']),(2,str(self.run),SECONDARY_MODEL))
        self.assertNotIn('provider_fallback_policy',meta)
        self.assertEqual(task['test_commands'],self.req['test_commands'])
        self.assertEqual(task['worker_bash_mode'],'declared_tests_only')
        with self.assertRaises(RuntimeError): b.validate_recovery_launch(child,meta,supervisor=False)

    def test_supervisor_dispatches_child_after_saved_network_error(self):
        run = Path(b.prepare_task(dict(self.req, worker_bash_mode='standard'))['run'])
        message = dict(self.message['message'], errorMessage='connect ECONNRESET')
        (run/'events.jsonl').write_text(json.dumps({'type':'message_end', 'message':message}) + '\n')
        (run/'tool-evidence.jsonl').write_bytes(b'')
        b.dump(run/'progress.json', {'terminal_event':True})
        b.dump(run/'termination.json', {'reason':'terminal_agent_end', 'details':[]})
        with mock.patch.object(b, 'worker_env', return_value={}), mock.patch.object(
                b.shutil, 'which', return_value=sys.executable), mock.patch(
                'rpc_worker.execute', return_value=('exited',0)), mock.patch.object(b,'start') as start:
            meta = b.worker(run)
        self.assertEqual(meta['process'], 'failed')
        receipt = b.load(run/'provider-fallback.json')
        self.assertEqual((receipt['reason'],receipt['state']), ('network','started'))
        start.assert_called_once_with(Path(receipt['run']))
        self.assertEqual(b.load(run/'worker-result.json')['errors'], ['connect ECONNRESET'])

    def test_direct_child_worker_retry_rejected_before_credentials(self):
        with mock.patch.object(b, 'start'):
            self.handoff()
        child = Path(b.load(self.run/'provider-fallback.json')['run'])
        meta = b.load(child/'metadata.json')
        meta['process'] = 'launch_failed'
        b.dump(child/'metadata.json', meta)
        with mock.patch.object(b, 'worker_env') as credentials, mock.patch('rpc_worker.execute') as rpc:
            with self.assertRaisesRegex(RuntimeError, 'Fallback launch'):
                b.worker(child)
        credentials.assert_not_called()
        rpc.assert_not_called()

    def test_target_drift_and_launch_errors_preserve_one_way_receipt(self):
        (self.repo/'main.py').write_text('x = 2\n')
        with mock.patch.object(b,'start') as start:
            self.handoff()
            self.handoff()
        start.assert_not_called()
        receipt=b.load(self.run/'provider-fallback.json')
        self.assertEqual(receipt['state'],'blocked')
        child=Path(receipt['run'])
        with self.assertRaises(RuntimeError): b.start(child)

    def test_launch_failure_is_not_replayed(self):
        with mock.patch.object(b,'start',side_effect=RuntimeError('private fixture')) as start:
            self.handoff()
            self.handoff()
        self.assertEqual(start.call_count,1)
        receipt=b.load(self.run/'provider-fallback.json')
        self.assertEqual(receipt['state'],'blocked')
        self.assertNotIn('private fixture',json.dumps(receipt))

    def test_suppress_after_tool_work_partial_candidate_or_later_success(self):
        for name in ('bash','edit','write'):
            self.events({'type':'tool_execution_start','toolName':name},self.message)
            self.assertIsNone(recoverable_fault(self.run,self.meta,self.result))
        self.events(self.message,{'type':'message_end','message':{'role':'assistant','stopReason':'stop'}})
        self.assertIsNone(recoverable_fault(self.run,self.meta,self.result))
        self.events(self.message)
        (self.run/'snapshot/main.py').write_text('x = 2\n')
        with mock.patch.object(b,'start') as start:self.handoff()
        start.assert_not_called()

    def test_parent_cancel_propagates_and_child_refuses_to_prompt(self):
        with mock.patch.object(b, 'start'):
            self.handoff()
        receipt = b.load(self.run/'provider-fallback.json')
        child = Path(receipt['run'])
        _, meta, _ = b.run_state(child)
        meta['process'] = 'launching'
        b.dump(child/'metadata.json', meta)
        b.dump(self.run/'metadata.json', self.meta)
        b.cancel(self.run)
        self.assertTrue((child/'cancel-requested').exists())
        with self.assertRaises(RuntimeError):
            b.validate_recovery_launch(child, meta, supervisor=True)

    def test_non_provider_failures_and_recovery_attempt_do_not_dispatch(self):
        for patch in ({'process':'exited'}, {'failure_reason':'cancel_requested'},
                      {'failure_reason':'preexec_gate_invalid'}, {'attempt':2}):
            with self.subTest(patch=patch):
                self.assertIsNone(recoverable_fault(self.run,dict(self.meta, **patch),self.result))
        (self.run/'tool-evidence.jsonl').write_text('{"type":"async_result"}\n')
        self.assertIsNone(recoverable_fault(self.run,self.meta,self.result))

    def test_invalid_opt_out_rejected_before_capture_or_run_allocation(self):
        with mock.patch.object(b, 'RUNS', self.root/'unused'), mock.patch.object(b.Repo, 'capture') as capture:
            with self.assertRaisesRegex(ValueError, 'must be boolean'):
                b.prepare_task(dict(self.req, disable_provider_fallback='false'))
            capture.assert_not_called()
            self.assertFalse(b.RUNS.exists())

    def test_policy_pinning_cancel_and_old_runs(self):
        self.assertNotIn('provider_fallback_policy', b.prepare_task(dict(self.req,disable_provider_fallback=True)))
        self.assertNotIn('provider_fallback_policy', b.prepare_task(dict(self.req,worker_model=SECONDARY_MODEL)))
        b.dump(b.CONFIG/'runtime.json',{})
        legacy=b.prepare_task(self.req)
        self.assertNotIn('provider_fallback_policy',legacy)
        with mock.patch.object(b,'start') as start:
            b.launch_provider_fallback(self.run,legacy,self.task,self.result)
            (self.run/'cancel-requested').touch()
            self.handoff()
        start.assert_not_called()
