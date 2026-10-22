"""Bounded OMP RPC execution; vendor transport handles framing and correlation."""
import json
from pathlib import Path
import queue
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
from omp_rpc import RpcClient
from progress_monitor import ProgressMonitor
from closeout_monitor import CloseoutMonitor
from snapshot_guard import SnapshotViolation
from run_diagnostics import error_kind
from provider_evidence import request_configuration
from startup_monitor import StartupMonitor, first_output_wait
from worker_model import frozen_model, model_id, PROVIDER
from command_audit import CommandAudit, inspect_bash
from process_evidence import ToolEvidence, enabled, async_enabled, blocked_requests, live_output_enabled
from live_output import LiveOutputAudit, inspect as inspect_live_output, OBSERVATION as LIVE_FILTER
from async_evidence import AsyncTracker, bash_async, async_notice, protocol_version
from bash_gate import Gate, GateError, validate as validate_gate, audit_event


def compact_event(event):
    """Retain execution evidence without copying model reasoning into logs."""
    item = {'type': event.type}
    if event.type in ('message_start', 'message_end'):
        message = getattr(event, 'message', None)
        if isinstance(message, dict) and message.get('role') in ('assistant', 'user', 'toolResult'):
            item['message_role'] = message['role']
    for attr, key in (('tool_name', 'toolName'), ('tool_call_id', 'toolCallId'),
                      ('is_error', 'isError'), ('is_terminal', 'isTerminal')):
        if hasattr(event, attr):
            item[key] = getattr(event, attr)
    if event.type == 'tool_execution_start' and item.get('toolName') == 'bash':
        item['commandAudit'] = inspect_bash(getattr(event, 'args', None))
    if event.type == 'tool_execution_end' and item.get('toolName') == 'bash':
        item['asyncBash'] = bash_async(getattr(event, 'result', None))
    if event.type == 'message_end':
        msg = event.message
        notice = async_notice(msg)
        if notice is not None:
            item['asyncResult'] = notice
        item['message'] = {k: msg[k] for k in ('role', 'model', 'stopReason', 'errorMessage') if k in msg}
        content = msg.get('content', [])
        if isinstance(content, str):
            item['message']['content'] = [{'type': 'text', 'text': content}]
        elif content is None:
            item['message']['content'] = []
        elif isinstance(content, list):
            item['message']['content'] = [p for p in content if isinstance(p, dict) and p.get('type') == 'text']
        else:
            raise TypeError('Unsupported message content shape')
    if event.type == 'message_update':
        update = getattr(event, 'assistant_message_event', None)
        # The outer SDK event is a dataclass; the nested event is a TypedDict.
        subtype = update.get('type', '') if isinstance(update, dict) else getattr(update, 'type', '')
        kind = subtype.split('_', 1)[0]
        item['stream_kind'] = kind if kind in ('thinking', 'text', 'toolcall') else 'unknown'
        delta = update.get('delta') if isinstance(update, dict) else getattr(update, 'delta', None)
        item['delta_chars'] = len(delta) if isinstance(delta, str) else 0
    if event.type in ('auto_retry_start', 'auto_retry_end'):
        for attr in ('attempt', 'max_attempts', 'delay_ms'):
            value = getattr(event, attr, None)
            if type(value) is int and value >= 0:
                item[attr] = value
        if type(getattr(event, 'success', None)) is bool:
            item['success'] = event.success
        error = getattr(event, 'error_message', None) or getattr(event, 'final_error', None)
        if error:
            item['error_kind'] = error_kind(error)
    return item


def exception_evidence(error):
    """No exception text, source lines, locals or payloads: they may contain secrets."""
    return {'exception_type': type(error).__name__,
            'frames': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                       for f in traceback.extract_tb(error.__traceback__)[-8:]]}


def execute(run, meta, task, env, argv, dump, process_stamp, candidate_paths,
            client_factory=RpcClient, preflight=None):
    expected_model = model_id(frozen_model(task, meta))
    started = time.monotonic()
    monitor = ProgressMonitor(started, **task.get('progress', {}))
    closeout = CloseoutMonitor(started, task['execution_policy']['closeout']
        if (task.get('execution_policy') or {}).get('policy_version') == 2 else None)
    startup = StartupMonitor(started)
    command_audit = CommandAudit()
    strict_evidence = enabled(task, meta)
    strict_async = async_enabled(task, meta)
    strict_live = live_output_enabled(task, meta)
    live_output = LiveOutputAudit()
    from worker_bash import validate as validate_exact, inspect as inspect_exact, ExactBashAudit, OBSERVATION as EXACT_REJECTION
    exact_policy = validate_exact(task, meta, run)
    gate_policy = validate_gate(task, meta, run)
    gate = Gate(run, gate_policy, exact_policy) if gate_policy else None
    exact_bash = ExactBashAudit()
    async_tracker = AsyncTracker(protocol_version(task, meta))
    tool_evidence = None
    events = queue.Queue()
    client = client_factory(command=argv, env=env, startup_timeout=30, request_timeout=10,
                            max_event_history=32)
    def receive(event):
        item, observed = compact_event(event), time.monotonic()
        if gate and item.get('type') in ('tool_execution_start','tool_execution_end'):
            gate.refresh()
        lockout = gate is not None and item.get('toolCallId') in gate.lockouts
        if tool_evidence is not None:
            tool_evidence.observe(event, gate_lockout=lockout)
        if lockout and item.get('type') == 'tool_execution_start':
            item['preexecGate'] = gate.annotation(item['toolCallId'],getattr(event,'args',None),item.get('toolName'))
        if strict_live and item.get('type') == 'tool_execution_start' and item.get('toolName') == 'bash':
            item['liveOutputAudit'] = inspect_live_output(getattr(event, 'args', None))
        if exact_policy and item.get('type') == 'tool_execution_start' and item.get('toolName') == 'bash':
            item['exactBashAudit'] = inspect_exact(getattr(event, 'args', None), exact_policy)
            if gate:
                item['preexecGate'] = gate.annotation(item.get('toolCallId'), getattr(event, 'args', None))
        startup.event(item, observed)
        events.put((item, observed))
    client.on_event(receive)
    failures = queue.Queue()
    client.on_protocol_error(lambda error: failures.put(dict(reason='protocol_error', **exception_evidence(error))))
    client.on_listener_error(lambda event: failures.put(dict(reason='listener_error',
        source_type=event.source_type if event.source_type in (
            'message_start', 'message_update', 'message_end', 'tool_execution_start',
            'tool_execution_update', 'tool_execution_end', 'turn_start', 'turn_end', 'agent_end') else 'other',
        **exception_evidence(event.error))))
    if hasattr(client, 'on_unknown_notification'):
        client.on_unknown_notification(lambda event: failures.put({'reason': 'protocol_parse_error'})
                                       if event.parse_error else None)
    timeout = min(max(int(task.get('timeout_seconds', 600)), 10), 3600)
    outcome, terminal = 'failed', False
    progress = dict(monitor.snapshot(started), changed_paths=[], terminal_event=False)
    last_scan = float("-inf")
    changed = []
    reason = 'rpc_exception'
    details = []
    natural_exit_code = None
    client_started = False
    def save_progress(process_state='running'):
        nonlocal progress
        now = time.monotonic()
        timing = startup.snapshot(now)
        progress = dict(monitor.snapshot(now), changed_paths=changed,
                        worker_thinking=task['worker_thinking'], updated_at=time.time(),
                        attempt=meta.get('attempt', 1), terminal_event=terminal, startup=timing)
        progress['command_audit'] = command_audit.summary()
        if closeout.policy is not None:
            progress['closeout'] = closeout.snapshot(now)
        if strict_live:
            progress['live_output'] = live_output.summary()
        if exact_policy:
            progress['exact_bash'] = exact_bash.summary()
        if gate:
            progress['preexec_gate'] = gate.summary()
        if strict_async:
            progress['async_execution'] = async_tracker.summary()
            if progress['async_execution']['pending']:
                progress['phase'] = 'waiting_async'
        if progress['phase'] == 'starting':
            progress['phase'] = timing['stage']
        progress['first_output_wait'] = first_output_wait(timing, progress, {
            'progress': {'warn_seconds': monitor.warn_seconds, 'stall_seconds': monitor.stall_seconds,
                         'first_write_seconds': monitor.first_write_seconds},
            'timeout_seconds': timeout}, process_state, from_runtime=True)
        dump(run/'startup.json', timing)
        dump(run/'progress.json', progress)
        dump(run/'command-audit.json', command_audit.summary())
    def milestone(name):
        startup.mark(name, time.monotonic())
        save_progress()
    try:
        if strict_evidence:
            tool_evidence = ToolEvidence(run, capture_async=strict_async)
        (run / 'events.jsonl').write_text('')
        save_progress()
        if preflight is not None:
            preflight()
        milestone('rpc_start_requested')
        client.start()
        client_started = True
        milestone('rpc_ready')
        # Pinned upstream currently exposes no public process handle.
        process = client._process
        state = client.get_state()
        if (state.model is None or state.model.id != expected_model or
                state.model.provider != PROVIDER or state.thinking_level != task['worker_thinking']):
            raise RuntimeError('RPC model/thinking does not match the task')
        if meta.get('resume_session') and (not state.session_file or
                Path(state.session_file).resolve() != Path(meta['resume_session']).resolve()):
            raise RuntimeError('RPC resumed a different session; refusing to prompt')
        meta.update(process='running', pid=process.pid, process_stamp=process_stamp(process.pid),
                    started_at=time.time(), session_file=state.session_file, worker_mode='rpc',
                    worker_thinking=state.thinking_level)
        dump(run / 'metadata.json', meta)
        dump(run / 'rpc-state.json', {'model': state.model.id, 'provider': state.model.provider,
                                     'thinking': state.thinking_level, 'session_file': state.session_file,
                                     'request_configuration': request_configuration(
                                         getattr(state.model, 'api', None), getattr(state.model, 'base_url', None))})
        milestone('model_state_checked')
        if gate:
            gate.refresh()  # session_start must register before RPC ready/prompt.
        prompt = (run / 'prompt.md').read_text()
        milestone('prompt_call_started')
        client.prompt(prompt)  # ACK is not completion.
        milestone('prompt_ack_received')
        with (run / 'events.jsonl').open('w') as log:
            while True:
                tool_finished = False
                # Limit draining so a fast stream cannot starve deadlines/control.
                for _ in range(500):
                    try:
                        item, observed = events.get_nowait()
                    except queue.Empty:
                        break
                    if strict_async:
                        if item['type'] in ('agent_start', 'turn_start') or 'asyncResult' in item:
                            terminal = False
                            monitor.completed = False
                        async_tracker.event(item)
                        if 'asyncResult' in item and not async_tracker.errors:
                            monitor.last_tool_progress = observed
                    audited = audit_event(item, gate)
                    monitor.event(audited, observed) if audited else None
                    command_audit.event(audited)
                    if exact_policy:
                        exact_bash.event(audited)
                    if strict_live:
                        live_output.event(audited)
                    if item['type'] == 'tool_execution_end':
                        tool_finished = True
                    log.write(json.dumps(dict(item, elapsed_seconds=observed-started), ensure_ascii=False) + '\n')
                    if item['type'] == 'agent_end' and item.get('isTerminal') is not False:
                        terminal = True
                log.flush()
                now = time.monotonic()
                if gate:
                    gate.refresh()
                if terminal or tool_finished or now - last_scan >= 1:
                    changed = candidate_paths()
                    closeout.observe(changed, now)
                    if changed and monitor.first_write_at is None:
                        monitor.first_write_at = now  # bash can also create/edit a candidate.
                    last_scan = now
                save_progress()
                if (run / 'cancel-requested').exists():
                    outcome = 'cancelled'
                    reason = 'cancel_requested'
                    break
                while len(details) < 16:
                    try:
                        details.append(failures.get_nowait())
                    except queue.Empty:
                        break
                if details or client.protocol_errors or client.listener_errors:
                    outcome = 'failed'
                    reason = details[0]['reason'] if details else 'rpc_client_error'
                    break
                if gate and gate.denials >= 3:
                    outcome, reason = 'failed', 'preexec_bash_denial_limit'
                    break
                blocked = blocked_requests(command_audit.summary())
                if strict_live and live_output.filters:
                    blocked.append(LIVE_FILTER)
                if strict_evidence and blocked:
                    outcome = 'failed'
                    reason = 'process_constraint_request'
                    details.append({'reason': reason, 'observations': blocked})
                    break
                if exact_policy and exact_bash.rejected:
                    outcome, reason = 'failed', EXACT_REJECTION
                    details.append({'reason': reason, 'rejected_requests': exact_bash.rejected})
                    break
                async_state = async_tracker.summary() if strict_async else {'pending': [], 'errors': []}
                if async_state['errors']:
                    outcome, reason = 'failed', 'async_evidence_invalid'
                    details.append({'reason': reason, 'errors': async_state['errors']})
                    break
                if terminal and not async_state['pending']:
                    if gate:
                        from process_evidence import binding
                        binding(run, async_required=strict_async, live_output_required=strict_live,
                                exact_bash_policy=exact_policy)
                    outcome = 'exited'
                    reason = 'terminal_agent_end'
                    break
                if now - started >= timeout:
                    outcome = 'timed_out'
                    reason = 'async_results_pending' if async_state['pending'] else 'total_deadline'
                    break
                closed = getattr(client, '_closed_error', None)
                if closed is not None:
                    outcome = 'failed'
                    reason = 'transport_closed'
                    details.append(dict(reason=reason, **exception_evidence(closed)))
                    break
                if process.poll() is not None:
                    outcome = 'failed'
                    reason = 'process_exit_without_agent_end'
                    break
                # The window includes active/async tools. Do not steer an active
                # command into OMP's background path merely to give a reminder.
                closing = closeout.check(now, can_warn=not monitor.snapshot(now)['active_tools']
                                         and not async_state['pending'])
                if closing == 'deadline':
                    outcome, reason = 'timed_out', 'candidate_closeout_deadline'
                    break
                if closing == 'warn':
                    state = closeout.snapshot(now)
                    remaining = max(0, int(round(state['policy']['deadline_seconds']
                                                 - state['candidate_age_seconds'])))
                    client.steer('Closeout warning: about %d seconds remain in the frozen candidate budget '
                                 '(descriptive only; it neither resets nor extends the window). Stop expanding '
                                 'the change and do not rerun already-current passing tests or polish harmless '
                                 'warnings. Once the current exact tests and their complete results have been '
                                 'inspected, finish the compact COMPLETE ledger immediately after the Status line; '
                                 'report real results or NOT VERIFIED with the blocker. Do not replace tests with '
                                 'probes. Further edits and this steering ACK do not extend the deadline.'
                                 % remaining)
                    dump(run/'closeout-warning.json', {'at': time.time(), 'rpc_ack': True,
                        'action': 'steer', 'closeout': state, 'remaining_seconds': remaining,
                        'note': 'Queued only; not proof of delivery, tests or handoff'})
                action = None if async_state['pending'] else monitor.check(now)
                if action == 'stall':
                    outcome = 'stalled'
                    reason = ('tool_idle' if now - monitor.last_tool_progress >= monitor.stall_seconds
                              else 'first_candidate_deadline')
                    break
                if action == 'warn':
                    marks = progress['startup']['milestones']
                    if marks['first_model_delta'] is None and marks['first_tool_event'] is None:
                        dump(run/'watchdog-warning.json', {'at': time.time(), 'action': 'observe',
                             'kind': 'model_output_not_observed', 'startup': progress['startup'],
                             'first_output_wait': progress['first_output_wait'],
                             'note': 'RPC acceptance is not provider HTTP progress; no automatic steering sent'})
                    else:
                        client.steer('Use the fixed task contract. Implement the smallest valid candidate now; '
                                     'if blocked by missing context, state the blocker and finish. Do not expand scope.')
                        dump(run / 'watchdog-warning.json', {'at': time.time(), 'rpc_ack': True,
                                                           'action': 'steer',
                                                           'note': 'Queued steering, not proof of delivery or completion'})
                inbox = run / 'controls'
                for path in sorted(inbox.glob('*.json'))[:1] if inbox.exists() else []:
                    request = json.loads(path.read_text())
                    client.steer(request['message'])
                    dump(path.with_suffix('.ack'), {'rpc_ack': True, 'at': time.time()})
                    path.unlink()
                time.sleep(0.2)
        natural_exit_code = process.poll()
        return outcome, 0 if outcome == 'exited' else 1
    except GateError:
        outcome, reason = 'failed', 'preexec_gate_invalid'
        return outcome, 1
    except SnapshotViolation as error:
        outcome, reason = 'failed', error.reason
        details.append(error.evidence())
        return outcome, 1
    except Exception as error:
        reason = 'rpc_exception'
        details.append(dict(reason=reason, **exception_evidence(error)))
        raise
    finally:
        termination = {'process': outcome, 'reason': reason, 'elapsed_seconds': time.monotonic()-started,
                       'terminal_event': terminal, 'natural_exit_code': natural_exit_code, 'details': details}
        # Persist the original cause before cleanup; cleanup must not replace it.
        try:
            dump(run / 'termination.json', termination)
            if outcome != 'exited':
                dump(run / 'failure.json', termination)
            save_progress(process_state=outcome)
        finally:
            try:
                if outcome != 'exited' and client_started:
                    try:
                        client.abort()
                    except Exception:
                        pass  # stop() still tears down the OMP process group.
                client.stop()
            finally:
                if tool_evidence is not None:
                    tool_evidence.close()
                if gate and outcome != 'exited':
                    try:
                        dump(run/'gate-lockout-evidence.json',gate.failure_evidence())
                    except Exception:
                        dump(run/'gate-lockout-evidence.json',{'acceptance':'failed_attempt_not_eligible',
                             'correlation':'unavailable_or_invalid','reason':'gate_evidence_incomplete'})
                (run / 'stderr.log').write_text(client.stderr)
                if progress:
                    progress.update(process=outcome, stop_reason=reason, updated_at=time.time())
                    dump(run / 'progress.json', progress)
