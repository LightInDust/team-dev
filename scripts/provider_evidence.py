"""Local selection/configuration observations, never an endpoint health check."""
import json
from pathlib import Path
from worker_model import diagnostic_model_id, PROVIDER


ENDPOINT_FAMILIES = {
    'https://opencode.ai/inference/openai/v1': 'opencode_inference_openai_v1',
    'https://opencode.ai/zen/v1': 'opencode_zen_v1',
}
API_MODES = {'openai-completions', 'other_or_unknown'}
FAMILIES = set(ENDPOINT_FAMILIES.values()) | {'other_or_unknown'}
RUNTIME_FILES = ('rpc-state.json', 'startup.json', 'progress.json', 'worker-result.json',
                 'termination.json', 'failure.json', 'events.jsonl', 'invocation.json',
                 'stdout.log', 'stderr.log', 'tool-evidence.jsonl')
RUNTIME_META = ('pid', 'process_stamp', 'started_at', 'ended_at', 'exit_code',
                'failure_reason', 'worker_mode', 'session_file')


def request_configuration(api, base_url):
    """Capture only fixed labels; URLs, queries, headers and secrets never persist."""
    endpoint = (ENDPOINT_FAMILIES.get(base_url.rstrip('/'), 'other_or_unknown')
                if isinstance(base_url, str) else 'other_or_unknown')
    return {'version': 1, 'basis': 'local_omp_model_configuration',
            'api_mode': 'openai-completions' if api == 'openai-completions' else 'other_or_unknown',
            'endpoint_family': endpoint}


def provider_access(run, meta, result, startup, *, http_errors, final_text_present,
                    http_error_kinds=None):
    root = Path(run)
    state, state_status = {}, 'not_recorded'
    try:
        state = json.loads((root / 'rpc-state.json').read_text())
        if not isinstance(state, dict):
            raise ValueError('Not an object')
        state_status = 'recorded'
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        state, state_status = {}, 'unreadable'
    selector = 'unknown'
    if all(isinstance(state.get(k), str) and state[k].strip() for k in ('model', 'provider')):
        selector = ('expected_selector_observed' if state['model'] == diagnostic_model_id(meta) and
                    state['provider'] == PROVIDER else 'other_selector_observed')
    route = state.get('request_configuration')
    route_status = 'unreadable' if state_status == 'unreadable' else 'not_recorded'
    safe_route = None
    if 'request_configuration' in state:
        valid = (isinstance(route, dict) and type(route.get('version')) is int and route['version'] == 1 and
                 route.get('basis') == 'local_omp_model_configuration' and
                 isinstance(route.get('api_mode'), str) and route['api_mode'] in API_MODES and
                 isinstance(route.get('endpoint_family'), str) and route['endpoint_family'] in FAMILIES)
        route_status = 'recorded' if valid else 'unreadable'
        if valid:
            safe_route = {k: route[k] for k in ('version', 'basis', 'api_mode', 'endpoint_family')}

    preparation = 'not_applicable_or_unknown'
    if meta.get('process') == 'not_started' and meta.get('delivery') == 'prepared':
        runtime = any((root / name).exists() for name in RUNTIME_FILES) or any(k in meta for k in RUNTIME_META)
        preparation = 'prepared_with_runtime_evidence_conflict' if runtime else 'prepared_without_runtime_evidence'
    marks = startup.get('milestones', {})
    delta = marks.get('first_model_delta') if isinstance(marks, dict) else None
    if preparation == 'prepared_without_runtime_evidence':
        generation = 'not_started_in_saved_records'
    elif http_errors:
        generation = 'http_errors_recorded'
    elif delta is not None or final_text_present:
        generation = 'model_output_recorded'
    elif (isinstance(result.get('final_text'), str) and not result['final_text'].strip() and
          result.get('tools') == []):
        generation = 'no_usable_completion_recorded'
    else:
        generation = 'unknown'
    billing = 'not_observed'
    if type(http_errors) is int and http_errors > 0 and isinstance(http_error_kinds, dict):
        for kind, label in (('http_insufficient_account_funds', 'insufficient_account_funds_reported'),
                            ('http_payment_required', 'payment_required_reported')):
            count = http_error_kinds.get(kind)
            if type(count) is int and count > 0:
                billing = label
                break
    return {'version': 1, 'preparation': preparation, 'local_selector': selector,
            'request_configuration_status': route_status, 'request_configuration': safe_route,
            'generation_observation': generation, 'current_service_health': 'unknown',
            'billing_observation': billing, 'account_balance_verified': False,
            'provider_http_request_observed': None}
