"""Local RPC milestones, not provider HTTP telemetry or token measurements."""
import json
import math
from pathlib import Path
import threading
from worker_model import diagnostic_model_id, PROVIDER


MILESTONES = ('supervisor_entered', 'rpc_start_requested', 'rpc_ready',
              'model_state_checked', 'prompt_call_started', 'prompt_ack_received',
              'first_rpc_event', 'first_assistant_event', 'first_model_delta', 'first_tool_event')


def stage(marks):
    for key, label in (
        ('first_model_delta', 'model_output_observed'),
        ('first_tool_event', 'tool_observed'),
        ('first_assistant_event', 'assistant_event_observed'),
        ('prompt_ack_received', 'awaiting_model_output'),
        ('prompt_call_started', 'awaiting_prompt_ack'),
        ('model_state_checked', 'prompt_not_called'),
        ('rpc_ready', 'checking_model_state'),
        ('rpc_start_requested', 'rpc_starting'),
    ):
        if marks.get(key) is not None:
            return label
    return 'snapshot_preflight'


def seconds(value):
    return value if type(value) in (int, float) and 0 <= value <= 86400 and math.isfinite(value) else None


def interval(value):
    if type(value) not in (int, float) or value <= 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def first_output_wait(startup, progress, policy, process, *, from_runtime=False):
    """Describe saved first-output waiting; never change timers or authorize recovery."""
    result = {'version': 1, 'state': 'unknown', 'observation': 'insufficient_startup_evidence',
              'wait_since_ack_seconds': None, 'stop_at_elapsed_seconds': None,
              'remaining_seconds': None, 'budget_basis': None, 'next_step': 'inspect_saved_evidence',
              'automatic_retry_authorized': False, 'provider_http_request_observed': None}
    marks = startup.get('milestones', {})
    complete = (isinstance(marks, dict) and all(k in marks and
                (marks[k] is None or seconds(marks[k]) is not None) for k in MILESTONES))
    if not complete or startup.get('milestones_complete') is False:
        return result
    if marks['first_model_delta'] is not None or marks['first_tool_event'] is not None:
        result.update(state='output_observed', observation='model_delta_or_tool_observed',
                      next_step='follow_progress_and_failure_evidence')
        return result
    # Old reconstruction cannot establish prompt acceptance or a fresh wait clock.
    ack = marks['prompt_ack_received']
    elapsed = seconds(progress.get('elapsed_seconds'))
    if ack is None or elapsed is None or any(v is not None and v > elapsed for v in marks.values()):
        return result
    stopped = process in ('exited', 'failed', 'stalled', 'cancelled', 'timed_out')
    if process != 'running' and not stopped:
        return result
    result.update(state='ended_without_output' if stopped else 'waiting',
                  observation=('prompt_accepted_no_assistant_event' if marks['first_assistant_event'] is None
                               else 'assistant_event_without_model_delta_or_tool'),
                  wait_since_ack_seconds=max(0, elapsed-ack),
                  next_step='verify_exit_then_inspect_or_take_over' if stopped else 'inspect_existing_budget')
    # Only predict the unchanged no-tool/no-candidate bound. A resumed candidate
    # can have a different closeout deadline; missing policy/progress stays unknown.
    if (progress.get('changed_paths') != [] or 'first_write_at' not in progress
            or progress['first_write_at'] is not None
            or progress.get('active_tools') != [] or progress.get('tool_counts') != {}
            or not isinstance(policy, dict)):
        return result
    if not from_runtime and (type(policy.get('policy_version')) is not int
                             or policy['policy_version'] not in (1, 2)):
        return result
    intervals = policy.get('progress', {})
    if not isinstance(intervals, dict):
        return result
    total = policy.get('timeout_seconds')
    if type(total) is not int or not 10 <= total <= 3600:
        return result
    limits = [interval(intervals.get('stall_seconds')), interval(intervals.get('first_write_seconds')), total]
    warn = interval(intervals.get('warn_seconds'))
    if (any(v is None for v in limits) or warn is None
            or warn >= limits[0]):
        return result
    bound = min(limits)
    result['stop_at_elapsed_seconds'] = bound
    result['budget_basis'] = 'live_watchdog_limits' if from_runtime else 'saved_execution_policy'
    if not stopped:
        remaining = max(0, bound-elapsed)
        result['remaining_seconds'] = remaining
        result['next_step'] = ('expect_stop_then_verify_exit' if remaining == 0 else
                               'inspect_or_cancel_without_automatic_retry' if elapsed >= warn
                               else 'wait_within_existing_budget')
    return result


class StartupMonitor:
    def __init__(self, started):
        self.started = started
        self.marks = {'supervisor_entered': 0.0}
        self.lock = threading.Lock()

    def mark(self, name, now):
        if name not in MILESTONES:
            raise ValueError('Unknown startup milestone')
        with self.lock:
            self.marks.setdefault(name, max(0.0, now - self.started))

    def event(self, item, now):
        self.mark('first_rpc_event', now)
        typ = item.get('type')
        message = item.get('message', {})
        role = item.get('message_role') or (message.get('role') if isinstance(message, dict) else None)
        if typ == 'message_update' or (typ in ('message_start', 'message_end') and role == 'assistant'):
            self.mark('first_assistant_event', now)
        if (typ == 'message_update' and item.get('stream_kind') in ('thinking', 'text', 'toolcall')
                and isinstance(item.get('delta_chars'), int) and item['delta_chars'] > 0):
            self.mark('first_model_delta', now)
        if typ == 'tool_execution_start':
            self.mark('first_tool_event', now)

    def snapshot(self, now):
        with self.lock:
            marks = {k: self.marks.get(k) for k in MILESTONES}
        return {'version': 1, 'stage': stage(marks), 'elapsed_seconds': max(0.0, now-self.started),
                'milestones': marks, 'provider_http_request_observed': None}


def read_startup(run, meta=None):
    """Allowlisted readback, with explicitly limited reconstruction for old runs."""
    root = Path(run)
    try:
        saved = json.loads((root/'startup.json').read_text())
    except FileNotFoundError:
        saved = None
    except (OSError, ValueError):
        return {'source': 'unreadable', 'provider_http_request_observed': None}
    if saved is not None:
        if not isinstance(saved, dict) or saved.get('version') != 1 or not isinstance(saved.get('milestones'), dict):
            return {'source': 'unreadable', 'provider_http_request_observed': None}
        marks = {}
        for key in MILESTONES:
            value = saved['milestones'].get(key)
            marks[key] = value if type(value) in (int, float) and 0 <= value <= 86400 and math.isfinite(value) else None
        complete = all(k in saved['milestones'] and (saved['milestones'][k] is None or
                       seconds(saved['milestones'][k]) is not None) for k in MILESTONES)
        return {'source': 'recorded', 'stage': stage(marks), 'milestones': marks,
                'milestones_complete': complete,
                'provider_http_request_observed': None}
    # Old rpc-state proves get_state returned, but carries no prompt-ACK timestamp.
    ready = False
    try:
        state = json.loads((root/'rpc-state.json').read_text())
        expected_model = diagnostic_model_id(meta or {})
        ready = (expected_model is not None and isinstance(state, dict) and
                 state.get('model') == expected_model and state.get('provider') == PROVIDER)
    except (OSError, ValueError):
        pass
    monitor = StartupMonitor(0)
    scan = 'complete'
    try:
        with (root/'events.jsonl').open('rb') as stream:
            total = 0
            for _ in range(100000):
                line = stream.readline(4 * 1024 * 1024 + 1)
                if not line:
                    break
                total += len(line)
                if total > 16 * 1024 * 1024 or not line.endswith(b'\n'):
                    scan = 'truncated'; break
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    scan = 'invalid_records'; continue
                if not isinstance(item, dict):
                    scan = 'invalid_records'; continue
                elapsed = item.get('elapsed_seconds')
                if type(elapsed) not in (int, float) or not 0 <= elapsed <= 86400:
                    scan = 'invalid_records'; continue
                monitor.event(item, elapsed)
            else:
                scan = 'truncated'
    except FileNotFoundError:
        scan = 'missing'
    except OSError:
        scan = 'unreadable'
    marks = monitor.snapshot(0)['milestones']
    marks['supervisor_entered'] = None  # Never invent a measured timestamp for old evidence.
    return {'source': 'legacy_events', 'rpc_ready_evidence': ready,
            'prompt_ack_observed': None, 'events_scan': scan,
            'milestones': marks, 'provider_http_request_observed': None,
            'observation': ('rpc_ready_without_assistant_event' if ready and scan == 'complete'
                            and marks['first_assistant_event'] is None and marks['first_tool_event'] is None
                            else 'partial_legacy_observation')}
