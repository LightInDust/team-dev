"""Behavioral tests: no providers, SSH, experiments or wall-clock waiting."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from closeout_monitor import CloseoutMonitor
from execution_policy import resolve_policy
import rpc_worker as rpc
import team_dev as bridge
from run_diagnostics import diagnose_run
from test_rpc_worker import Clock, FakeClient


class PolicyTests(unittest.TestCase):
    def test_profiles_and_custom_are_independent(self):
        for effort, pair in [('low', (180, 300)), ('high', (360, 600)), ('max', (540, 900))]:
            actual = resolve_policy({'worker_thinking': effort})['closeout']
            self.assertEqual((actual['warn_seconds'], actual['deadline_seconds']), pair)
        task = {'worker_thinking': 'high', 'closeout': {'deadline_seconds': 1000}}
        before = copy.deepcopy(task)
        resolved = resolve_policy(task)
        self.assertEqual(resolved['budget_mode'], 'custom')
        self.assertEqual(resolved['closeout'], {'warn_seconds': 360, 'deadline_seconds': 1000})
        resolved['closeout']['deadline_seconds'] = 1
        self.assertEqual(task, before)

    def test_auto_replaces_stale_closeout_and_rejects_unknown_keys(self):
        result = resolve_policy({'worker_thinking': 'max', 'budget_mode': 'auto',
                                 'closeout': {'deadline_seconds': None}})
        self.assertEqual(result['closeout']['deadline_seconds'], 900)
        for mode in ('auto', 'custom'):
            with self.assertRaises(ValueError):
                resolve_policy({'budget_mode': mode, 'closeout': {'disable': True}})

    def test_bad_values_rejected(self):
        for value in (None, False, [], {'warn_seconds': True}, {'deadline_seconds': 0},
                      {'deadline_seconds': float('nan')}, {'warn_seconds': float('inf')},
                      {'deadline_seconds': 3601}, {'warn_seconds': 300}, {'deadline_seconds': 100}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_policy({'budget_mode': 'custom', 'closeout': value})

    def test_v1_frozen_policy_unchanged_and_v2_tampering_rejected(self):
        for version in (1, 2):
            policy = resolve_policy({'worker_thinking': 'high'}, version=version)
            task = {k: copy.deepcopy(v) for k, v in policy.items() if k != 'policy_version'}
            task['execution_policy'] = copy.deepcopy(policy)
            before = copy.deepcopy(task)
            bridge.validate_worker_options(task, expected_policy=policy)
            self.assertEqual(task, before)
            self.assertEqual('closeout' in task, version == 2)
            task['closeout'] = {'warn_seconds': 1, 'deadline_seconds': 5}
            with self.assertRaises(ValueError):
                bridge.validate_worker_options(task, expected_policy=policy)

    def test_explicit_missing_copy_fails_but_standalone_and_legacy_are_valid(self):
        policy = resolve_policy({})
        task = {k:copy.deepcopy(v) for k,v in policy.items() if k != 'policy_version'}
        task['execution_policy'] = policy
        bridge.validate_worker_options(task)
        with self.assertRaisesRegex(ValueError, 'Execution policy changed'):
            bridge.validate_worker_options(task, expected_policy=None)
        legacy = {}
        bridge.validate_worker_options(legacy, expected_policy=None)
        self.assertEqual(legacy, {'worker_thinking': 'low'})
        with self.assertRaisesRegex(ValueError, 'missing for closeout'):
            bridge.validate_worker_options({'closeout': policy['closeout']}, expected_policy=None)


class MonitorTests(unittest.TestCase):
    def test_candidate_clock_never_resets_even_when_diff_reverted(self):
        monitor = CloseoutMonitor(10, {'warn_seconds': 3, 'deadline_seconds': 5})
        monitor.observe([], 100)
        self.assertIsNone(monitor.check(100))
        monitor.observe(['code.py'], 101)
        monitor.observe([], 103)
        monitor.observe(['code.py'], 105)
        self.assertEqual(monitor.check(105), 'warn')
        self.assertIsNone(monitor.check(105.5))
        self.assertEqual(monitor.check(106), 'deadline')
        self.assertEqual(monitor.snapshot(106)['first_candidate_at'], 91)

    def test_busy_reminder_deferred_without_deadline_extension(self):
        monitor = CloseoutMonitor(0, {'warn_seconds': 3, 'deadline_seconds': 5})
        monitor.observe(['x'], 0)
        self.assertIsNone(monitor.check(4, can_warn=False))
        self.assertFalse(monitor.warned)
        self.assertEqual(monitor.check(5, can_warn=False), 'deadline')

    def test_legacy_disabled(self):
        monitor = CloseoutMonitor(0)
        monitor.observe(['x'], 0)
        self.assertIsNone(monitor.check(99999))
        self.assertFalse(monitor.snapshot(99999)['enabled'])


class RpcCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name); (self.run/'prompt.md').write_text('implement')
        self.clock, self.client = Clock(), FakeClient()
        self.changed = ['code.py']
        self.task = {'worker_thinking': 'low', 'budget_mode': 'custom', 'timeout_seconds': 10,
                     'progress': {'warn_seconds': 20, 'stall_seconds': 30, 'first_write_seconds': 40},
                     'closeout': {'warn_seconds': .4, 'deadline_seconds': .8}}
        bridge.validate_worker_options(self.task, resolve=True)

    def execute(self):
        with mock.patch.object(rpc, 'time', self.clock):
            return rpc.execute(self.run, {}, self.task, {}, ['fake'], bridge.dump, lambda pid: 'stamp',
                               lambda: self.changed, client_factory=lambda **kw: self.client)

    def failure(self):
        return json.loads((self.run/'failure.json').read_text())

    def test_repeated_edits_and_reasoning_hit_closeout_with_one_reminder(self):
        def tick():
            self.client.emit(NS(type='tool_execution_start', tool_name='edit', tool_call_id='a'))
            self.client.emit(NS(type='tool_execution_end', tool_name='edit', tool_call_id='a', is_error=False))
            self.client.emit(NS(type='message_update'))
        self.clock.tick = tick
        self.assertEqual(self.execute(), ('timed_out', 1))
        self.assertEqual(self.failure()['reason'], 'candidate_closeout_deadline')
        self.assertLess(self.clock.now, 1.1)
        self.assertEqual(len(self.client.steers), 1)
        warning = json.loads((self.run/'closeout-warning.json').read_text())
        self.assertTrue(warning['rpc_ack'])
        self.assertTrue(self.client.aborted)
        progress = json.loads((self.run/'progress.json').read_text())
        self.assertEqual(progress['closeout']['first_candidate_at'], 0)
        self.assertEqual(progress['idle_tool_seconds'], 0)

    def test_closeout_warning_states_remaining_budget_and_stays_queued_only(self):
        self.task['closeout'] = {'warn_seconds': 1.0, 'deadline_seconds': 3.0}
        bridge.validate_worker_options(self.task, resolve=True)
        self.clock.tick = lambda: self.client.emit(NS(type='message_update'))
        self.assertEqual(self.execute(), ('timed_out', 1))
        self.assertEqual(self.failure()['reason'], 'candidate_closeout_deadline')
        self.assertEqual(len(self.client.steers), 1)
        # One second of the frozen three-second window is consumed at the single warning.
        self.assertIn('2 seconds', self.client.steers[0])
        warning = json.loads((self.run/'closeout-warning.json').read_text())
        self.assertTrue(warning['rpc_ack'])
        self.assertEqual(warning['action'], 'steer')
        self.assertEqual(warning['closeout']['candidate_age_seconds'], 1.0)
        self.assertEqual(warning['remaining_seconds'], 2)
        self.assertIn('Queued only', warning['note'])
        self.assertFalse((self.run/'worker-result.json').exists())
        self.assertFalse(json.loads((self.run/'progress.json').read_text())['terminal_event'])

    def test_partial_final_text_neither_succeeds_nor_resets_the_window(self):
        self.clock.tick = lambda: self.client.emit(
            NS(type='message_update', assistant_message_event={'type': 'text_delta', 'delta': 'Status DONE'}))
        self.assertEqual(self.execute(), ('timed_out', 1))
        self.assertEqual(self.failure()['reason'], 'candidate_closeout_deadline')
        self.assertFalse((self.run/'worker-result.json').exists())
        progress = json.loads((self.run/'progress.json').read_text())
        self.assertFalse(progress['terminal_event'])
        self.assertEqual(progress['closeout']['first_candidate_at'], 0)
        # The assistant was still streaming final text when the window expired.
        self.assertEqual(progress['phase'], 'text')

    def test_active_tool_no_automatic_steer_but_still_bounded(self):
        self.client.initial = [NS(type='tool_execution_start', tool_name='bash', tool_call_id='a')]
        self.assertEqual(self.execute(), ('timed_out', 1))
        self.assertEqual(self.failure()['reason'], 'candidate_closeout_deadline')
        self.assertEqual(self.client.steers, [])

    def test_pending_async_result_does_not_escape_window(self):
        from process_evidence import POLICY
        self.task['process_evidence_policy'] = dict(POLICY)
        self.client.initial = [
            NS(type='tool_execution_start', tool_name='bash', tool_call_id='a', args={'command': 'echo fixture'}),
            NS(type='tool_execution_end', tool_name='bash', tool_call_id='a', is_error=False,
               result={'content': [{'type': 'text', 'text': 'running'}],
                       'details': {'async': {'jobId': 'bg_1', 'state': 'running', 'type': 'bash'}}}),
            NS(type='agent_end', is_terminal=True)]
        with mock.patch.object(rpc, 'time', self.clock):
            result = rpc.execute(self.run, {'process_evidence_policy': dict(POLICY)}, self.task, {}, ['fake'],
                                 bridge.dump, lambda pid: 'stamp', lambda: self.changed,
                                 client_factory=lambda **kw: self.client)
        self.assertEqual(result, ('timed_out', 1))
        self.assertEqual(self.failure()['reason'], 'candidate_closeout_deadline')
        self.assertEqual(self.client.steers, [])
        self.assertEqual(json.loads((self.run/'progress.json').read_text())['async_execution']['pending'], ['bg_1'])

    def test_real_diff_required_not_write_ack(self):
        self.changed = []
        self.client.initial = [NS(type='tool_execution_end', tool_name='write', tool_call_id='a', is_error=False)]
        self.assertEqual(self.execute(), ('timed_out', 1))
        self.assertEqual(self.failure()['reason'], 'total_deadline')
        self.assertIsNone(json.loads((self.run/'progress.json').read_text())['closeout']['first_candidate_at'])

    def test_completion_before_deadline_keeps_existing_gate(self):
        self.clock.tick = lambda: self.client.emit(NS(type='agent_end', is_terminal=True))
        self.assertEqual(self.execute(), ('exited', 0))
        self.assertFalse(self.client.aborted)
        self.assertFalse((self.run/'failure.json').exists())

    def test_cancel_reason_preserved(self):
        self.clock.tick = lambda: (self.run/'cancel-requested').write_text('cancel') if self.clock.now >= .8 else None
        self.assertEqual(self.execute(), ('cancelled', 1))
        self.assertEqual(self.failure()['reason'], 'cancel_requested')

    def test_existing_v1_ignores_new_window(self):
        self.task.pop('closeout')
        self.task['execution_policy'] = resolve_policy(self.task, version=1)
        self.assertEqual(self.execute(), ('timed_out', 1))
        self.assertEqual(self.failure()['reason'], 'total_deadline')
        self.assertNotIn('closeout', json.loads((self.run/'progress.json').read_text()))

    def test_unversioned_null_policy_keeps_legacy_worker_behavior(self):
        self.task.pop('closeout')
        self.task['execution_policy'] = None
        bridge.validate_worker_options(self.task, expected_policy=None)
        self.assertEqual(self.execute(), ('timed_out', 1))
        self.assertEqual(self.failure()['reason'], 'total_deadline')
        self.assertNotIn('closeout', json.loads((self.run/'progress.json').read_text()))

    def test_recorded_closeout_cannot_be_resumed_and_source_unchanged(self):
        self.assertEqual(self.execute(), ('timed_out', 1))
        bridge.dump(self.run/'metadata.json', {'process': 'timed_out'})
        bridge.dump(self.run/'task.json', self.task)
        before = {p.name:p.read_bytes() for p in self.run.iterdir() if p.is_file()}
        with mock.patch.object(bridge, 'worker_env') as env, self.assertRaisesRegex(RuntimeError, 'closeout failure'):
            bridge.resume(self.run, self.run/'missing-message')
        env.assert_not_called()
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.run.iterdir()
                                  if p.is_file() and p.name in before})

    def test_diagnosis_keeps_transport_errors_separate(self):
        self.execute()
        result = diagnose_run(self.run)
        self.assertEqual(result['assessment'], 'candidate_handoff_budget_exhausted')
        bridge.dump(self.run/'worker-result.json', {'errors': ['socket connection was closed unexpectedly']})
        result = diagnose_run(self.run)
        self.assertEqual(result['stop_reason'], 'candidate_closeout_deadline')
        self.assertEqual(result['assessment'], 'model_stream_faults_observed')


if __name__ == '__main__':
    unittest.main()
