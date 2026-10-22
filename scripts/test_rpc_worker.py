import json
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

import rpc_worker as r
import team_dev as bridge


class Clock:
    def __init__(self): self.now = 0; self.tick = lambda: None
    def monotonic(self): return self.now
    def time(self): return self.now + 1000
    def sleep(self, seconds): self.now += seconds; self.tick()


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs; self.protocol_errors = []; self.listener_errors = []
        self.stderr = ''; self.stopped = False; self.aborted = False; self.steers = []
        self._process = NS(pid=os.getpid(), poll=lambda: None)
        self.initial = []; self.state = NS(model=NS(id='deepseek-v4-flash', provider='opencode-ds'),
                                           thinking_level='low', session_file='/fake/session')
    def on_event(self, callback): self.emit = callback
    def on_protocol_error(self, callback): self.protocol_callback = callback
    def on_listener_error(self, callback): self.listener_callback = callback
    def start(self): pass
    def get_state(self): return self.state
    def prompt(self, message):
        for event in self.initial: self.emit(event)
    def steer(self, message): self.steers.append(message)
    def abort(self): self.aborted = True
    def stop(self): self.stopped = True


class RpcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name); (self.run/'prompt.md').write_text('implement')
        self.clock = Clock(); self.client = FakeClient()
        self.task = {'worker_thinking': 'low', 'timeout_seconds': 10,
                     'progress': {'warn_seconds': .3, 'stall_seconds': .7, 'first_write_seconds': 2}}
        self.changed = []

    def execute(self, meta=None, candidate_paths=None, preflight=None):
        with mock.patch.object(r, 'time', self.clock):
            return r.execute(self.run, meta or {}, self.task, {'ONLY': 'worker'}, ['fake'], bridge.dump,
                             lambda pid: 'stamp', candidate_paths or (lambda: self.changed),
                             client_factory=lambda **kwargs: self.client, preflight=preflight)

    def test_rpc_captures_safe_local_route_before_prompt_without_health_claim(self):
        from omp_rpc.protocol import ModelInfo, ModelCost
        self.client.state.model = ModelInfo(id='deepseek-v4-flash', name='PRIVATE',
            api='openai-completions', provider='opencode-ds',
            base_url='https://opencode.ai/inference/openai/v1', reasoning=True,
            input_modalities=('text',), cost=ModelCost(0, 0, 0, 0),
            context_window=100, max_tokens=10, headers={'Authorization': 'PRIVATE'})
        self.client.initial = [NS(type='agent_end', is_terminal=True)]
        original_prompt = self.client.prompt
        inspected = []
        def prompt(message):
            saved = json.loads((self.run/'rpc-state.json').read_text())
            route = saved['request_configuration']
            self.assertEqual(route, {'version': 1, 'basis': 'local_omp_model_configuration',
                'api_mode': 'openai-completions', 'endpoint_family': 'opencode_inference_openai_v1'})
            self.assertNotIn('PRIVATE', json.dumps(saved))
            self.assertNotIn('https:', json.dumps(saved))
            inspected.append(True)
            original_prompt(message)
        self.client.prompt = prompt
        self.assertEqual(self.execute(), ('exited', 0))
        self.assertEqual(inspected, [True])

    def test_rpc_route_capture_tolerates_missing_optional_model_fields(self):
        self.client.initial = [NS(type='agent_end', is_terminal=True)]
        self.assertEqual(self.execute(), ('exited', 0))
        route = json.loads((self.run/'rpc-state.json').read_text())['request_configuration']
        self.assertEqual(route['endpoint_family'], 'other_or_unknown')
        self.assertEqual(route['api_mode'], 'other_or_unknown')

    def test_snapshot_guard_stops_after_tool_deletes_context(self):
        from snapshot_guard import SnapshotGuard
        snapshot = self.run/'snapshot'; snapshot.mkdir()
        (snapshot/'code.py').write_text('code\n')
        (snapshot/'context.py').write_text('context\n')
        bridge.git(snapshot, 'init', '-q'); bridge.git(snapshot, 'add', '-A')
        bridge.git(snapshot, '-c', 'user.name=Tests', '-c', 'user.email=tests@localhost', 'commit', '-qm', 'baseline')
        guard = SnapshotGuard(snapshot, bridge.git(snapshot, 'rev-parse', 'HEAD').decode().strip(), ['code.py'])
        self.client.initial = [NS(type='tool_execution_start', tool_name='bash', tool_call_id='a')]
        def tick():
            (snapshot/'context.py').unlink()
            self.client.emit(NS(type='tool_execution_end', tool_name='bash', tool_call_id='a', is_error=False))
            self.clock.tick = lambda: None
        self.clock.tick = tick
        def abort():
            self.assertTrue((self.run/'failure.json').exists())
            self.client.aborted = True
        self.client.abort = abort
        self.assertEqual(self.execute(candidate_paths=guard.inspect, preflight=guard.inspect), ('failed', 1))
        self.assertLess(self.clock.now, 1)
        failure = json.loads((self.run/'failure.json').read_text())
        self.assertEqual(failure['reason'], 'snapshot_scope_violation')
        self.assertEqual(failure['details'][0]['paths'], ['context.py'])
        self.assertTrue(self.client.aborted)
        self.assertFalse(failure['terminal_event'])

    def test_damaged_snapshot_preflight_does_not_start_or_prompt(self):
        from snapshot_guard import SnapshotViolation
        def preflight(): raise SnapshotViolation('snapshot_unavailable')
        with mock.patch.object(self.client, 'start') as start, mock.patch.object(self.client, 'prompt') as prompt:
            self.assertEqual(self.execute(preflight=preflight), ('failed', 1))
            start.assert_not_called(); prompt.assert_not_called()
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'], 'snapshot_unavailable')
        self.assertEqual((self.run/'events.jsonl').read_text(), '')
        self.assertFalse(json.loads((self.run/'progress.json').read_text())['terminal_event'])

    def test_prompt_ack_and_nonterminal_end_are_not_success(self):
        self.client.initial = [NS(type='agent_end', is_terminal=False)]
        self.assertEqual(self.execute(), ('stalled', 1))
        self.assertTrue(self.client.aborted); self.assertTrue(self.client.stopped)
        self.assertEqual(len(self.client.steers), 0)
        warning = json.loads((self.run/'watchdog-warning.json').read_text())
        self.assertEqual(warning['action'], 'observe')
        self.assertNotIn('rpc_ack', warning)
        self.assertEqual(warning['first_output_wait']['state'], 'waiting')
        self.assertAlmostEqual(warning['first_output_wait']['stop_at_elapsed_seconds'], .7)
        progress = json.loads((self.run/'progress.json').read_text())
        self.assertEqual(progress['stop_reason'], 'tool_idle')
        self.assertEqual(progress['first_output_wait']['state'], 'ended_without_output')
        self.assertIsNone(progress['first_output_wait']['remaining_seconds'])

    def test_user_echo_is_not_model_output_and_cancel_is_not_timeout(self):
        self.client.initial = [NS(type='agent_start'), NS(type='turn_start'),
            NS(type='message_end', message={'role':'user', 'content':'PRIVATE'})]
        self.clock.tick = lambda: (self.run/'cancel-requested').write_text('cancel') if self.clock.now >= .5 else None
        self.assertEqual(self.execute(), ('cancelled', 1))
        progress = json.loads((self.run/'progress.json').read_text())
        self.assertEqual(progress['phase'], 'awaiting_model_output')
        self.assertEqual(progress['stop_reason'], 'cancel_requested')
        self.assertEqual(progress['first_output_wait']['state'], 'ended_without_output')
        self.assertIsNone(progress['first_output_wait']['remaining_seconds'])
        marks = progress['startup']['milestones']
        self.assertIsNotNone(marks['rpc_ready'])
        self.assertIsNotNone(marks['prompt_ack_received'])
        self.assertIsNotNone(marks['first_rpc_event'])
        self.assertIsNone(marks['first_assistant_event'])
        self.assertIsNone(marks['first_model_delta'])
        self.assertNotIn('PRIVATE', (self.run/'startup.json').read_text())

    def test_real_model_delta_still_allows_the_existing_bounded_steer(self):
        self.client.initial = [NS(type='message_update',
            assistant_message_event={'type':'thinking_delta','delta':'PRIVATE'})]
        self.assertEqual(self.execute(), ('stalled', 1))
        self.assertEqual(len(self.client.steers), 1)
        self.assertEqual(json.loads((self.run/'watchdog-warning.json').read_text())['action'], 'steer')
        self.assertEqual(json.loads((self.run/'progress.json').read_text())['first_output_wait']['state'], 'output_observed')

    def test_start_ready_and_prompt_failures_preserve_the_last_milestone(self):
        for hook, expected in [('start','rpc_starting'), ('get_state','checking_model_state'),
                               ('prompt','awaiting_prompt_ack')]:
            with self.subTest(hook=hook), mock.patch.object(self.client, hook, side_effect=RuntimeError('PRIVATE')):
                with self.assertRaises(RuntimeError):
                    self.execute()
                saved = json.loads((self.run/'startup.json').read_text())
                self.assertEqual(saved['stage'], expected)
                self.assertNotIn('PRIVATE', json.dumps(saved))
                self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'], 'rpc_exception')

    def test_live_thinking_does_not_reset_stall(self):
        self.clock.tick = lambda: self.client.emit(NS(type='message_update'))
        self.assertEqual(self.execute()[0], 'stalled')
        p = json.loads((self.run/'progress.json').read_text())
        self.assertLess(p['last_event_age_seconds'], .01)
        self.assertGreaterEqual(p['idle_tool_seconds'], .7)

    def test_missing_prompt_does_not_claim_prompt_call_started(self):
        (self.run/'prompt.md').unlink()
        with mock.patch.object(self.client, 'prompt') as prompt:
            with self.assertRaises(FileNotFoundError):
                self.execute()
            prompt.assert_not_called()
        saved = json.loads((self.run/'startup.json').read_text())
        self.assertEqual(saved['stage'], 'prompt_not_called')
        self.assertIsNotNone(saved['milestones']['model_state_checked'])
        self.assertIsNone(saved['milestones']['prompt_call_started'])
        self.assertIsNone(saved['milestones']['prompt_ack_received'])
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'], 'rpc_exception')

    def test_active_tool_uses_hard_deadline(self):
        self.client.initial = [NS(type='tool_execution_start', tool_name='bash', tool_call_id='a')]
        self.assertEqual(self.execute()[0], 'timed_out')
        self.assertGreaterEqual(self.clock.now, 10)
        self.assertEqual(self.client.steers, [])

    def test_terminal_event_completes(self):
        self.client.initial = [NS(type='agent_end', is_terminal=True)]
        self.assertEqual(self.execute(), ('exited', 0))
        self.assertFalse(self.client.aborted)

    def test_cancel_wins_over_terminal_event(self):
        (self.run/'cancel-requested').write_text('cancel')
        self.client.initial = [NS(type='agent_end', is_terminal=True)]
        self.assertEqual(self.execute()[0], 'cancelled')

    def test_external_steering_is_acknowledged(self):
        (self.run/'controls').mkdir()
        bridge.dump(self.run/'controls/a.json', {'message': 'limit scope'})
        self.execute()
        self.assertEqual(self.client.steers[0], 'limit scope')
        self.assertTrue((self.run/'controls/a.ack').exists())

    def test_v41_frozen_identity_accepts_rpc_and_keeps_old_model_distinct(self):
        from worker_model import DEFAULT_MODEL, LEGACY_MODEL, model_id
        self.task['worker_model'] = DEFAULT_MODEL
        self.client.state.model.id = model_id(DEFAULT_MODEL)
        self.client.initial = [NS(type='agent_end', is_terminal=True)]
        self.execute({'worker_model': DEFAULT_MODEL})
        self.assertEqual(bridge.load(self.run/'rpc-state.json')['model'], model_id(DEFAULT_MODEL))
        self.client.state.model.id = model_id(LEGACY_MODEL)
        with mock.patch.object(self.client, 'prompt') as prompt:
            with self.assertRaisesRegex(RuntimeError, 'model/thinking'):
                self.execute({'worker_model': DEFAULT_MODEL})
            prompt.assert_not_called()

    def test_changed_frozen_identity_fails_before_rpc_client_creation(self):
        from worker_model import DEFAULT_MODEL, LEGACY_MODEL
        self.task['worker_model'] = DEFAULT_MODEL
        with mock.patch.object(self.client, 'start') as start:
            with self.assertRaisesRegex(ValueError, 'Worker model changed'):
                self.execute({'worker_model': LEGACY_MODEL})
            start.assert_not_called()

    def test_model_mismatch_stops_without_prompt(self):
        self.client.state.thinking_level = 'high'
        with self.assertRaisesRegex(RuntimeError, 'model/thinking'):
            self.execute()
        self.assertTrue(self.client.stopped)

    def test_resume_session_mismatch_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'different session'):
            self.execute({'resume_session': '/different/session'})
        self.assertTrue(self.client.stopped)

    def test_candidate_from_bash_counts_as_first_write(self):
        self.changed = ['code.py']
        self.client.initial = [NS(type='agent_end', is_terminal=True)]
        self.execute()
        p = json.loads((self.run/'progress.json').read_text())
        self.assertIsNotNone(p['first_write_at'])

    def test_event_log_omits_reasoning(self):
        event = NS(type='message_end', message={'role':'assistant','model':'deepseek-v4-flash',
                   'content':[{'type':'thinking','thinking':'PRIVATE'},{'type':'text','text':'done'}]})
        self.assertNotIn('PRIVATE', json.dumps(r.compact_event(event)))

    def test_real_sdk_command_observation_survives_clean_final_claim(self):
        from omp_rpc.protocol import parse_notification
        # Never execute this command; replay the real SDK tool-start shape only.
        command = 'env TOKEN=SYNTHETIC_SECRET rm -rf ./__pycache__'
        start = parse_notification({'type': 'tool_execution_start', 'toolName': 'bash',
                                    'toolCallId': 'audit-1', 'args': {'command': command}})
        self.client.initial = [start,
            NS(type='tool_execution_end', tool_name='bash', tool_call_id='audit-1', is_error=False),
            NS(type='message_end', message={'role': 'assistant', 'model': 'deepseek-v4-flash',
                                          'content': [{'type': 'text', 'text': 'no shell cleanup'}]}),
            NS(type='agent_end', is_terminal=True)]
        self.assertEqual(self.execute(), ('exited', 0))
        audit = json.loads((self.run/'command-audit.json').read_text())
        self.assertEqual(audit['observation_counts']['requested_recursive_rm'], 1)
        self.assertTrue(audit['review_required'])
        self.assertEqual(audit['compliance'], 'not_assessed')
        self.assertEqual(json.loads((self.run/'progress.json').read_text())['command_audit'], audit)
        result = bridge.summarize_events(self.run)
        self.assertEqual(result['command_audit'], audit)
        self.assertEqual(result['final_text'], 'no shell cleanup')
        for name in ('events.jsonl', 'progress.json', 'command-audit.json', 'worker-result.json'):
            self.assertNotIn('SYNTHETIC_SECRET', (self.run/name).read_text())
            self.assertNotIn('__pycache__', (self.run/name).read_text())

    def test_missing_args_are_explicit_and_new_attempt_audit_starts_empty(self):
        bridge.dump(self.run/'command-audit.json', {'obsolete': True})
        self.client.initial = [NS(type='tool_execution_start', tool_name='bash', tool_call_id='legacy'),
                               NS(type='agent_end', is_terminal=True)]
        self.execute()
        audit = json.loads((self.run/'command-audit.json').read_text())
        self.assertNotIn('obsolete', audit)
        self.assertEqual(audit['status_counts']['missing_command'], 1)
        self.assertTrue(audit['review_required'])

    def test_unsupported_compile_text_cues_survive_rpc_and_final_summary(self):
        from omp_rpc.protocol import parse_notification
        command = ('python3 -B - <<\x27PY\x27\nimport py_compile\n'
                   'py_compile.compile("source.py", cfile="/tmp/SYNTHETIC_PRIVATE.pyc")\nPY')
        start = parse_notification({'type': 'tool_execution_start', 'toolName': 'bash',
                                    'toolCallId': 'compile-cue', 'args': {'command': command}})
        self.client.initial = [start, NS(type='tool_execution_end', tool_name='bash',
                                        tool_call_id='compile-cue', is_error=False),
                               NS(type='agent_end', is_terminal=True)]
        self.assertEqual(self.execute(), ('exited', 0))
        audit = json.loads((self.run/'command-audit.json').read_text())
        self.assertEqual(audit['status_counts']['unsupported_syntax'], 1)
        self.assertEqual(audit['text_observation_counts']['explicit_bytecode_writer_mention'], 1)
        self.assertEqual(audit['text_observation_counts']['absolute_tmp_path_mention'], 1)
        self.assertEqual(audit['text_scanned_calls'], 1)
        self.assertEqual(audit['compliance'], 'not_assessed')
        self.assertEqual(bridge.summarize_events(self.run)['command_audit'], audit)
        for name in ('events.jsonl', 'progress.json', 'command-audit.json', 'worker-result.json'):
            self.assertNotIn('SYNTHETIC_PRIVATE', (self.run/name).read_text())

    def test_new_policy_stops_test_pipeline_and_preserves_original_failure(self):
        from process_evidence import POLICY
        self.task['process_evidence_policy'] = dict(POLICY)
        self.client.initial = [
            NS(type='tool_execution_start',tool_name='bash',tool_call_id='pipe',
               args={'command':'env PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_code -v 2>&1 | tail -60'}),
            NS(type='tool_execution_end',tool_name='bash',tool_call_id='pipe',is_error=False,
               result={'content':[{'type':'text','text':'fixture tail, not full test evidence'}]}),
            NS(type='agent_end',is_terminal=True)]
        self.assertEqual(self.execute(meta={'process_evidence_policy':dict(POLICY)}),('failed',1))
        self.assertTrue(self.client.aborted)
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'],'process_constraint_request')
        self.assertIn('tail -60',(self.run/'tool-evidence.jsonl').read_text())

    def test_manual_probe_cues_survive_rpc_without_claiming_interception(self):
        from process_evidence import POLICY_V2 as POLICY, binding
        self.task['process_evidence_policy']=dict(POLICY)
        command='python3 -c "render(\'/tmp/SYNTHETIC_PRIVATE.png\')" 2>&1 | head -20'
        self.client.initial=[NS(type='tool_execution_start',tool_name='bash',tool_call_id='probe',
                                args={'command':command}),
            NS(type='tool_execution_end',tool_name='bash',tool_call_id='probe',is_error=False,
               result={'content':[{'type':'text','text':'fixture only'}]}),
            NS(type='agent_end',is_terminal=True)]
        self.assertEqual(self.execute(meta={'process_evidence_policy':dict(POLICY)}),('exited',0))
        self.assertFalse(self.client.aborted)
        audit=json.loads((self.run/'command-audit.json').read_text())
        self.assertEqual(audit['version'],2)
        self.assertEqual(audit['review_cue_counts']['output_filter_pipeline_mention'],1)
        self.assertEqual(audit['review_cue_counts']['python_header_without_explicit_guard_mention'],1)
        self.assertEqual(audit['compliance'],'not_assessed')
        self.assertEqual(binding(self.run)['audit'],audit)
        self.assertEqual(bridge.summarize_events(self.run)['command_audit'],audit)
        for name in ('events.jsonl','progress.json','command-audit.json','worker-result.json'):
            self.assertNotIn('SYNTHETIC_PRIVATE',(self.run/name).read_text())
        self.assertIn('SYNTHETIC_PRIVATE',(self.run/'tool-evidence.jsonl').read_text())

    def test_v3_dependency_probe_stops_after_observation_despite_successful_tool_result(self):
        from process_evidence import POLICY, binding
        self.task['process_evidence_policy']=dict(POLICY)
        command='python3 -B -c "import PRIVATE_MISSING" 2>&1 | tail -1'
        self.client.initial=[NS(type='tool_execution_start',tool_name='bash',tool_call_id='probe',args={'command':command}),
            NS(type='tool_execution_end',tool_name='bash',tool_call_id='probe',is_error=False,
               result={'content':[{'type':'text','text':'ModuleNotFoundError fixture'}]}),NS(type='agent_end',is_terminal=True)]
        self.assertEqual(self.execute(meta={'process_evidence_policy':dict(POLICY)}),('failed',1))
        self.assertTrue(self.client.aborted)
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'],'process_constraint_request')
        bound=binding(self.run,async_required=True,live_output_required=True)
        self.assertEqual(bound['blocked_requests'],['python_live_output_filter'])
        self.assertEqual(json.loads((self.run/'progress.json').read_text())['live_output'],bound['live_output'])
        self.assertEqual(bridge.summarize_events(self.run)['live_output'],bound['live_output'])
        self.assertNotIn('PRIVATE_MISSING',(self.run/'events.jsonl').read_text())

    def test_v3_passive_log_read_does_not_stop_worker(self):
        from process_evidence import POLICY
        self.task['process_evidence_policy']=dict(POLICY)
        self.client.initial=[NS(type='tool_execution_start',tool_name='bash',tool_call_id='readlog',args={'command':'cat saved.log | tail -1'}),
            NS(type='tool_execution_end',tool_name='bash',tool_call_id='readlog',is_error=False,result={}),NS(type='agent_end',is_terminal=True)]
        self.assertEqual(self.execute(meta={'process_evidence_policy':dict(POLICY)}),('exited',0))
        self.assertFalse(self.client.aborted)

    def test_new_policy_keeps_unsupported_parameters_for_bound_review(self):
        from process_evidence import POLICY, binding, validate_review
        self.task['process_evidence_policy'] = dict(POLICY)
        meta={'process_evidence_policy':dict(POLICY)}
        self.client.initial = [
            NS(type='tool_execution_start',tool_name='bash',tool_call_id='quoted',
               args={'command':'echo "SYNTHETIC_PRIVATE"'}),
            NS(type='tool_execution_end',tool_name='bash',tool_call_id='quoted',is_error=False,
               result={'content':[{'type':'text','text':'fixture output'}]}),
            NS(type='agent_end',is_terminal=True)]
        self.assertEqual(self.execute(meta=meta),('exited',0))
        self.assertEqual(binding(self.run)['audit']['status_counts']['unsupported_syntax'],1)
        self.assertNotIn('SYNTHETIC_PRIVATE',(self.run/'events.jsonl').read_text())
        self.assertIn('SYNTHETIC_PRIVATE',(self.run/'tool-evidence.jsonl').read_text())
        with self.assertRaises(RuntimeError):validate_review(self.run,self.task,meta,{'verdict':'pass'})

    def test_cancel_keeps_original_reason_and_observed_prohibited_request(self):
        from process_evidence import POLICY, observed_blocked_requests
        self.task['process_evidence_policy']=dict(POLICY)
        (self.run/'cancel-requested').write_text('cancel')
        self.client.initial=[NS(type='tool_execution_start',tool_name='bash',tool_call_id='pipe',
            args={'command':'python3 -B -m unittest test_code -v 2>&1 | tail -60'})]
        self.assertEqual(self.execute(meta={'process_evidence_policy':dict(POLICY)}),('cancelled',1))
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'],'cancel_requested')
        self.assertEqual(observed_blocked_requests(self.run),['test_output_pipeline'])
        self.assertTrue(self.client.aborted)

    def test_new_policy_missing_parameters_cannot_get_complete_binding(self):
        from process_evidence import POLICY, binding
        self.task['process_evidence_policy'] = dict(POLICY)
        self.client.initial = [NS(type='tool_execution_start',tool_name='bash',tool_call_id='missing'),
            NS(type='tool_execution_end',tool_name='bash',tool_call_id='missing',is_error=False,result={}),
            NS(type='agent_end',is_terminal=True)]
        self.assertEqual(self.execute(meta={'process_evidence_policy':dict(POLICY)}),('exited',0))
        with self.assertRaisesRegex(RuntimeError,'parameters unavailable'):binding(self.run)

    def test_background_string_result_and_terminal_complete(self):
        from omp_rpc.protocol import parse_notification
        event = parse_notification({'type': 'message_end', 'message': {
            'role': 'custom', 'customType': 'async-result', 'content': 'background complete',
            'display': True, 'timestamp': 1}})
        self.client.initial = [NS(type='tool_execution_start', tool_name='write', tool_call_id='a'),
                               NS(type='tool_execution_end', tool_name='write', tool_call_id='a', is_error=False),
                               NS(type='turn_end'), NS(type='turn_start'),
                               NS(type='message_start'), event, NS(type='agent_end', is_terminal=True)]
        self.assertEqual(self.execute(), ('exited', 0))
        self.assertIn('background complete', (self.run/'events.jsonl').read_text())
        self.assertFalse((self.run/'failure.json').exists())

    def async_fixture(self):
        from process_evidence import POLICY
        self.task['process_evidence_policy']=dict(POLICY)
        self.client.initial=[NS(type='tool_execution_start',tool_name='bash',tool_call_id='bg-call',args={'command':'echo fixture'}),
            NS(type='tool_execution_end',tool_name='bash',tool_call_id='bg-call',is_error=False,
               result={'content':[{'type':'text','text':'Backgrounded'}],
                       'details':{'async':{'jobId':'bg_3','state':'running','type':'bash'}}}),
            NS(type='agent_end',is_terminal=True)]
        return {'process_evidence_policy':dict(POLICY)}

    def test_async_pending_survives_agent_end_until_total_deadline(self):
        meta=self.async_fixture()
        self.assertEqual(self.execute(meta=meta),('timed_out',1))
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'],'async_results_pending')
        progress=json.loads((self.run/'progress.json').read_text())
        self.assertEqual(progress['async_execution']['pending'],['bg_3'])
        self.assertFalse(self.client.steers)

    def test_async_delivery_requires_later_agent_end_and_keeps_failure_text(self):
        from test_async_evidence import notification
        from process_evidence import binding
        meta=self.async_fixture()
        delivered=False
        def tick():
            nonlocal delivered
            if self.clock.now>=.6 and not delivered:
                delivered=True
                self.client.emit(NS(type='message_end',message=notification()))
            if self.clock.now>=1.0:
                self.client.emit(NS(type='agent_end',is_terminal=True));self.clock.tick=lambda:None
        self.clock.tick=tick
        self.assertEqual(self.execute(meta=meta),('exited',0))
        self.assertGreaterEqual(self.clock.now,1.0)
        self.assertEqual(binding(self.run,async_required=True)['async_execution']['jobs'][0]['delivery'],'delivered')
        self.assertIn('code 130',(self.run/'tool-evidence.jsonl').read_text())
        self.assertEqual(bridge.summarize_events(self.run)['async_execution']['pending'],[])

    def test_async_delivery_without_new_terminal_is_not_worker_success(self):
        from test_async_evidence import notification
        meta=self.async_fixture()
        def tick():
            self.client.emit(NS(type='message_end',message=notification()));self.clock.tick=lambda:None
        self.clock.tick=tick
        self.assertNotEqual(self.execute(meta=meta)[0],'exited')
        self.assertFalse(json.loads((self.run/'progress.json').read_text())['terminal_event'])

    def reuse_fixture(self):
        import async_evidence as async_policy
        from process_evidence import POLICY_V2
        from test_async_reuse import call_events, notice
        self.task.update(process_evidence_policy=dict(POLICY_V2),
                         **{async_policy.POLICY_KEY: dict(async_policy.POLICY)})
        meta = {'process_evidence_policy': dict(POLICY_V2),
                async_policy.POLICY_KEY: dict(async_policy.POLICY)}
        bridge.dump(self.run/'task.json', self.task); bridge.dump(self.run/'metadata.json', meta)
        self.client.initial = [*call_events('first'), notice(1), *call_events('second'),
                               NS(type='agent_end', is_terminal=True)]
        return meta

    def test_reused_async_id_waits_for_its_own_delivery_and_later_terminal(self):
        from test_async_reuse import notice
        from process_evidence import binding
        meta = self.reuse_fixture(); delivered = False
        def tick():
            nonlocal delivered
            if self.clock.now >= .6 and not delivered:
                delivered = True; self.client.emit(notice(2))
            if self.clock.now >= 1.0:
                self.client.emit(NS(type='agent_end', is_terminal=True)); self.clock.tick = lambda: None
        self.clock.tick = tick
        self.assertEqual(self.execute(meta=meta), ('exited', 0))
        self.assertGreaterEqual(self.clock.now, 1.0)
        expected = binding(self.run, async_required=True)['async_execution']
        self.assertEqual([j['tool_call_id'] for j in expected['jobs']], ['first', 'second'])
        self.assertEqual(expected['pending'], [])
        self.assertEqual(bridge.summarize_events(self.run)['async_execution'], expected)
        self.assertEqual(json.loads((self.run/'progress.json').read_text())['async_execution'], expected)

    def test_reused_id_without_second_delivery_times_out_instead_of_inheriting_success(self):
        self.assertEqual(self.execute(meta=self.reuse_fixture()), ('timed_out', 1))
        failure = json.loads((self.run/'failure.json').read_text())
        self.assertEqual(failure['reason'], 'async_results_pending')
        summary = json.loads((self.run/'progress.json').read_text())['async_execution']
        self.assertEqual(summary['pending'], ['second'])
        self.assertEqual(summary['errors'], [])

    def test_async_cancellation_keeps_unknown_resource_status(self):
        meta=self.async_fixture();(self.run/'cancel-requested').write_text('cancel')
        self.assertEqual(self.execute(meta=meta),('cancelled',1))
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'],'cancel_requested')
        self.assertEqual(json.loads((self.run/'progress.json').read_text())['async_execution']['pending'],['bg_3'])

    def test_v1_worker_retains_prior_terminal_semantics(self):
        from process_evidence import POLICY_V1
        self.async_fixture();self.task['process_evidence_policy']=dict(POLICY_V1)
        self.assertEqual(self.execute(meta={'process_evidence_policy':dict(POLICY_V1)}),('exited',0))
        self.assertNotIn('async_execution',json.loads((self.run/'progress.json').read_text()))

    def test_sync_bash_keeps_v1_and_legacy_worker_result_schema(self):
        from process_evidence import POLICY_V1
        for policy in (None, POLICY_V1):
            with self.subTest(policy=policy):
                if policy is None:
                    self.task.pop('process_evidence_policy', None)
                    meta = {}
                else:
                    self.task['process_evidence_policy'] = dict(policy)
                    meta = {'process_evidence_policy': dict(policy)}
                self.client.initial = [
                    NS(type='tool_execution_start', tool_name='bash', tool_call_id='sync',
                       args={'command': 'echo fixture'}),
                    NS(type='tool_execution_end', tool_name='bash', tool_call_id='sync', is_error=False,
                       result={'content': [{'type': 'text', 'text': 'fixture'}], 'details': {'exitCode': 0}}),
                    NS(type='agent_end', is_terminal=True)]
                self.assertEqual(self.execute(meta=meta), ('exited', 0))
                self.assertNotIn('async_execution', bridge.summarize_events(self.run))
                self.assertNotIn('async_execution', json.loads((self.run/'worker-result.json').read_text()))

    def test_sdk_listener_failure_is_persisted_without_payload(self):
        from omp_rpc.protocol import parse_notification
        sdk = r.RpcClient(command=['never-started'], env={'TEST_ONLY': '1'})
        bad = parse_notification({'type': 'message_end', 'message': {
            'role': 'custom', 'content': 42, 'timestamp': 1}})
        def prompt(message):
            sdk.on_event(self.client.emit)
            sdk.on_listener_error(self.client.listener_callback)
            sdk._dispatch_listeners('event', bad.type, sdk._event_listeners, bad)
        self.client.prompt = prompt
        self.assertEqual(self.execute(), ('failed', 1))
        result = json.loads((self.run/'failure.json').read_text())
        self.assertEqual(result['reason'], 'listener_error')
        self.assertEqual(result['details'][0]['exception_type'], 'TypeError')
        self.assertEqual(result['details'][0]['source_type'], 'message_end')
        self.assertTrue(result['details'][0]['frames'])
        self.assertTrue(self.client.aborted)

    def test_protocol_exception_text_is_not_saved(self):
        self.client.prompt = lambda message: self.client.protocol_callback(RuntimeError('SYNTHETIC_SECRET_PAYLOAD'))
        self.assertEqual(self.execute(), ('failed', 1))
        text = (self.run/'failure.json').read_text()
        self.assertIn('protocol_error', text)
        self.assertNotIn('SYNTHETIC_SECRET_PAYLOAD', text)

    def test_process_exit_reason_and_original_code(self):
        self.client._process.poll = lambda: 17
        self.assertEqual(self.execute(), ('failed', 1))
        result = json.loads((self.run/'failure.json').read_text())
        self.assertEqual(result['reason'], 'process_exit_without_agent_end')
        self.assertEqual(result['natural_exit_code'], 17)

    def test_timeout_and_candidate_deadline_are_distinct(self):
        self.task['progress'] = {'warn_seconds': .3, 'stall_seconds': 5, 'first_write_seconds': .8}
        self.assertEqual(self.execute(), ('stalled', 1))
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'], 'first_candidate_deadline')

    def test_stream_subtypes_do_not_include_content(self):
        for kind, phase in (('thinking', 'thinking'), ('text', 'text'), ('toolcall', 'tool_arguments')):
            event = NS(type='message_update', assistant_message_event=NS(type=kind+'_delta', delta='PRIVATE'))
            item = r.compact_event(event)
            self.assertNotIn('PRIVATE', json.dumps(item))
            self.assertEqual(item['delta_chars'], 7)
            monitor = r.ProgressMonitor(0)
            monitor.event(item, 1)
            self.assertEqual(monitor.snapshot(2)['phase'], phase)
            self.assertEqual(monitor.snapshot(2)['stream_chars'][kind], 7)

    def test_real_sdk_nested_stream_dictionary_is_counted_without_content(self):
        from omp_rpc.protocol import parse_notification
        for kind, phase in (('thinking', 'thinking'), ('text', 'text'), ('toolcall', 'tool_arguments')):
            message = {'role': 'assistant', 'content': []}
            event = parse_notification({'type': 'message_update', 'message': message,
                'assistantMessageEvent': {'type': kind + '_delta', 'contentIndex': 0,
                                          'delta': 'PRIVATE', 'partial': message}})
            self.assertIsInstance(event.assistant_message_event, dict)
            item = r.compact_event(event)
            self.assertEqual(item['stream_kind'], kind)
            self.assertEqual(item['delta_chars'], 7)
            self.assertNotIn('PRIVATE', json.dumps(item))
            monitor = r.ProgressMonitor(0)
            monitor.event(item, 1)
            self.assertEqual(monitor.snapshot(2)['phase'], phase)
            self.assertEqual(monitor.snapshot(2)['stream_chars'][kind], 7)

    def test_real_sdk_retry_metadata_omits_error_payload(self):
        from omp_rpc.protocol import parse_notification
        event = parse_notification({'type': 'auto_retry_start', 'attempt': 1,
            'maxAttempts': 3, 'delayMs': 1000,
            'errorMessage': 'PRIVATE socket connection was closed unexpectedly'})
        item = r.compact_event(event)
        self.assertEqual(item['error_kind'], 'socket_closed')
        self.assertEqual(item['max_attempts'], 3)
        self.assertEqual(item['delay_ms'], 1000)
        self.assertNotIn('PRIVATE', json.dumps(item))
        event = parse_notification({'type':'auto_retry_end', 'success':False,
            'attempt':1, 'finalError':'PRIVATE OpenAI completions stream timed out while waiting for the first event'})
        item = r.compact_event(event)
        self.assertFalse(item['success'])
        self.assertEqual(item['error_kind'], 'first_event_timeout')
        self.assertNotIn('PRIVATE', json.dumps(item))

    def test_real_sdk_http_retry_category_omits_payload_and_preserves_outcome(self):
        from omp_rpc.protocol import parse_notification
        raw = '404 Upstream request failed: Endpoint is unavailable. PRIVATE (type=server_error)'
        for packet in [
                {'type': 'auto_retry_start', 'attempt': 1, 'maxAttempts': 1,
                 'delayMs': 500, 'errorMessage': raw},
                {'type': 'auto_retry_end', 'attempt': 1, 'success': False, 'finalError': raw}]:
            with self.subTest(event=packet['type']):
                item = r.compact_event(parse_notification(packet))
                self.assertEqual(item['error_kind'], 'http_endpoint_unavailable')
                self.assertEqual(item['attempt'], 1)
                self.assertNotIn('PRIVATE', json.dumps(item))
                self.assertNotIn('server_error', json.dumps(item))
                if packet['type'] == 'auto_retry_end':
                    self.assertIs(item['success'], False)
                else:
                    self.assertEqual(item['max_attempts'], 1)
                    self.assertEqual(item['delay_ms'], 500)

    def test_string_user_and_empty_content_are_valid(self):
        for content, expected in (('hello', [{'type':'text','text':'hello'}]), (None, []), ([], [])):
            result = r.compact_event(NS(type='message_end', message={'role':'user', 'content':content}))
            self.assertEqual(result['message']['content'], expected)

    def test_sdk_explicit_environment_does_not_inherit_provider_secrets(self):
        marker = self.run / 'env.json'
        program = ('import os,json,sys;from pathlib import Path;'
                   'Path(sys.argv[1]).write_text(json.dumps({"parent":os.getenv("TEAM_DEV_PARENT_SECRET"),'
                   '"worker":os.getenv("TEAM_DEV_WORKER")}));'
                   'print(json.dumps({"type":"ready"}),flush=True);sys.stdin.read()')
        with mock.patch.dict(os.environ, {'TEAM_DEV_PARENT_SECRET': 'synthetic-only'}):
            with r.RpcClient(command=[sys.executable, '-c', program, str(marker)],
                             env={'TEAM_DEV_WORKER': 'yes'}, startup_timeout=3):
                self.assertEqual(json.loads(marker.read_text()), {'parent':None, 'worker':'yes'})


if __name__ == '__main__': unittest.main()
