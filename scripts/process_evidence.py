"""Versioned acceptance evidence; observed requests are not pre-execution interception."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading

from command_audit import CommandAudit, inspect_bash
from worker_bash import ExactBashAudit, inspect as inspect_exact, validate as validate_exact, OBSERVATION as EXACT_REJECTION

POLICY_V1 = {'version': 1, 'bash_parameters': 'required', 'process_review': 'required', 'test_logs': 'bound'}
POLICY_V2 = dict(POLICY_V1, version=2, async_results='bound', async_lifecycle='reviewed')
POLICY = dict(POLICY_V2, version=3, live_python_filters='stop_after_observation')
POLICY_GATE = dict(POLICY, version=4, preexec_bash='bound_with_two_corrections')
MAX_RECORD = 1024 * 1024
MAX_JOURNAL = 16 * 1024 * 1024
BLOCKED = ('requested_recursive_rm', 'test_output_pipeline')


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as source:
        for part in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(part)
    return hasher.hexdigest()


def enabled(task, meta):
    key = 'process_evidence_policy'
    if key not in task and key not in meta:
        return False
    if (task.get(key) != meta.get(key) or
            any(not isinstance(value, dict) or value not in (POLICY_V1, POLICY_V2, POLICY, POLICY_GATE) or type(value.get('version')) is not int
                for value in (task.get(key), meta.get(key)))):
        raise ValueError('Process evidence policy changed; prepare a fresh run')
    return True


def async_enabled(task, meta):
    active = enabled(task, meta)
    from async_evidence import protocol_version
    protocol_version(task, meta)
    return active and task['process_evidence_policy']['version'] >= 2


def live_output_enabled(task, meta):
    return enabled(task, meta) and task['process_evidence_policy']['version'] >= 3


def blocked_requests(summary):
    return [name for name in BLOCKED if summary['observation_counts'].get(name, 0)]


class ToolEvidence:
    """A private, bounded journal of Bash arguments/results, separate from compact logs."""
    def __init__(self, run, capture_async=False):
        path = Path(run) / 'tool-evidence.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        self.stream = os.fdopen(fd, 'wb')
        self.lock = threading.Lock()
        self.size = 0
        self.capture_async = capture_async

    def observe(self, event, *, gate_lockout=False):
        from async_evidence import async_notice
        message = getattr(event, 'message', None)
        if self.capture_async and event.type == 'message_end' and async_notice(message) is not None:
            record = {'version': 1, 'type': 'async_result', 'message': message}
        else:
            if (not gate_lockout and getattr(event, 'tool_name', None) != 'bash') or event.type not in ('tool_execution_start', 'tool_execution_end'):
                return
            record = {'version': 1, 'type': event.type, 'id': getattr(event, 'tool_call_id', None)}
            if event.type == 'tool_execution_start':
                record['args'] = getattr(event, 'args', None)
            else:
                record.update(is_error=getattr(event, 'is_error', None), result=getattr(event, 'result', None))
            if gate_lockout:
                record.update(type='gate_lockout_start' if event.type=='tool_execution_start' else 'gate_lockout_end',
                              tool_name=getattr(event,'tool_name',None))
        payload = encoded(record) + b'\n'
        with self.lock:
            if len(payload) > MAX_RECORD or self.size + len(payload) > MAX_JOURNAL:
                # Never silently truncate into apparently complete evidence.
                raise RuntimeError('Tool evidence journal limit exceeded')
            self.stream.write(payload)
            self.stream.flush()
            self.size += len(payload)

    def close(self):
        with self.lock:
            self.stream.close()


def records(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JOURNAL:
        raise RuntimeError('Missing, linked or oversized process evidence: ' + path.name)
    raw = path.read_bytes()
    if raw and not raw.endswith(b'\n'):
        raise RuntimeError('Incomplete process evidence: ' + path.name)
    result = []
    for line in raw.splitlines():
        if len(line) > MAX_RECORD:
            raise RuntimeError('Oversized process evidence record')
        item = json.loads(line)
        if not isinstance(item, dict):
            raise RuntimeError('Invalid process evidence record')
        result.append(item)
    return raw, result


def observed_blocked_requests(run, *, live_output_required=False, exact_bash_policy=None):
    """Check available start evidence before resume, without requiring completion.

    Cancellation may win the stop-reason race, and a callback can persist private
    arguments before the main loop consumes its compact event. Check both sources.
    Absent startup journals are allowed; malformed existing journals fail closed.
    """
    run = Path(run)
    audit = CommandAudit()
    exact_bash = ExactBashAudit()
    from live_output import LiveOutputAudit, inspect, OBSERVATION
    live = LiveOutputAudit()
    for name in ('events.jsonl', 'tool-evidence.jsonl'):
        path = run / name
        if not path.exists() and not path.is_symlink():
            continue
        for item in records(path)[1]:
            if name == 'events.jsonl':
                audit.event(item)
                if exact_bash_policy:
                    exact_bash.event(item)
                if live_output_required:
                    live.event(item)
            elif item.get('type') == 'tool_execution_start':
                if exact_bash_policy:
                    exact_bash.event({'type':'tool_execution_start','toolName':'bash',
                                      'exactBashAudit':inspect_exact(item.get('args'), exact_bash_policy)})
                audit.event({'type': 'tool_execution_start', 'toolName': 'bash',
                             'commandAudit': inspect_bash(item.get('args'))})
                if live_output_required:
                    live.event({'type':'tool_execution_start','toolName':'bash',
                                'liveOutputAudit':inspect(item.get('args'))})
    return blocked_requests(audit.summary()) + ([OBSERVATION] if live.filters else []) + ([EXACT_REJECTION] if exact_bash_policy and exact_bash.rejected else [])


def binding(run, *, async_required=False, live_output_required=False, exact_bash_policy=None):
    """Fail closed on missing/inconsistent parameters or completions; no semantic PASS."""
    run = Path(run)
    event_bytes, events = records(run / 'events.jsonl')
    tool_bytes, tools = records(run / 'tool-evidence.jsonl')
    from bash_gate import for_run, audit_event
    gate = for_run(run)
    if gate:
        gate.refresh(complete=True)
    starts, ends, tool_starts, tool_ends = {}, {}, {}, {}
    audit = CommandAudit()
    exact_bash = ExactBashAudit()
    from live_output import LiveOutputAudit, inspect, OBSERVATION
    live = LiveOutputAudit()
    terminal = False
    for event in events:
        audited = audit_event(event, gate)
        if gate is None and 'preexecGate' in event:
            raise RuntimeError('Unbound pre-execution annotation')
        audit.event(audited)
        if exact_bash_policy:
            exact_bash.event(audited)
        if live_output_required:
            live.event(audited)
        if async_required and (event.get('type') in ('agent_start', 'turn_start') or 'asyncResult' in event):
            terminal = False
        if event.get('type') == 'agent_end' and event.get('isTerminal') is not False:
            terminal = True
        if event.get('toolName') == 'bash' and event.get('type') in ('tool_execution_start', 'tool_execution_end'):
            call_id = event.get('toolCallId')
            target = starts if event['type'] == 'tool_execution_start' else ends
            if not isinstance(call_id, str) or not call_id or call_id in target:
                raise RuntimeError('Missing/duplicate Bash event identifier')
            if terminal or (target is ends and call_id not in starts):
                raise RuntimeError('Bash event order is inconsistent')
            target[call_id] = event
    for item in tools:
        if async_required and item.get('version') == 1 and item.get('type') == 'async_result':
            continue
        if gate and item.get('type') in ('gate_lockout_start','gate_lockout_end'):
            if item.get('id') not in gate.lockouts:
                raise RuntimeError('Unbound tool lockout evidence')
            continue  # An exhausted gate is rejected by gate.binding below.
        if item.get('version') != 1 or item.get('type') not in ('tool_execution_start', 'tool_execution_end'):
            raise RuntimeError('Invalid tool evidence envelope')
        call_id = item.get('id')
        target = tool_starts if item['type'] == 'tool_execution_start' else tool_ends
        if not isinstance(call_id, str) or not call_id or call_id in target:
            raise RuntimeError('Missing/duplicate tool evidence identifier')
        if target is tool_ends and call_id not in tool_starts:
            raise RuntimeError('Tool evidence order is inconsistent')
        target[call_id] = item
    if not terminal or not (set(starts) == set(ends) == set(tool_starts) == set(tool_ends)):
        raise RuntimeError('Incomplete Bash request/result coverage or terminal evidence')
    gate_binding = gate.binding(starts, ends, tool_starts, tool_ends) if gate else None
    for call_id, start in starts.items():
        args = tool_starts[call_id].get('args')
        if not isinstance(args, dict) or not isinstance(args.get('command'), str) or not args['command'].strip():
            raise RuntimeError('Bash command parameters unavailable')
        recorded_audit = start.get('commandAudit')
        if not isinstance(recorded_audit, dict):
            raise RuntimeError('Bash parameters do not match compact audit')
        try:
            observed = inspect_bash(args, version=recorded_audit.get('version'))
        except ValueError as error:
            raise RuntimeError('Unsupported recorded command audit version') from error
        if observed != start.get('commandAudit'):
            raise RuntimeError('Bash parameters do not match compact audit')
        if live_output_required and inspect(args) != start.get('liveOutputAudit'):
            raise RuntimeError('Live output parameters do not match compact audit')
        if exact_bash_policy and inspect_exact(args, exact_bash_policy) != start.get('exactBashAudit'):
            raise RuntimeError('Exact Bash parameters do not match compact audit')
        end = tool_ends[call_id]
        if (type(end.get('is_error')) is not bool or end.get('is_error') != ends[call_id].get('isError') or
                not isinstance(end.get('result'), dict)):
            raise RuntimeError('Bash completion/result evidence unavailable or inconsistent')
    summary = audit.summary()
    result = {'version': 1, 'events_sha256': sha(event_bytes), 'tool_evidence_sha256': sha(tool_bytes),
            'audit_sha256': sha(encoded(summary)), 'bash_calls': len(starts),
            'test_report_sha256': file_sha(run / 'tests.json') if (run / 'tests.json').is_file() else None,
            'blocked_requests': blocked_requests(summary), 'audit': summary}
    if live_output_required:
        result['live_output'] = live.summary()
        if live.filters:
            result['blocked_requests'].append(OBSERVATION)
    if exact_bash_policy:
        result['exact_bash'] = dict(exact_bash.summary(), policy_sha256=sha(encoded(exact_bash_policy)))
        if exact_bash.rejected:
            result['blocked_requests'].append(EXACT_REJECTION)
    if async_required:
        from async_evidence import validate_binding, version_for_run
        result['async_execution'] = validate_binding(events, tools, version=version_for_run(run))
    if gate_binding is not None:
        result['preexec_gate'] = gate_binding
    return result


def binding_difference(expected, supplied):
    """Describe schema paths only; never disclose supplied keys, values or payloads."""
    if not isinstance(supplied, dict):
        return 'binding must be a complete object'
    missing, changed = [], []
    unexpected = 0

    def compare(wanted, received, prefix=''):
        nonlocal unexpected
        unexpected += len(received.keys() - wanted.keys())
        for key, value in wanted.items():
            path = prefix + key
            if key not in received:
                missing.append(path)
            elif isinstance(value, dict) and isinstance(received[key], dict):
                compare(value, received[key], path + '.')
            elif received[key] != value:
                changed.append(path)

    compare(expected, supplied)
    details = []
    for label, paths in (('missing fields', missing), ('changed fields', changed)):
        if paths:
            details.append(label + ': ' + ', '.join(paths[:20]) + (' (more omitted)' if len(paths) > 20 else ''))
    if unexpected:
        details.append('unexpected field count: ' + str(unexpected))
    return '; '.join(details)


def validate_review(run, task, meta, review):
    exact_policy = validate_exact(task, meta, run)
    if not enabled(task, meta):
        return
    actual = binding(run, async_required=async_enabled(task, meta),
                     live_output_required=live_output_enabled(task, meta), exact_bash_policy=exact_policy)
    if actual['blocked_requests']:
        raise RuntimeError('Forbidden request evidence cannot be finalized as compliant')
    assessment = review.get('process_review')
    if (review.get('model') != 'gpt-5.6-sol' or review.get('effort') != 'max' or
            not isinstance(assessment, dict) or assessment.get('verdict') != 'pass'):
        raise RuntimeError('Require Sol max process review bound to complete current evidence')
    if assessment.get('binding') != actual:
        raise RuntimeError('Require Sol max process review bound to complete current evidence; ' +
                           binding_difference(actual, assessment.get('binding')))
    required = ['reason']
    if actual.get('preexec_gate', {}).get('denied_requests'):
        required.append('preexec_denial_resolution')
    audit = actual['audit']
    if audit['status_counts']['unsupported_syntax'] or audit['status_counts']['over_limit']:
        required.append('coverage_resolution')
    if audit['text_observation_counts']['absolute_tmp_path_mention']:
        required.append('temporary_path_resolution')
    if any(not isinstance(assessment.get(k), str) or not assessment[k].strip() for k in required):
        raise RuntimeError('Unresolved process evidence requires an explicit Sol disposition')
    if async_enabled(task, meta):
        from async_evidence import validate_lifecycle
        validate_lifecycle(run, actual['async_execution'], assessment)


def validate_test_argv(argv):
    """Require direct argv for automatic test receipts, not shell/SSH exit aggregation."""
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and '\x00' not in x for x in argv):
        raise ValueError('Tests require direct argument arrays')
    index = 0
    while PurePosixPath(argv[index]).name == 'env':
        index += 1
        while index < len(argv) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', argv[index]):
            index += 1
        if index == len(argv) or argv[index].startswith('-'):
            raise ValueError('Unsupported test environment wrapper')
    if PurePosixPath(argv[index]).name in ('sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'csh', 'tcsh', 'ssh'):
        raise ValueError('Automatic test evidence requires direct argv; use the SSH target transport, not shell/SSH test wrappers')


def validate_test_logs(run, task, report):
    tests = report.get('tests')
    if (report.get('evidence_version') != 1 or not isinstance(tests, list) or
            len(tests) != len(task['test_commands'])):
        raise RuntimeError('Missing complete target-test evidence')
    for index, (result, argv) in enumerate(zip(tests, task['test_commands'])):
        validate_test_argv(argv)
        if (result.get('argv') != argv or type(result.get('exit_code')) is not int or result['exit_code'] != 0 or
                result.get('transport_complete') is not True or result.get('exit_scope') != 'declared_test_process'):
            raise RuntimeError('Target test exit or transport is unverified')
        for stream in ('stdout', 'stderr'):
            name = 'test-%d.%s' % (index, stream)
            path = Path(run) / name
            if (result.get(stream) != name or path.is_symlink() or not path.is_file() or
                    result.get(stream + '_sha256') != file_sha(path)):
                raise RuntimeError('Target-test log missing or changed: ' + name)
