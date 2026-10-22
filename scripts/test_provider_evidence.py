import json
from pathlib import Path
import tempfile
import unittest

from provider_evidence import provider_access, request_configuration
from run_diagnostics import diagnose_run


class ProviderEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def diagnose(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = diagnose_run(self.root)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(result['provider_access']['current_service_health'], 'unknown')
        self.assertIsNone(result['provider_access']['provider_http_request_observed'])
        return result

    def test_route_capture_uses_fixed_categories_and_does_not_echo_arbitrary_urls(self):
        for url, expected in [
                ('https://opencode.ai/inference/openai/v1', 'opencode_inference_openai_v1'),
                ('https://opencode.ai/inference/openai/v1/', 'opencode_inference_openai_v1'),
                ('https://opencode.ai/zen/v1', 'opencode_zen_v1'),
                ('https://opencode.ai/zen/v1/chat/completions', 'other_or_unknown'),
                ('https://PRIVATE@opencode.ai/zen/v1', 'other_or_unknown'),
                ('https://opencode.ai/zen/v1?token=PRIVATE', 'other_or_unknown'),
                ('https://opencode.ai/zen/v1#PRIVATE', 'other_or_unknown'),
                ('https://opencode.ai.PRIVATE/zen/v1', 'other_or_unknown'),
                ('http://127.0.0.1:1/PRIVATE', 'other_or_unknown'),
                ('https://opencode.ai/PRIVATE', 'other_or_unknown'),
                (None, 'other_or_unknown'), (True, 'other_or_unknown'),
                ({'PRIVATE': 1}, 'other_or_unknown')]:
            with self.subTest(expected=expected, url_type=type(url).__name__):
                route = request_configuration('openai-completions', url)
                self.assertEqual(route['endpoint_family'], expected)
                self.assertEqual(route['api_mode'], 'openai-completions')
                self.assertNotIn('PRIVATE', json.dumps(route))
                self.assertNotIn('https:', json.dumps(route))
        self.assertEqual(request_configuration('PRIVATE', None)['api_mode'], 'other_or_unknown')

    def test_unstarted_preparation_is_not_a_model_failure(self):
        self.write('metadata.json', {'process': 'not_started', 'delivery': 'prepared'})
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'prepared_not_started')
        self.assertEqual(result['provider_access']['preparation'], 'prepared_without_runtime_evidence')
        self.assertEqual(result['provider_access']['generation_observation'], 'not_started_in_saved_records')
        self.assertEqual(result['provider_access']['local_selector'], 'unknown')
        self.assertEqual(result['model_error_count'], 0)
        self.assertEqual(result['stop_reason'], 'unknown')
        self.assertEqual(result['recovery']['next_step'], 'inspect_run_state')

    def test_conflicting_preparation_does_not_hide_saved_http_failure(self):
        self.write('metadata.json', {'process': 'not_started', 'delivery': 'prepared'})
        self.write('failure.json', {'reason': 'worker_evidence_invalid'})
        self.write('worker-result.json', {'errors': ['404 Endpoint is unavailable PRIVATE'],
                                        'tools': [], 'final_text': ''})
        result = self.diagnose()
        self.assertEqual(result['assessment'], 'model_http_errors_observed')
        self.assertEqual(result['stop_reason'], 'worker_evidence_invalid')
        self.assertEqual(result['provider_access']['preparation'], 'prepared_with_runtime_evidence_conflict')
        self.assertEqual(result['provider_access']['generation_observation'], 'http_errors_recorded')

    def test_partial_runtime_files_and_metadata_prevent_not_started_claim(self):
        for name in ('rpc-state.json', 'startup.json', 'events.jsonl', 'invocation.json', 'stderr.log'):
            with self.subTest(name=name):
                self.write('metadata.json', {'process': 'not_started', 'delivery': 'prepared'})
                (self.root / name).write_text('PRIVATE')
                result = self.diagnose()
                self.assertEqual(result['provider_access']['preparation'], 'prepared_with_runtime_evidence_conflict')
                self.assertNotEqual(result['assessment'], 'prepared_not_started')
                (self.root / name).unlink()
        self.write('metadata.json', {'process': 'not_started', 'delivery': 'prepared', 'started_at': None})
        self.assertEqual(self.diagnose()['provider_access']['preparation'], 'prepared_with_runtime_evidence_conflict')

    def test_local_selector_pass_does_not_establish_generation_or_health(self):
        self.write('rpc-state.json', {'model': 'deepseek-v4-flash', 'provider': 'opencode-ds',
            'request_configuration': request_configuration('openai-completions', 'https://opencode.ai/zen/v1')})
        result = self.diagnose()['provider_access']
        self.assertEqual(result['local_selector'], 'expected_selector_observed')
        self.assertEqual(result['request_configuration_status'], 'recorded')
        self.assertEqual(result['request_configuration']['endpoint_family'], 'opencode_zen_v1')
        self.assertEqual(result['generation_observation'], 'unknown')
        self.write('worker-result.json', {'errors': ['404 Endpoint is unavailable PRIVATE'],
                                        'tools': [], 'final_text': ''})
        result = self.diagnose()['provider_access']
        self.assertEqual(result['local_selector'], 'expected_selector_observed')
        self.assertEqual(result['generation_observation'], 'http_errors_recorded')

    def test_legacy_model_state_does_not_invent_historical_endpoint(self):
        self.write('rpc-state.json', {'model': 'deepseek-v4-flash', 'provider': 'opencode-ds'})
        result = self.diagnose()['provider_access']
        self.assertEqual(result['local_selector'], 'expected_selector_observed')
        self.assertEqual(result['request_configuration_status'], 'not_recorded')
        self.assertIsNone(result['request_configuration'])
        self.write('rpc-state.json', {'model': 'PRIVATE', 'provider': 'PRIVATE'})
        self.assertEqual(self.diagnose()['provider_access']['local_selector'], 'other_selector_observed')

    def test_route_readback_rejects_malformed_markers_and_omits_extra_payload(self):
        good = request_configuration('openai-completions', 'https://opencode.ai/inference/openai/v1')
        for route in [None, [], 'PRIVATE', dict(good, version=True), dict(good, version=1.0),
                      dict(good, basis='PRIVATE'), dict(good, api_mode=['PRIVATE']),
                      dict(good, endpoint_family={'PRIVATE': 1}), dict(good, endpoint_family='PRIVATE')]:
            with self.subTest(route_type=type(route).__name__):
                self.write('rpc-state.json', {'request_configuration': route})
                result = self.diagnose()['provider_access']
                self.assertEqual(result['request_configuration_status'], 'unreadable')
                self.assertIsNone(result['request_configuration'])
        self.write('rpc-state.json', {'request_configuration': dict(good, secret='PRIVATE')})
        self.assertEqual(self.diagnose()['provider_access']['request_configuration'], good)

    def test_missing_invalid_and_empty_state_do_not_establish_selection(self):
        self.assertEqual(self.diagnose()['provider_access']['local_selector'], 'unknown')
        for state in [None, [], {}, {'model': ' ', 'provider': 'opencode-ds'},
                      {'model': 'deepseek-v4-flash', 'provider': None}]:
            self.write('rpc-state.json', state)
            self.assertEqual(self.diagnose()['provider_access']['local_selector'], 'unknown')
        (self.root / 'rpc-state.json').write_text('{PRIVATE')
        self.assertEqual(self.diagnose()['provider_access']['request_configuration_status'], 'unreadable')

    def test_saved_output_does_not_certify_current_availability_or_completion(self):
        self.write('startup.json', {'version': 1, 'milestones': {'first_model_delta': 2}})
        result = self.diagnose()
        self.assertEqual(result['provider_access']['generation_observation'], 'model_output_recorded')
        self.write('worker-result.json', {'errors': ['404 Endpoint is unavailable PRIVATE'],
                                        'final_text': 'PRIVATE report', 'tools': ['read']})
        result = self.diagnose()
        self.assertEqual(result['provider_access']['generation_observation'], 'http_errors_recorded')
        self.assertIn('model_errors_present', result['worker_evidence_blockers'])
        self.assertEqual(result['output_observation'], 'changes_unknown_with_final_text')

    def test_402_output_and_local_selector_do_not_verify_balance_or_restore_generation(self):
        self.write('rpc-state.json', {'model': 'deepseek-v4-flash', 'provider': 'opencode-ds'})
        self.write('startup.json', {'version': 1, 'milestones': {'first_model_delta': 2}})
        self.write('worker-result.json', {'errors': ['402 Insufficient account funds PRIVATE'],
                                        'tools': ['write'], 'final_text': 'PRIVATE saved'})
        access = self.diagnose()['provider_access']
        self.assertEqual(access['local_selector'], 'expected_selector_observed')
        self.assertEqual(access['generation_observation'], 'http_errors_recorded')
        self.assertEqual(access['billing_observation'], 'insufficient_account_funds_reported')
        self.assertFalse(access['account_balance_verified'])

    def test_billing_readback_allows_only_positive_integer_classified_counts(self):
        cases = [(None, 1), ([], 1), ('PRIVATE', 1), ({'PRIVATE': 2}, 1),
                 ({'http_insufficient_account_funds': True}, 1),
                 ({'http_insufficient_account_funds': 'PRIVATE'}, 1),
                 ({'http_insufficient_account_funds': -1}, 1),
                 ({'http_insufficient_account_funds': 1}, 0)]
        for kinds, count in cases:
            with self.subTest(kinds_type=type(kinds).__name__):
                result = provider_access(self.root, {}, {}, {}, http_errors=count,
                                         final_text_present=False, http_error_kinds=kinds)
                self.assertEqual(result['billing_observation'], 'not_observed')
                self.assertFalse(result['account_balance_verified'])
                self.assertNotIn('PRIVATE', json.dumps(result))
        result = provider_access(self.root, {}, {}, {}, http_errors=2, final_text_present=False,
                                 http_error_kinds={'http_payment_required': 1,
                                                   'http_insufficient_account_funds': 1})
        self.assertEqual(result['billing_observation'], 'insufficient_account_funds_reported')
        legacy_call = provider_access(self.root, {}, {}, {}, http_errors=0, final_text_present=True)
        self.assertEqual(legacy_call['billing_observation'], 'not_observed')


if __name__ == '__main__':
    unittest.main()
