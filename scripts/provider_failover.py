"""Evidence required for a fresh, one-way provider recovery."""
from process_evidence import records
from provider_fault import fallback_reason
from worker_model import DEFAULT_MODEL


def recoverable_fault(run, meta, result):
    if (meta.get('process') not in ('failed', 'stalled', 'timed_out') or
            meta.get('failure_reason') not in ('worker_evidence_invalid', 'tool_idle',
                                               'first_candidate_deadline', 'total_deadline') or
            meta.get('attempt', 1) != 1 or result.get('client_errors') != []):
        return None
    reason = fallback_reason(result.get('errors'))
    if reason is None:
        return None

    # A transport error from an earlier, internally recovered turn must not
    # trigger a second worker. Only the last assistant turn may authorize one.
    _, events = records(run / 'events.jsonl')
    last = None
    for event in events:
        if event.get('type') == 'message_end' and event.get('message', {}).get('role') == 'assistant':
            last = event['message']
        if event.get('type') in ('tool_execution_start', 'tool_execution_end'):
            if event.get('toolName') not in ('read', 'grep', 'glob'):
                return None
        if 'asyncResult' in event or 'preexecGate' in event:
            return None
    if not last or last.get('model') != DEFAULT_MODEL.split('/')[1] or last.get('stopReason') != 'error':
        return None
    if fallback_reason([last.get('errorMessage')]) is None:
        return None
    # The independent tool journal contains Bash, lockout and async records.
    if records(run / 'tool-evidence.jsonl')[1]:
        return None
    gate = result.get('preexec_gate')
    if gate is not None and (gate.get('denied_requests') != 0 or gate.get('bash_decisions') != 0):
        return None
    return reason
