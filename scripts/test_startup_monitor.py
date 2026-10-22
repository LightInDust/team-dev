import json
from pathlib import Path
import tempfile
import unittest

from startup_monitor import StartupMonitor, read_startup, first_output_wait


class StartupTests(unittest.TestCase):
    def wait_fixture(self, elapsed=269.6):
        monitor = StartupMonitor(0)
        monitor.mark('prompt_ack_received', .35)
        progress = {'elapsed_seconds': elapsed, 'changed_paths': [], 'tool_counts': {},
                    'active_tools': [], 'first_write_at': None}
        policy = {'policy_version': 2, 'progress': {'warn_seconds': 240, 'stall_seconds': 600, 'first_write_seconds': 900},
                  'timeout_seconds': 1800}
        return monitor, progress, policy

    def test_no_output_wait_uses_existing_attempt_bound_not_time_since_ack(self):
        monitor, progress, policy = self.wait_fixture()
        value = first_output_wait(monitor.snapshot(269.6), progress, policy, 'running')
        self.assertEqual(value['state'], 'waiting')
        self.assertEqual(value['observation'], 'prompt_accepted_no_assistant_event')
        self.assertAlmostEqual(value['wait_since_ack_seconds'], 269.25)
        self.assertEqual(value['stop_at_elapsed_seconds'], 600)
        self.assertAlmostEqual(value['remaining_seconds'], 330.4)
        self.assertEqual(value['next_step'], 'inspect_or_cancel_without_automatic_retry')
        self.assertFalse(value['automatic_retry_authorized'])
        self.assertIsNone(value['provider_http_request_observed'])
        ended = first_output_wait(monitor.snapshot(269.6), progress, policy, 'cancelled')
        self.assertEqual(ended['state'], 'ended_without_output')
        self.assertIsNone(ended['remaining_seconds'])
        self.assertEqual(ended['next_step'], 'verify_exit_then_inspect_or_take_over')

    def test_wait_guidance_follows_earliest_frozen_deadline(self):
        monitor, progress, policy = self.wait_fixture(20)
        self.assertEqual(first_output_wait(monitor.snapshot(20), progress, policy, 'running')['next_step'],
                         'wait_within_existing_budget')
        for field, expected in [('timeout_seconds', 40), ('first_write_seconds', 30)]:
            if field == 'timeout_seconds': policy[field] = expected
            else: policy['progress'][field] = expected
            value = first_output_wait(monitor.snapshot(20), progress, policy, 'running')
            self.assertEqual(value['stop_at_elapsed_seconds'], expected)
            self.assertEqual(value['remaining_seconds'], expected-20)
        progress['elapsed_seconds'] = 31
        value = first_output_wait(monitor.snapshot(31), progress, policy, 'running')
        self.assertEqual(value['remaining_seconds'], 0)
        self.assertEqual(value['next_step'], 'expect_stop_then_verify_exit')

    def test_assistant_header_is_not_output_but_positive_delta_and_tool_are(self):
        monitor, progress, policy = self.wait_fixture()
        monitor.event({'type': 'message_start', 'message_role': 'assistant'}, 5)
        value = first_output_wait(monitor.snapshot(269.6), progress, policy, 'running')
        self.assertEqual(value['state'], 'waiting')
        self.assertEqual(value['observation'], 'assistant_event_without_model_delta_or_tool')
        for event in ({'type': 'message_update', 'stream_kind': 'thinking', 'delta_chars': 1},
                      {'type': 'tool_execution_start', 'toolName': 'read'}):
            monitor, progress, policy = self.wait_fixture()
            monitor.event(event, 5)
            value = first_output_wait(monitor.snapshot(269.6), progress, policy, 'running')
            self.assertEqual(value['state'], 'output_observed')
            self.assertIsNone(value['remaining_seconds'])

    def test_incomplete_or_invalid_saved_startup_never_invents_waiting(self):
        monitor, progress, policy = self.wait_fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for key, bad in [('first_model_delta', 'PRIVATE'), ('prompt_ack_received', float('nan')),
                             ('first_tool_event', True), ('first_assistant_event', 10**400)]:
                saved = monitor.snapshot(269.6); saved['milestones'][key] = bad
                (root/'startup.json').write_text(json.dumps(saved))
                value = first_output_wait(read_startup(root), progress, policy, 'running')
                self.assertEqual(value['state'], 'unknown')
                self.assertNotIn('PRIVATE', json.dumps(value))
            saved = monitor.snapshot(269.6); del saved['milestones']['first_model_delta']
            (root/'startup.json').write_text(json.dumps(saved))
            self.assertEqual(first_output_wait(read_startup(root), progress, policy, 'running')['state'], 'unknown')
            (root/'startup.json').unlink()
            self.assertEqual(first_output_wait(read_startup(root), progress, policy, 'running')['state'], 'unknown')

    def test_saved_candidate_or_missing_policy_does_not_invent_remaining_budget(self):
        monitor, progress, policy = self.wait_fixture()
        for incomplete in (None, {}, {'progress': {'stall_seconds': 600}, 'timeout_seconds': 1800}):
            value = first_output_wait(monitor.snapshot(269.6), progress, incomplete, 'running')
            self.assertEqual(value['state'], 'waiting')
            self.assertIsNone(value['stop_at_elapsed_seconds'])
        progress['changed_paths'] = ['candidate.py']
        self.assertIsNone(first_output_wait(monitor.snapshot(269.6), progress, policy, 'running')['remaining_seconds'])
        progress['changed_paths'] = []
        for missing in ('first_write_at', 'active_tools', 'tool_counts', 'changed_paths'):
            partial = dict(progress); del partial[missing]
            self.assertIsNone(first_output_wait(monitor.snapshot(269.6), partial, policy, 'running')['remaining_seconds'])

    def test_incomplete_or_invalid_warning_policy_cannot_recommend_waiting(self):
        monitor, progress, policy = self.wait_fixture()
        for value in (None, 'PRIVATE', 0, -1, True, float('nan'), float('inf'), 10**400, 600, 700):
            with self.subTest(value_type=type(value).__name__):
                policy['progress']['warn_seconds'] = value
                if value is None:
                    del policy['progress']['warn_seconds']
                result = first_output_wait(monitor.snapshot(269.6), progress, policy, 'running')
                self.assertEqual(result['state'], 'waiting')
                self.assertEqual(result['next_step'], 'inspect_existing_budget')
                self.assertIsNone(result['stop_at_elapsed_seconds'])
                self.assertIsNone(result['remaining_seconds'])
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_saved_policy_requires_supported_version_and_valid_integer_total(self):
        monitor, progress, policy = self.wait_fixture()
        for field, values in [('policy_version', (None, True, 0, 3, 2.0, 'PRIVATE')),
                              ('timeout_seconds', (None, True, 9, 3601, 1800.0, 'PRIVATE'))]:
            for value in values:
                bad = dict(policy); bad[field] = value
                if value is None:
                    del bad[field]
                result = first_output_wait(monitor.snapshot(269.6), progress, bad, 'running')
                self.assertIsNone(result['stop_at_elapsed_seconds'])
                self.assertIsNone(result['budget_basis'])
        for version in (1, 2):
            policy['policy_version'] = version
            self.assertEqual(first_output_wait(monitor.snapshot(269.6), progress, policy, 'running')
                             ['budget_basis'], 'saved_execution_policy')
        del policy['policy_version']
        live = first_output_wait(monitor.snapshot(269.6), progress, policy, 'running', from_runtime=True)
        self.assertEqual(live['stop_at_elapsed_seconds'], 600)
        self.assertEqual(live['budget_basis'], 'live_watchdog_limits')

    def test_valid_long_custom_progress_is_bounded_by_total_not_telemetry_cap(self):
        monitor, progress, policy = self.wait_fixture()
        policy['progress'] = {'warn_seconds': 90000, 'stall_seconds': 100000, 'first_write_seconds': 120000}
        result = first_output_wait(monitor.snapshot(269.6), progress, policy, 'running')
        self.assertEqual(result['stop_at_elapsed_seconds'], 1800)
        self.assertAlmostEqual(result['remaining_seconds'], 1530.4)

    def test_assistant_start_and_first_delta_are_distinct(self):
        monitor = StartupMonitor(10)
        monitor.mark('prompt_ack_received', 11)
        monitor.event({'type':'message_start','message_role':'assistant'}, 15)
        before = monitor.snapshot(16)
        self.assertEqual(before['stage'], 'assistant_event_observed')
        self.assertIsNone(before['milestones']['first_model_delta'])
        monitor.event({'type':'message_update','stream_kind':'thinking','delta_chars':3}, 18)
        monitor.event({'type':'message_update','stream_kind':'text','delta_chars':8}, 20)
        after = monitor.snapshot(21)
        self.assertEqual(after['stage'], 'model_output_observed')
        self.assertEqual(after['milestones']['first_model_delta'], 8)
        self.assertIsNone(after['provider_http_request_observed'])

    def test_old_ready_and_user_echo_do_not_invent_prompt_ack_or_network_cause(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/'rpc-state.json').write_text(json.dumps({'model':'deepseek-v4-flash','provider':'opencode-ds','private':'PRIVATE'}))
            (root/'events.jsonl').write_text(json.dumps({'type':'message_end','elapsed_seconds':.28,
                'message':{'role':'user','content':'PRIVATE'}})+'\n')
            before = {p.name:p.read_bytes() for p in root.iterdir()}
            value = read_startup(root)
            self.assertEqual(value['observation'], 'rpc_ready_without_assistant_event')
            self.assertIsNone(value['prompt_ack_observed'])
            self.assertIsNone(value['milestones']['first_assistant_event'])
            self.assertNotIn('PRIVATE', json.dumps(value))
            self.assertEqual(before, {p.name:p.read_bytes() for p in root.iterdir()})

    def test_missing_or_partial_events_never_prove_absent_output(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'rpc-state.json').write_text(json.dumps({'model':'deepseek-v4-flash','provider':'opencode-ds'}))
            self.assertEqual(read_startup(root)['observation'], 'partial_legacy_observation')
            (root/'events.jsonl').write_text('{PRIVATE')
            self.assertEqual(read_startup(root)['events_scan'], 'truncated')
            self.assertEqual(read_startup(root)['observation'], 'partial_legacy_observation')

    def test_recorded_fields_are_allowlisted_and_bad_telemetry_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'startup.json').write_text(json.dumps({'version':1, 'stage':'PRIVATE',
                'milestones':{'prompt_ack_received':1, 'first_model_delta':'PRIVATE', 'extra':'PRIVATE'}}))
            value = read_startup(root)
            self.assertEqual(value['stage'], 'awaiting_model_output')
            self.assertNotIn('PRIVATE', json.dumps(value))
            (root/'startup.json').write_text('{PRIVATE')
            self.assertEqual(read_startup(root)['source'], 'unreadable')


if __name__ == '__main__':
    unittest.main()
