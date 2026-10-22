import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from run_diagnostics import diagnose_run, error_kind, http_error_status


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write('metadata.json', {'process': 'failed', 'delivery': 'worker_failed'})
        self.write('progress.json', {'changed_paths': [], 'first_write_at': None,
            'stream_chars': {'unknown': 0}, 'tool_counts': {'read': 4}})
        self.write('termination.json', {'reason': 'terminal_agent_end', 'terminal_event': True})
        self.write('failure.json', {'reason': 'worker_evidence_invalid'})
        self.result = {'models':['deepseek-v4-flash'], 'tools':['read'], 'final_text':'',
            'terminal_event':True, 'client_errors':[], 'errors':[],
            'event_types':{'auto_retry_start':1,'auto_retry_end':1}}

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data))

    def diagnose(self):
        self.write('worker-result.json', self.result)
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        value = diagnose_run(self.root)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.root.iterdir()})
        return value

    def test_terminal_with_socket_error_keeps_both_layers(self):
        self.result['errors'] = ['The socket connection was closed unexpectedly. PRIVATE',
            'OpenAI completions stream timed out while waiting for the first event']
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'model_stream_faults_observed')
        self.assertEqual(result['stop_reason'], 'worker_evidence_invalid')
        self.assertEqual(result['rpc_termination_reason'], 'terminal_agent_end')
        self.assertTrue(result['terminal_agent_end'])
        self.assertFalse(result['candidate_observed'])
        self.assertEqual(result['model_error_kinds'], {'socket_closed':1,'first_event_timeout':1})
        self.assertEqual(result['worker_evidence_blockers'], ['model_errors_present','final_text_missing'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_idle_timeout_does_not_hide_stream_errors(self):
        self.write('failure.json', {'reason':'tool_idle'})
        self.result['terminal_event'] = False
        self.result['errors'] = ['OpenAI completions stream closed before a finish_reason was received',
            'OpenAI completions stream stalled while waiting for the next event']
        result = self.diagnose()
        self.assertEqual(result['stop_reason'], 'tool_idle')
        self.assertEqual(result['model_error_count'], 2)
        self.assertEqual(result['assessment'], 'model_stream_faults_observed')

    def test_process_policy_stop_reasons_are_preserved_without_inventing_cause(self):
        for reason in ('async_results_pending', 'async_evidence_invalid', 'process_constraint_request'):
            with self.subTest(reason=reason):
                self.write('failure.json', {'reason': reason})
                self.write('termination.json', {'reason': reason, 'terminal_event': False})
                self.result['terminal_event'] = False
                result = self.diagnose()
                self.assertEqual(result['stop_reason'], reason)
                self.assertEqual(result['rpc_termination_reason'], reason)
                self.assertEqual(result['assessment'], 'insufficient_evidence_for_cause')

    def test_async_stop_preserves_separate_stream_and_rpc_faults(self):
        self.write('failure.json', {'reason': 'async_results_pending'})
        self.write('termination.json', {'reason': 'async_results_pending'})
        self.result['errors'] = ['The socket connection was closed unexpectedly. PRIVATE']
        self.result['client_errors'] = [{'reason': 'listener_error', 'frames': ['PRIVATE']}]
        result = self.diagnose()
        self.assertEqual(result['stop_reason'], 'async_results_pending')
        self.assertEqual(result['rpc_termination_reason'], 'async_results_pending')
        self.assertEqual(result['assessment'], 'model_stream_and_rpc_faults_observed')
        self.assertEqual(result['model_error_kinds'], {'socket_closed': 1})
        self.assertEqual(result['rpc_client_fault_count'], 1)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_no_errors_is_not_proof_of_network_health_or_reasoning(self):
        self.write('failure.json', {'reason':'first_candidate_deadline'})
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'no_candidate_before_deadline')
        self.assertEqual(result['stream_measurement'], 'unclassified')
        self.assertEqual(result['model_error_count'], 0)

    def test_cancelled_before_first_output_keeps_stop_cause_and_no_retry_guidance(self):
        from startup_monitor import StartupMonitor
        monitor = StartupMonitor(0); monitor.mark('prompt_ack_received', .35)
        self.write('startup.json', monitor.snapshot(269.6))
        self.write('metadata.json', {'process': 'cancelled', 'worker_bash_gate': {'version': 2},
            'execution_policy': {'policy_version': 2, 'progress': {'warn_seconds': 240, 'stall_seconds': 600,
                'first_write_seconds': 900}, 'timeout_seconds': 1800}})
        self.write('progress.json', {'elapsed_seconds': 269.6, 'changed_paths': [],
                                   'first_write_at': None, 'tool_counts': {}, 'active_tools': []})
        self.write('failure.json', {'reason': 'cancel_requested'})
        self.result.update(terminal_event=False, models=[], tools=[])
        value = self.diagnose()
        self.assertEqual(value['stop_reason'], 'cancel_requested')
        self.assertEqual(value['assessment'], 'insufficient_evidence_for_cause')
        self.assertEqual(value['first_output_wait']['state'], 'ended_without_output')
        self.assertEqual(value['first_output_wait']['observation'], 'prompt_accepted_no_assistant_event')
        self.assertEqual(value['first_output_wait']['stop_at_elapsed_seconds'], 600)
        self.assertIsNone(value['first_output_wait']['remaining_seconds'])
        self.assertEqual(value['recovery']['same_run_resume'], 'prohibited_by_gate_policy')
        self.result['errors'] = ['404 Not Found PRIVATE']
        self.assertEqual(self.diagnose()['assessment'], 'model_http_errors_observed')

    def test_unclassified_errors_are_not_discarded_or_disclosed(self):
        self.result['errors'] = ['PRIVATE_OPAQUE_ERROR']
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'unclassified_model_errors_observed')
        self.assertEqual(result['model_error_kinds'], {'unclassified':1})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_endpoint_unavailable_before_any_output_is_http_not_slow_reasoning(self):
        self.result['errors'] = [
            '404 Upstream request failed: Endpoint is unavailable.\n'
            'Upstream request failed: Endpoint is unavailable. (type=server_error) PRIVATE'
        ] * 2
        self.result['tools'] = []
        self.write('progress.json', {'changed_paths': [], 'stream_chars': {}, 'tool_counts': {}})
        self.write('termination.json', {'reason': 'terminal_agent_end', 'terminal_event': True,
                                       'elapsed_seconds': 6.296295})
        result = self.diagnose()
        self.assertEqual(result['diagnostic_version'], 2)
        self.assertEqual(result['assessment'], 'model_http_errors_observed')
        self.assertEqual(result['model_error_kinds'], {'http_endpoint_unavailable': 2})
        self.assertEqual(result['model_http_status_counts'], {'404': 2})
        self.assertEqual(result['model_http_error_count'], 2)
        self.assertEqual(result['model_stream_fault_count'], 0)
        self.assertEqual(result['rpc_client_fault_count'], 0)
        self.assertEqual(result['stop_reason'], 'worker_evidence_invalid')
        self.assertEqual(result['rpc_termination_reason'], 'terminal_agent_end')
        self.assertEqual(result['elapsed_seconds'], 6.296295)
        self.assertEqual(result['output_observation'], 'no_changes_or_final_text')
        self.assertEqual(result['worker_evidence_blockers'],
                         ['model_errors_present', 'final_text_missing', 'tool_use_missing'])
        self.assertEqual(result['auto_retry_events'], {'auto_retry_start': 1, 'auto_retry_end': 1})
        self.assertEqual(result['request_failure_guidance'], {
            'next_step': 'inspect_endpoint_model_access_before_recovery',
            'root_cause': 'unknown', 'automatic_retry_authorized': False})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_http_categories_keep_numeric_status_without_raw_body(self):
        for raw, status, kind in [
                ('HTTP 404 Not Found PRIVATE', 404, 'http_not_found'),
                ('Error code: 401 PRIVATE', 401, 'http_auth_or_access_rejected'),
                ('403 PRIVATE', 403, 'http_auth_or_access_rejected'),
                ('HTTP/2 429 PRIVATE', 429, 'http_rate_limited'),
                ('HTTP/1.1 408 PRIVATE', 408, 'http_request_timeout'),
                ('400: PRIVATE', 400, 'http_client_error'),
                ('422 PRIVATE', 422, 'http_client_error'),
                ('500 PRIVATE', 500, 'http_server_error'),
                ('503 PRIVATE socket connection was closed unexpectedly', 503, 'http_server_error')]:
            with self.subTest(status=status):
                self.assertEqual(http_error_status(raw), status)
                self.result['errors'] = [raw]
                result = self.diagnose()
                self.assertEqual(result['model_error_kinds'], {kind: 1})
                self.assertEqual(result['model_http_status_counts'], {str(status): 1})
                self.assertEqual(result['assessment'], 'model_http_errors_observed')
                self.assertEqual(result['model_stream_fault_count'], 0)
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_402_distinguishes_explicit_funds_report_from_generic_payment_required(self):
        cases = [
            ('402 Upstream request failed: Insufficient account funds (type=server_error) PRIVATE',
             'http_insufficient_account_funds', 'insufficient_account_funds_reported'),
            ('HTTP/1.1 402 INSUFFICIENT\nACCOUNT\tFUNDS PRIVATE',
             'http_insufficient_account_funds', 'insufficient_account_funds_reported'),
            ('Error code: 402 Payment required PRIVATE',
             'http_payment_required', 'payment_required_reported'),
            ('402 PRIVATE socket connection was closed unexpectedly',
             'http_payment_required', 'payment_required_reported'),
            ('402 Insufficient account funding PRIVATE',
             'http_payment_required', 'payment_required_reported'),
        ]
        for raw, kind, reported in cases:
            with self.subTest(kind=kind, reported=reported):
                self.result['errors'] = [raw]
                result = self.diagnose()
                self.assertEqual(result['model_error_kinds'], {kind: 1})
                self.assertEqual(result['model_http_status_counts'], {'402': 1})
                self.assertEqual(result['model_stream_fault_count'], 0)
                self.assertEqual(result['provider_access']['billing_observation'], reported)
                self.assertFalse(result['provider_access']['account_balance_verified'])
                self.assertEqual(result['request_failure_guidance'], {
                    'next_step': 'defer_provider_retry_until_billing_resolution',
                    'root_cause': 'unknown', 'automatic_retry_authorized': False,
                    'reported_condition': reported,
                    'same_endpoint_retry': 'defer_pending_billing_resolution_evidence',
                    'configuration_change_authorized': False})
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_billing_words_without_leading_402_do_not_invent_a_funds_diagnosis(self):
        for raw, kind in [
                ('Insufficient account funds PRIVATE', 'unclassified'),
                ('PRIVATE status=402 Insufficient account funds', 'unclassified'),
                ('4020 Insufficient account funds PRIVATE', 'unclassified'),
                ('402suffix Insufficient account funds PRIVATE', 'unclassified'),
                ('403 Insufficient account funds PRIVATE', 'http_auth_or_access_rejected'),
                ('500 Insufficient account funds PRIVATE', 'http_server_error'),
                ({'status': 402, 'message': 'Insufficient account funds PRIVATE'}, 'unclassified')]:
            with self.subTest(kind=kind):
                self.result['errors'] = [raw]
                result = self.diagnose()
                self.assertEqual(result['model_error_kinds'], {kind: 1})
                self.assertEqual(result['provider_access']['billing_observation'], 'not_observed')
                self.assertNotIn('reported_condition', result['request_failure_guidance'] or {})
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_402_after_candidate_and_terminal_keeps_failed_untested_evidence(self):
        self.write('metadata.json', {'process': 'failed', 'delivery': 'worker_failed',
            'exit_code': 0, 'worker_bash_gate': {'version': 2}, 'attempt': 2})
        self.write('progress.json', {'changed_paths': ['PRIVATE-a', 'PRIVATE-b'],
                                    'tool_counts': {'write': 2, 'bash': 0}})
        self.result.update(errors=['402 Insufficient account funds PRIVATE'] * 4,
                           tools=['write', 'write'], final_text='PRIVATE now running tests')
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'model_http_errors_observed')
        self.assertEqual(result['model_error_kinds'], {'http_insufficient_account_funds': 4})
        self.assertEqual(result['provider_access']['generation_observation'], 'http_errors_recorded')
        self.assertEqual(result['provider_access']['current_service_health'], 'unknown')
        self.assertTrue(result['terminal_agent_end'])
        self.assertEqual(result['changed_paths_count'], 2)
        self.assertEqual(result['tool_counts']['bash'], 0)
        self.assertEqual(result['worker_evidence_blockers'], ['model_errors_present'])
        self.assertEqual(result['recovery']['next_step'], 'inspect_preserved_candidate_for_independent_takeover')
        self.assertEqual(result['recovery']['saved_attempt'], 2)
        self.assertEqual(result['recovery']['same_run_resume'], 'prohibited_by_gate_policy')
        self.assertEqual(json.loads((self.root/'metadata.json').read_text())['delivery'], 'worker_failed')
        self.assertFalse((self.root/'tests.json').exists())
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_402_guidance_keeps_mixed_transport_faults_and_active_state_separate(self):
        self.write('metadata.json', {'process': 'running'})
        self.result['errors'] = ['402 Insufficient account funds PRIVATE',
                                 'The socket connection was closed unexpectedly PRIVATE',
                                 '503 PRIVATE', 'PRIVATE_UNKNOWN']
        self.result['client_errors'] = [{'reason': 'listener_error'}]
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'model_http_and_transport_faults_observed')
        self.assertEqual(result['model_http_status_counts'], {'402': 1, '503': 1})
        self.assertEqual(result['model_stream_fault_count'], 1)
        self.assertEqual(result['rpc_client_fault_count'], 1)
        self.assertEqual(result['model_error_kinds']['unclassified'], 1)
        self.assertEqual(result['recovery']['next_step'], 'inspect_run_state')
        self.assertFalse(result['request_failure_guidance']['automatic_retry_authorized'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_existing_worker_gate_rejects_billing_error_despite_exit_zero_and_report(self):
        from team_dev import require_worker
        model = 'opencode-ds/deepseek-v4.1-flash'
        meta = {'worker_model': model, 'process': 'exited', 'exit_code': 0, 'worker_mode': 'rpc'}
        self.write('task.json', {'worker_model': model})
        self.result.update(models=['deepseek-v4.1-flash'], tools=['write'],
                           final_text='PRIVATE candidate saved; tests next', errors=[])
        self.write('worker-result.json', self.result)
        require_worker(self.root, meta)  # Positive control for this gate, not test/acceptance proof.
        self.result['errors'] = ['402 Insufficient account funds PRIVATE']
        self.write('worker-result.json', self.result)
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaisesRegex(RuntimeError, 'Worker success'):
            require_worker(self.root, meta)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.root.iterdir()})
        self.assertFalse((self.root/'tests.json').exists())

    def test_embedded_status_and_endpoint_words_do_not_invent_http_evidence(self):
        for raw in ['PRIVATE url/path/404: Endpoint is unavailable',
                    'PRIVATE http_status=404', '4040 PRIVATE', '404suffix PRIVATE',
                    '200 Endpoint is unavailable', '600 Endpoint is unavailable',
                    'Endpoint is unavailable', 'server_error PRIVATE',
                    None, True, 404, {'status': 404}, ['404 PRIVATE']]:
            with self.subTest(value_type=type(raw).__name__):
                self.assertIsNone(http_error_status(raw))
                self.assertEqual(error_kind(raw), 'unclassified')
                self.result['errors'] = [raw]
                result = self.diagnose()
                self.assertEqual(result['model_http_error_count'], 0)
                self.assertIsNone(result['request_failure_guidance'])
                self.assertEqual(result['assessment'], 'unclassified_model_errors_observed')
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_mixed_http_stream_and_rpc_observations_are_all_preserved(self):
        for with_stream, with_rpc in [(True, False), (False, True), (True, True)]:
            with self.subTest(stream=with_stream, rpc=with_rpc):
                self.result['errors'] = ['404 Endpoint is unavailable PRIVATE', 'PRIVATE_OTHER']
                if with_stream:
                    self.result['errors'].append('The socket connection was closed unexpectedly PRIVATE')
                self.result['client_errors'] = [{'reason': 'listener_error'}] if with_rpc else []
                result = self.diagnose()
                self.assertEqual(result['assessment'], 'model_http_and_transport_faults_observed')
                self.assertEqual(result['model_http_error_count'], 1)
                self.assertEqual(result['model_http_status_counts'], {'404': 1})
                self.assertEqual(result['model_stream_fault_count'], int(with_stream))
                self.assertEqual(result['rpc_client_fault_count'], int(with_rpc))
                self.assertEqual(result['model_error_kinds']['unclassified'], 1)
                self.assertEqual(result['model_error_count'], 2 + int(with_stream))
                self.assertEqual(result['stop_reason'], 'worker_evidence_invalid')
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_http_diagnosis_does_not_reset_recovery_or_skip_existing_candidate(self):
        self.result['errors'] = ['404 Endpoint is unavailable']
        for meta, paths, next_step in [
                ({'process': 'failed', 'worker_bash_gate': {'version': 1}}, [],
                 'inspect_evidence_then_fresh_preparation'),
                ({'process': 'failed', 'attempt': 2, 'worker_bash_gate': {'version': 1}}, [],
                 'reassess_scope_or_independent_takeover'),
                ({'process': 'failed', 'worker_bash_gate': {'version': 1}}, ['PRIVATE'],
                 'inspect_preserved_candidate_for_independent_takeover'),
                ({'process': 'running', 'worker_bash_gate': {'version': 1}}, [], 'inspect_run_state')]:
            with self.subTest(next_step=next_step):
                self.write('metadata.json', meta)
                self.write('progress.json', {'changed_paths': paths})
                result = self.diagnose()
                self.assertEqual(result['recovery']['next_step'], next_step)
                self.assertEqual(result['recovery']['same_run_resume'], 'prohibited_by_gate_policy')
                self.assertFalse(result['recovery']['eligibility_checked'])
                self.assertFalse(result['request_failure_guidance']['automatic_retry_authorized'])
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_callback_fault_separate_from_snapshot_violation(self):
        self.result['client_errors'] = [{'reason':'listener_error','exception_type':'TypeError','frames':['PRIVATE']}]
        result = self.diagnose()
        self.assertEqual(result['rpc_client_fault_count'], 1)
        self.assertEqual(result['assessment'], 'rpc_client_faults_observed')
        self.result['client_errors'] = [{'reason':'snapshot_scope_violation','paths':['PRIVATE']}]
        self.write('failure.json', {'reason':'snapshot_unavailable'})
        result = self.diagnose()
        self.assertEqual(result['rpc_client_fault_count'], 0)
        self.assertEqual(result['stop_reason'], 'snapshot_unavailable')

    def test_partial_and_malformed_records_are_explicit(self):
        (self.root/'worker-result.json').write_text('{PRIVATE')
        (self.root/'termination.json').unlink()
        result = diagnose_run(self.root)
        self.assertIn('worker-result.json', result['unreadable_files'])
        self.assertIn('termination.json', result['missing_files'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_cli_needs_no_task_config_credentials_or_home(self):
        self.write('worker-result.json', self.result)
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('team_dev.py')),
                                 'diagnose','--run',str(self.root)], capture_output=True, text=True,
                                env=dict(os.environ, HOME=str(self.root),PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['stop_reason'], 'worker_evidence_invalid')
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.root.iterdir()})

    def test_terminal_empty_completion_rejected_without_invented_transport_fault(self):
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'completion_evidence_rejected')
        self.assertEqual(result['worker_evidence_blockers'], ['final_text_missing'])

    def test_whitespace_and_nonstring_final_text_remain_invalid(self):
        for value, state in [('\n\n', 'blank'), (' \t\r\n', 'blank'),
                             (None, 'invalid_type'), ({'text': 'PRIVATE'}, 'invalid_type'),
                             (['PRIVATE'], 'invalid_type'), (True, 'invalid_type')]:
            with self.subTest(state=state, value_type=type(value).__name__):
                self.result['final_text'] = value
                result = self.diagnose()
                self.assertEqual(result['final_text_state'], state)
                self.assertIn('final_text_missing', result['worker_evidence_blockers'])
                self.assertNotIn('PRIVATE', json.dumps(result))
        self.result.pop('final_text')
        self.assertEqual(self.diagnose()['final_text_state'], 'missing')
        self.result['final_text'] = ' \nPRIVATE\n '
        result = self.diagnose()
        self.assertEqual(result['final_text_state'], 'present')
        self.assertNotIn('final_text_missing', result['worker_evidence_blockers'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_blank_terminal_stream_fault_with_strict_gate_needs_fresh_preparation(self):
        self.write('metadata.json', {'process': 'failed', 'worker_bash_gate': {'version': 1}})
        self.result['final_text'] = '\n\n'
        self.result['errors'] = ['OpenAI completions stream stalled while waiting for the next event',
                                 'The socket connection was closed unexpectedly. PRIVATE']
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'model_stream_faults_observed')
        self.assertEqual(result['worker_evidence_blockers'], ['model_errors_present', 'final_text_missing'])
        self.assertEqual(result['recovery'], {'same_run_resume': 'prohibited_by_gate_policy',
            'next_step': 'inspect_evidence_then_fresh_preparation', 'saved_attempt': None,
            'eligibility_checked': False})
        self.assertEqual(result['auto_retry_events'], {'auto_retry_start': 1, 'auto_retry_end': 1})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_recovery_hints_do_not_authorize_resume_or_retry_active_runs(self):
        for meta, route, action, attempt in [
                ({'process': 'failed', 'process_evidence_policy': {'version': 4}},
                 'prohibited_by_gate_policy', 'inspect_evidence_then_fresh_preparation', None),
                ({'process': 'failed', 'worker_bash_gate': None},
                 'prohibited_by_gate_policy', 'inspect_evidence_then_fresh_preparation', None),
                ({'process': 'failed', 'attempt': 1}, 'not_evaluated', 'inspect_resume_requirements', 1),
                ({'process': 'failed', 'attempt': 2}, 'not_evaluated', 'reassess_scope_or_independent_takeover', 2),
                ({'process': 'failed', 'attempt': True}, 'not_evaluated', 'inspect_resume_requirements', None),
                ({'process': 'running', 'worker_bash_gate': {'version': 1}},
                 'prohibited_by_gate_policy', 'inspect_run_state', None),
                ({}, 'not_evaluated', 'inspect_run_state', None)]:
            with self.subTest(meta=meta):
                self.write('metadata.json', meta)
                recovery = self.diagnose()['recovery']
                self.assertEqual(recovery, {'same_run_resume': route, 'next_step': action,
                                           'saved_attempt': attempt, 'eligibility_checked': False})

    def test_recorded_candidate_and_report_after_retry_do_not_erase_stream_failure(self):
        self.write('metadata.json', {'process': 'failed', 'worker_bash_gate': {'version': 1}, 'attempt': 2})
        self.write('progress.json', {'changed_paths': ['PRIVATE-a', 'PRIVATE-b', 'PRIVATE-c'],
                                     'first_write_at': 10})
        self.result['final_text'] = 'PRIVATE report; target dependency NOT VERIFIED'
        self.result['errors'] = ['The socket connection was closed unexpectedly. PRIVATE']
        result = self.diagnose()
        self.assertEqual(result['output_observation'], 'recorded_changes_and_final_text')
        self.assertEqual(result['assessment'], 'model_stream_faults_observed')
        self.assertEqual(result['stop_reason'], 'worker_evidence_invalid')
        self.assertEqual(result['worker_evidence_blockers'], ['model_errors_present'])
        self.assertEqual(result['changed_paths_count'], 3)
        self.assertEqual(result['auto_retry_events'], {'auto_retry_start': 1, 'auto_retry_end': 1})
        self.assertEqual(result['recovery'], {'same_run_resume': 'prohibited_by_gate_policy',
            'next_step': 'inspect_preserved_candidate_for_independent_takeover',
            'saved_attempt': 2, 'eligibility_checked': False})
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('NOT VERIFIED', json.dumps(result))

    def test_saved_output_states_preserve_absent_unknown_and_reverted_diff_distinctions(self):
        for paths, text, expected in [
                (['PRIVATE'], '', 'recorded_changes_only'),
                (['PRIVATE'], ' \n', 'recorded_changes_only'),
                ([], 'PRIVATE', 'final_text_only'),
                ([], '', 'no_changes_or_final_text'),
                (None, 'PRIVATE', 'changes_unknown_with_final_text'),
                (None, '', 'changes_unknown_without_final_text'),
                ([None], 'PRIVATE', 'changes_unknown_with_final_text'),
                ([' '], '', 'changes_unknown_without_final_text')]:
            with self.subTest(expected=expected, paths=paths):
                self.write('progress.json', {'changed_paths': paths, 'first_write_at': 5})
                self.result['final_text'] = text
                result = self.diagnose()
                self.assertEqual(result['output_observation'], expected)
                if paths == []:
                    self.assertTrue(result['candidate_observed'])  # Historical progress only.
                    self.assertEqual(result['changed_paths_count'], 0)
                if expected.startswith('changes_unknown'):
                    self.assertIsNone(result['changed_paths_count'])
                    self.assertEqual(result['recovery']['next_step'], 'inspect_resume_requirements')
                self.assertNotIn('PRIVATE', json.dumps(result))
        self.write('metadata.json', {'process': 'running'})
        self.write('progress.json', {'changed_paths': ['PRIVATE']})
        self.assertEqual(self.diagnose()['recovery']['next_step'], 'inspect_run_state')


if __name__ == '__main__':
    unittest.main()
