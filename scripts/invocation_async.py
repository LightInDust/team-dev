"""Pure join policy for structured v2 async Bash delivery in the read-only ledger.

Callers supply already-parsed private tool records and compact events; this
module performs no I/O, starts no process and copies no private message body.
Delivery is provenance only: it is never test success, resource cleanup or
acceptance. The whole-run private/compact validation is delegated to
``async_evidence.validate_binding`` so the ledger never invents a second job
identity algorithm.
"""
import async_evidence

SUPPORTED_VERSION = 2
ACK_STATES = ('queued', 'running')
LEGACY_JOB_KEYS = ('jobId', 'job_id', 'backgroundJobId', 'asyncJobId')
LOG_DIR_MARKER = 'WORKER_TEST_LOG_DIR='


def _details(result):
    return result.get('details') if isinstance(result, dict) else None


def unsupported(reason, version=None):
    return {'state': 'unsupported', 'reason': reason, 'version': version, 'jobs': []}


def presence(tools):
    """True when the private journal carries any native or legacy async marker."""
    for row in tools:
        if not isinstance(row, dict):
            continue
        if row.get('type') == 'async_result':
            return True
        if row.get('type') == 'tool_execution_end':
            details = _details(row.get('result'))
            if isinstance(details, dict) and ('async' in details or any(
                    key in details for key in LEGACY_JOB_KEYS)):
                return True
    return False


def protocol(task, metadata):
    """The frozen async policy version, or an unsupported reason when it changed."""
    task = task if isinstance(task, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    try:
        return async_evidence.protocol_version(task, metadata), None
    except ValueError:
        return None, 'async_execution_policy_changed'


def text_items(content):
    """Bounded text blocks from notice content; no other block type is copied."""
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    if isinstance(content, list):
        return [{'type': 'text', 'text': item['text']} for item in content
                if isinstance(item, dict) and item.get('type') == 'text' and
                isinstance(item.get('text'), str)]
    return []


def _carries_marker(result):
    return any(line.startswith(LOG_DIR_MARKER) for item in text_items(
        (result or {}).get('content')) for line in item['text'].splitlines())


def _ack_unsupported(tools):
    """Reject ACK shapes the join cannot certify; return a reason or None."""
    for row in tools:
        if not isinstance(row, dict) or row.get('type') != 'tool_execution_end':
            continue
        details = _details(row.get('result'))
        if not isinstance(details, dict):
            continue
        if any(key in details for key in LEGACY_JOB_KEYS):
            return 'async_legacy_job_keys_unsupported'
        descriptor = details.get('async')
        if descriptor is None:
            continue
        if 'exitCode' in details:
            # A background acknowledgement is not a completed result; a status field on
            # it cannot stand in for the retained test exit or the notice evidence.
            return 'async_ack_exit_code_unsupported'
        if row.get('is_error') is not False:
            return 'async_ack_is_error_unsupported'
        if not isinstance(descriptor, dict) or descriptor.get('state') not in ACK_STATES:
            return 'async_ack_state_unsupported'
        if _carries_marker(row.get('result')):
            return 'async_ack_marker_conflict'
    return None


def _notice_unsupported(tools):
    """Only one structured bash job per delivered notice packet is supported."""
    for row in tools:
        if not isinstance(row, dict) or row.get('type') != 'async_result':
            continue
        notice = async_evidence.async_notice(row.get('message'))
        jobs = notice.get('jobs') if isinstance(notice, dict) else None
        if notice is None or notice.get('invalid') or not isinstance(jobs, list) or len(jobs) != 1:
            return 'async_notice_shape_unsupported'
        if jobs[0].get('type') != 'bash':
            return 'async_notice_job_type_unsupported'
    return None


def join(tools, events, version):
    """Validate one whole-run v2 delivery and return per-call bindings.

    The result is ``{'state': 'supported', 'version': 2, 'jobs': [...]}`` where
    each job carries its validated ``tool_call_id``/``generation``/original
    ``result_sha256``/``notice_sha256`` plus derived notice text for marker
    binding only, or ``{'state': 'unsupported', 'reason': ...}``.
    """
    if type(version) is not int or version != SUPPORTED_VERSION:
        return unsupported('async_execution_protocol_unsupported', version)
    problem = _ack_unsupported(tools) or _notice_unsupported(tools)
    if problem is not None:
        return unsupported(problem, version)
    try:
        summary = async_evidence.validate_binding(events, tools, version=SUPPORTED_VERSION)
    except (RuntimeError, KeyError, TypeError, ValueError, AttributeError):
        return unsupported('async_delivery_unverified', version)
    messages = {}
    for row in tools:
        if isinstance(row, dict) and row.get('type') == 'async_result':
            messages[async_evidence.value_sha(row.get('message'))] = row.get('message')
    compact_ends = {row.get('toolCallId'): row for row in events
                    if isinstance(row, dict) and row.get('type') == 'tool_execution_end' and
                    row.get('toolName') == 'bash'}
    jobs = []
    for job in summary.get('jobs') or []:
        if (not isinstance(job, dict) or job.get('delivery') != 'delivered' or
                job.get('state') not in ACK_STATES or job.get('type') != 'bash' or
                not isinstance(job.get('generation'), int)):
            return unsupported('async_delivery_incomplete', version)
        compact = compact_ends.get(job.get('tool_call_id'))
        if not isinstance(compact, dict) or compact.get('isError') is not False:
            return unsupported('async_ack_compact_error_conflict', version)
        message = messages.get(job.get('notice_sha256'))
        notice = async_evidence.async_notice(message)
        entries = notice.get('jobs') if isinstance(notice, dict) else None
        if (notice is None or notice.get('invalid') or not isinstance(entries, list) or
                len(entries) != 1 or entries[0].get('job_id') != job.get('job_id') or
                entries[0].get('type') != 'bash'):
            return unsupported('async_notice_identity_mismatch', version)
        jobs.append({'tool_call_id': job['tool_call_id'], 'job_id': job['job_id'],
                     'generation': job['generation'], 'ack_state': job['state'],
                     'result_sha256': job['result_sha256'],
                     'notice_sha256': job['notice_sha256'],
                     'content': text_items(message.get('content'))})
    return {'state': 'supported', 'reason': None, 'version': SUPPORTED_VERSION, 'jobs': jobs}
