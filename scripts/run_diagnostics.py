"""Read-only, allowlisted diagnostics; never copy error payloads or model content."""
import json
import math
import re
from pathlib import Path
from startup_monitor import read_startup, first_output_wait
from provider_evidence import provider_access


def usable_final_text(value):
    return isinstance(value, str) and bool(value.strip())


def recovery_guidance(meta, *, recorded_changes=False):
    """Metadata-only hints, never validation or authorization to launch a worker."""
    policy = meta.get('process_evidence_policy')
    gated = ('worker_bash_gate' in meta or
             isinstance(policy, dict) and policy.get('version') == 4)
    attempt = meta.get('attempt')
    attempt = attempt if type(attempt) is int and attempt >= 1 else None
    stopped = meta.get('process') in ('stalled', 'cancelled', 'timed_out', 'failed')
    action = 'inspect_run_state'
    if stopped:
        if recorded_changes:
            action = 'inspect_preserved_candidate_for_independent_takeover'
        elif attempt is not None and attempt >= 2:
            action = 'reassess_scope_or_independent_takeover'
        elif gated:
            action = 'inspect_evidence_then_fresh_preparation'
        else:
            action = 'inspect_resume_requirements'
    return {'same_run_resume': 'prohibited_by_gate_policy' if gated else 'not_evaluated',
            'next_step': action, 'saved_attempt': attempt, 'eligibility_checked': False}


def output_observation(changed_count, final_text):
    """Combine saved observations without claiming current diff or report quality."""
    present = usable_final_text(final_text)
    if changed_count is None:
        return 'changes_unknown_with_final_text' if present else 'changes_unknown_without_final_text'
    if changed_count > 0:
        return 'recorded_changes_and_final_text' if present else 'recorded_changes_only'
    return 'final_text_only' if present else 'no_changes_or_final_text'


STREAM_ERROR_KINDS = frozenset({
    'stream_closed_before_finish_reason', 'stream_idle_timeout', 'first_event_timeout',
    'socket_closed', 'connection_reset', 'connection_refused', 'dns_lookup_failed',
    'connection_timeout',
})

LOOP_GUARD_ERROR_KIND = 'thinking_loop_guard_reported'
# Anchored saved-text label from OMP's loop guard after optional leading whitespace.
# A mention later in prose, a quote, a negation or a lookalike suffix stays unclassified.
LOOP_GUARD_RE = re.compile(r'\A\s*thinking loop detected(?=\s*(?:$|[:.]))',
                           re.IGNORECASE)


def http_error_status(value):
    """Recognize a leading status in saved error text, not an authenticated response."""
    if not isinstance(value, str):
        return None
    match = re.match(r'\A\s*(?:HTTP(?:/[0-9](?:\.[0-9])?)?\s+|Error code:\s*)?'
                     r'([45][0-9]{2})(?=$|[\s:])', value, re.IGNORECASE)
    return int(match[1]) if match else None


def error_kind(value):
    if not isinstance(value, str):
        return 'unclassified'
    text = value.lower()
    status = http_error_status(value)
    # A leading HTTP rejection takes precedence over words inside its body.
    # Otherwise a 503 body mentioning a socket could masquerade as a stream fault.
    if status is not None:
        if status == 402:
            return ('http_insufficient_account_funds' if
                    re.search(r'\binsufficient\s+account\s+funds\b', text) else 'http_payment_required')
        if status == 404:
            return 'http_endpoint_unavailable' if 'endpoint is unavailable' in text else 'http_not_found'
        if status in (401, 403):
            return 'http_auth_or_access_rejected'
        if status == 429:
            return 'http_rate_limited'
        if status == 408:
            return 'http_request_timeout'
        return 'http_server_error' if status >= 500 else 'http_client_error'
    if LOOP_GUARD_RE.match(value):
        return LOOP_GUARD_ERROR_KIND
    for fragment, kind in (
        ('closed before a finish_reason', 'stream_closed_before_finish_reason'),
        ('stalled while waiting for the next event', 'stream_idle_timeout'),
        ('timed out while waiting for the first event', 'first_event_timeout'),
        ('socket connection was closed unexpectedly', 'socket_closed'),
        ('econnreset', 'connection_reset'),
        ('econnrefused', 'connection_refused'),
        ('enotfound', 'dns_lookup_failed'),
        ('etimedout', 'connection_timeout'),
    ):
        if fragment in text:
            return kind
    return 'unclassified'


def number(value):
    if type(value) is int:
        return value if value >= 0 else None
    return value if type(value) is float and math.isfinite(value) and value >= 0 else None


def known(value, choices):
    return value if isinstance(value, str) and value in choices else 'unknown'


STOP_REASONS = {
    'tool_idle', 'first_candidate_deadline', 'total_deadline', 'terminal_agent_end',
    'worker_evidence_invalid', 'cancel_requested', 'transport_closed',
    'process_exit_without_agent_end', 'protocol_error', 'listener_error',
    'protocol_parse_error', 'rpc_client_error', 'rpc_exception',
    'snapshot_scope_violation', 'snapshot_undeclared_deletion',
    'snapshot_root_missing', 'snapshot_git_missing', 'snapshot_head_changed',
    'snapshot_scan_failed', 'snapshot_guard_policy_changed',
    'snapshot_unavailable',
    'async_results_pending', 'async_evidence_invalid', 'process_constraint_request',
    'candidate_closeout_deadline', 'undeclared_bash_request',
    'preexec_bash_denial_limit', 'preexec_gate_invalid',
}


def bash_rejection_assessment(exact):
    """Keep v1 missing match evidence unknown; v2 argument status can mask mismatch."""
    if not isinstance(exact, dict):
        return 'observed_request_outside_declared_tests'
    statuses = exact.get('status_counts', {})
    if not isinstance(statuses, dict) or not number(statuses.get('invalid_arguments')):
        return 'observed_request_outside_declared_tests'
    matches = exact.get('command_match_counts')
    valid_matches = (isinstance(matches, dict) and set(matches) == {'matched', 'not_matched', 'unavailable'} and
                     all(type(value) is int and value >= 0 for value in matches.values()))
    if number(statuses.get('undeclared_command')) or valid_matches and matches['not_matched'] > 0:
        return 'bash_command_and_arguments_rejected'
    if (valid_matches and type(exact.get('bash_calls')) is int and exact['bash_calls'] > 0 and
            matches['matched'] == exact['bash_calls'] and matches['not_matched'] == matches['unavailable'] == 0 and
            statuses.get('undeclared_command') == statuses.get('unavailable') == 0):
        return 'bash_tool_arguments_rejected'
    return 'bash_tool_arguments_rejected_command_match_unverified'


def diagnose_run(run):
    from worker_model import diagnostic_model_id
    root = Path(run)
    if not root.is_dir():
        raise ValueError('Run directory is missing')
    data, missing, unreadable = {}, [], []
    for name in ('metadata.json', 'progress.json', 'termination.json', 'failure.json', 'worker-result.json'):
        try:
            value = json.loads((root / name).read_text())
            if not isinstance(value, dict):
                raise ValueError('Not an object')
            data[name] = value
        except FileNotFoundError:
            missing.append(name)
        except (OSError, ValueError):
            unreadable.append(name)
    meta, progress, termination, failure, result = (data.get(name, {}) for name in (
        'metadata.json', 'progress.json', 'termination.json', 'failure.json', 'worker-result.json'))
    errors = result.get('errors', [])
    errors = errors if isinstance(errors, list) else []
    kinds, http_statuses = {}, {}
    for value in errors:
        kind = error_kind(value)
        kinds[kind] = kinds.get(kind, 0) + 1
        status = http_error_status(value)
        if status is not None:
            key = str(status)
            http_statuses[key] = http_statuses.get(key, 0) + 1
    client_errors = result.get('client_errors', termination.get('details', []))
    client_errors = client_errors if isinstance(client_errors, list) else []
    # Snapshot integrity evidence also lives in details; it is not a transport error.
    rpc_faults = sum(isinstance(e, dict) and (known(e.get('reason'), {
        'protocol_error', 'listener_error', 'protocol_parse_error', 'rpc_client_error',
        'rpc_exception', 'transport_closed'}) != 'unknown' or 'exception_type' in e) for e in client_errors)
    stop = known(failure.get('reason', meta.get('failure_reason', progress.get('stop_reason'))), STOP_REASONS)
    terminal = result.get('terminal_event', termination.get('terminal_event', progress.get('terminal_event')))
    candidate_paths = progress.get('changed_paths')
    changed_count = (len(candidate_paths) if isinstance(candidate_paths, list) and
                     all(isinstance(path, str) and path.strip() for path in candidate_paths) else None)
    first_write = number(progress.get('first_write_at'))
    candidate_observed = (changed_count > 0 or first_write is not None) if changed_count is not None else None
    blockers = []
    if 'worker-result.json' in data:
        if errors:
            blockers.append('model_errors_present')
        if not usable_final_text(result.get('final_text')):
            blockers.append('final_text_missing')
        expected_model = diagnostic_model_id(meta)
        if expected_model is None or result.get('models') != [expected_model]:
            blockers.append('expected_model_evidence_missing')
        if not result.get('tools'):
            blockers.append('tool_use_missing')
        if terminal is not True:
            blockers.append('terminal_agent_end_missing')
    stream_faults = sum(v for k, v in kinds.items() if k in STREAM_ERROR_KINDS)
    http_faults = sum(http_statuses.values())
    loop_guard_faults = kinds.get(LOOP_GUARD_ERROR_KIND, 0)
    if http_faults and (stream_faults or rpc_faults):
        assessment = 'model_http_and_transport_faults_observed'
    elif http_faults:
        assessment = 'model_http_errors_observed'
    elif stream_faults and rpc_faults:
        assessment = 'model_stream_and_rpc_faults_observed'
    elif stream_faults:
        assessment = 'model_stream_faults_observed'
    elif rpc_faults:
        assessment = 'rpc_client_faults_observed'
    elif loop_guard_faults:
        assessment = 'thinking_loop_guard_reported'
    elif errors:
        assessment = 'unclassified_model_errors_observed'
    elif stop == 'worker_evidence_invalid':
        assessment = 'completion_evidence_rejected'
    elif stop == 'undeclared_bash_request':
        assessment = bash_rejection_assessment(progress.get('exact_bash', result.get('exact_bash', {})))
    elif stop == 'preexec_bash_denial_limit':
        assessment = 'preexec_requests_blocked_correction_budget_exhausted'
    elif stop == 'preexec_gate_invalid':
        assessment = 'preexec_gate_evidence_unavailable_or_invalid'
    elif stop == 'candidate_closeout_deadline':
        assessment = 'candidate_handoff_budget_exhausted'
    elif stop in ('tool_idle', 'first_candidate_deadline', 'total_deadline') and candidate_observed is False:
        assessment = 'no_candidate_before_deadline'
    else:
        assessment = 'insufficient_evidence_for_cause'
    event_types = result.get('event_types', {})
    event_types = event_types if isinstance(event_types, dict) else {}
    stream = progress.get('stream_chars', {})
    stream = stream if isinstance(stream, dict) else {}
    counts = progress.get('tool_counts', {})
    counts = counts if isinstance(counts, dict) else {}
    startup = read_startup(root, meta)
    access = provider_access(root, meta, result, startup, http_errors=http_faults,
                             final_text_present=usable_final_text(result.get('final_text')),
                             http_error_kinds=kinds)
    if access['preparation'] == 'prepared_without_runtime_evidence':
        assessment = 'prepared_not_started'
    request_guidance = ({
        'next_step': 'inspect_endpoint_model_access_before_recovery',
        'root_cause': 'unknown', 'automatic_retry_authorized': False,
    } if http_faults else None)
    if request_guidance is not None and access['billing_observation'] != 'not_observed':
        request_guidance.update(
            next_step='defer_provider_retry_until_billing_resolution',
            reported_condition=access['billing_observation'],
            same_endpoint_retry='defer_pending_billing_resolution_evidence',
            configuration_change_authorized=False)
    loop_guard_guidance = None if not loop_guard_faults else {
        'basis': 'saved_error_text',
        'trigger_origin_verified': False,
        'detection_accuracy': 'unknown',
        'root_cause': 'unknown',
        'network_provider_root_cause': 'unknown',
        'automatic_retry_authorized': False,
        'configuration_change_authorized': False,
        'next_step': 'inspect_preserved_candidate_and_guard_evidence',
    }
    return {
        'diagnostic_version': 2,
        'startup': startup,
        'first_output_wait': first_output_wait(startup, progress, meta.get('execution_policy'),
                                              meta.get('process')),
        'provider_access': access,
        'assessment': assessment,
        'stop_reason': stop,
        'rpc_termination_reason': known(termination.get('reason'), STOP_REASONS),
        'terminal_agent_end': terminal if type(terminal) is bool else None,
        'elapsed_seconds': number(termination.get('elapsed_seconds', progress.get('elapsed_seconds'))),
        'idle_tool_seconds': number(progress.get('idle_tool_seconds')),
        'candidate_observed': candidate_observed,
        'changed_paths_count': changed_count,
        'output_observation': output_observation(changed_count, result.get('final_text')),
        'tool_counts': {k: number(counts[k]) for k in ('read', 'grep', 'glob', 'edit', 'write', 'bash') if k in counts},
        'model_error_count': len(errors), 'model_error_kinds': kinds,
        'model_http_error_count': http_faults, 'model_http_status_counts': http_statuses,
        'model_stream_fault_count': stream_faults,
        'model_loop_guard_error_count': loop_guard_faults,
        'loop_guard_guidance': loop_guard_guidance,
        'request_failure_guidance': request_guidance,
        'rpc_client_fault_count': rpc_faults, 'worker_evidence_blockers': blockers,
        'final_text_state': ('missing' if 'final_text' not in result else
                             'invalid_type' if not isinstance(result['final_text'], str) else
                             'present' if usable_final_text(result['final_text']) else 'blank'),
        'recovery': recovery_guidance(meta, recorded_changes=changed_count is not None and changed_count > 0),
        'auto_retry_events': {k: number(event_types.get(k, 0)) for k in ('auto_retry_start', 'auto_retry_end')},
        'stream_chars': {k: number(stream[k]) for k in ('thinking', 'text', 'toolcall', 'unknown') if k in stream},
        'stream_measurement': 'unclassified' if not any(number(stream.get(k)) for k in ('thinking', 'text', 'toolcall')) else 'classified',
        'missing_files': missing, 'unreadable_files': unreadable,
        'limits': [
            'This is an observation summary, not a root-cause or network-health verdict.',
            'Model stream errors cannot locate the fault in the local proxy, network, gateway, or provider.',
            'HTTP categories/statuses are heuristics from leading saved error text, not authenticated response evidence.',
            'A reported billing condition does not verify an account balance, identify the charged upstream account, or establish current service health.',
            'HTTP rejection does not identify a bad URL, unavailable model, account restriction, proxy, gateway or upstream cause.',
            'Inspect HTTP request configuration and availability before considering recovery; effort/deadline increases do not repair a rejected request.',
            'Local model selection and metadata GET success do not validate authenticated generation, streaming or tools.',
            'Saved request configuration is local OMP model metadata, not the actual wire URL, account access or service health.',
            'A prepared run without saved runtime evidence is not counted as a generation failure; conflicting records remain explicit.',
            'Error and retry counts cover the saved attempt; unknown:0 does not mean zero reasoning.',
            'Recovery hints do not validate policy, session, journals, resources, snapshot or target baseline.',
            'Cross-run recovery limits are coordinator-managed; a fresh run does not establish a new allowance.',
            'An agent_end or process exit zero does not satisfy worker, diff, test, and review gates.',
            'Output observations use saved progress and text presence; no current snapshot, report completeness or test claims are verified.',
            'Preserved candidates from failed attempts require independent takeover evidence; prior model errors remain blocking.',
            'No run state, budget, retry policy, or delivery evidence is changed by diagnosis.',
        ],
    }
