"""Opt-in exact Bash requests. Observations are not execution interception."""
import hashlib
from pathlib import Path
import shlex

from command_audit import MAX_CHARS
from worker_commands import logged_worker_test_command

KEY = 'worker_bash_policy'
MODE = 'declared_tests_only'
OBSERVATION = 'undeclared_bash_request'
STATUSES = ('allowed', 'undeclared_command', 'invalid_arguments', 'unavailable')


def mode(task):
    value = task.get('worker_bash_mode', 'standard')
    if value not in ('standard', MODE):
        raise ValueError('worker_bash_mode requires standard or declared_tests_only')
    return value


def commands(task, run):
    argv = [['env', 'PYTHONDONTWRITEBYTECODE=1'] + entry for entry in task['test_commands']]
    log_root = Path(run) / 'worker-test-logs'
    return argv, [shlex.join(entry) for entry in argv], [logged_worker_test_command(entry, log_root) for entry in argv]


def command_sha(command):
    return hashlib.sha256(command.encode('utf-8')).hexdigest()


def resolve(task, run, *, version=2):
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported worker Bash policy version')
    selected = mode(task)
    hashes = []
    if selected == MODE:
        from process_evidence import validate_test_argv
        tests = task.get('test_commands')
        if not isinstance(tests, list) or not tests:
            raise ValueError('Strict Bash mode requires declared tests')
        for argv in tests:
            validate_test_argv(argv)
        _, direct, logged = commands(task, run)
        if any(len(value) > MAX_CHARS for value in direct + logged):
            raise ValueError('Prepared Bash command exceeds exact-match limit')
        hashes = sorted(set(command_sha(value.strip()) for value in direct + logged))
    policy = {'version': version, 'mode': selected, 'allowed_command_sha256': hashes}
    if version == 2:
        # Literal match only. No runtime filesystem lookup or alias expansion.
        policy['snapshot_cwd'] = str(Path(run) / 'snapshot') if selected == MODE else None
    return policy


def validate(task, meta, run):
    """Frozen copies and regenerated argv/path hashes must agree; no legacy retrofit."""
    from bash_gate import validate as validate_gate
    if KEY not in task and KEY not in meta:
        validate_gate(task, meta, run)
        if 'worker_bash_mode' in task:
            raise ValueError('Unfrozen worker Bash mode; prepare a fresh run')
        return None
    saved = task.get(KEY)
    if not isinstance(saved, dict):
        raise ValueError('Worker Bash policy changed; prepare a fresh run')
    expected = resolve(task, run, version=saved.get('version'))
    if any(not isinstance(value, dict) or type(value.get('version')) is not int or value != expected
           for value in (task.get(KEY), meta.get(KEY))):
        raise ValueError('Worker Bash policy changed; prepare a fresh run')
    validate_gate(task, meta, run)
    if expected['mode'] == MODE:
        from process_evidence import live_output_enabled
        if not live_output_enabled(task, meta):
            raise ValueError('Strict Bash mode requires process evidence v3')
        return expected
    return None


def inspect(args, policy):
    version = policy['version']
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported worker Bash policy version')
    result = {'version': version, 'status': 'unavailable', 'command_sha256': None}
    if version == 2:
        result.update(command_match=None, cwd_status='unavailable', argument_issues=[])
    command = args.get('command') if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_CHARS:
        return result
    result['command_sha256'] = command_sha(command.strip())
    # v1 remains byte-for-byte compatible for frozen evidence reinspection.
    timeout = args.get('timeout')
    if version == 2:
        result['command_match'] = result['command_sha256'] in policy['allowed_command_sha256']
        issues = result['argument_issues']
        if set(args) - {'command', 'timeout', 'cwd'}:
            issues.append('unsupported_arguments')
        if 'timeout' in args and (type(timeout) not in (int, float) or not 0 < timeout <= 3600):
            issues.append('invalid_timeout')
        if 'cwd' not in args:
            result['cwd_status'] = 'omitted'
        elif not isinstance(args['cwd'], str) or not args['cwd'] or '\x00' in args['cwd']:
            result['cwd_status'] = 'invalid'
            issues.append('invalid_cwd')
        elif args['cwd'] == policy.get('snapshot_cwd'):
            result['cwd_status'] = 'snapshot'
        else:
            result['cwd_status'] = 'different'
            issues.append('cwd_not_snapshot')
        result['status'] = ('invalid_arguments' if issues else
                            'allowed' if result['command_match'] else 'undeclared_command')
        return result
    if (set(args) - {'command', 'timeout'} or 'timeout' in args and
            (type(timeout) not in (int, float) or not 0 < timeout <= 3600)):
        result['status'] = 'invalid_arguments'
    else:
        result['status'] = 'allowed' if result['command_sha256'] in policy['allowed_command_sha256'] else 'undeclared_command'
    return result


class ExactBashAudit:
    def __init__(self):
        self.counts = {key: 0 for key in STATUSES}
        self.command_matches = {'matched': 0, 'not_matched': 0, 'unavailable': 0}
        self.match_schema_seen = False

    def event(self, item):
        if item.get('type') != 'tool_execution_start' or item.get('toolName') != 'bash':
            return
        value = item.get('exactBashAudit')
        match = None
        if isinstance(value, dict) and type(value.get('version')) is int and value['version'] == 2:
            self.match_schema_seen = True
            match = value.get('command_match')
        self.command_matches['matched' if match is True else 'not_matched' if match is False else 'unavailable'] += 1
        status = value.get('status') if isinstance(value, dict) and type(value.get('version')) is int and value['version'] in (1, 2) else None
        self.counts[status if status in STATUSES else 'unavailable'] += 1

    @property
    def rejected(self):
        return sum(value for key, value in self.counts.items() if key != 'allowed')

    def summary(self):
        result = {'version': 1, 'bash_calls': sum(self.counts.values()), 'status_counts': dict(self.counts),
                  'rejected_requests': self.rejected, 'scope': 'observed_requests_not_execution_or_damage'}
        if self.match_schema_seen:
            result['command_match_counts'] = dict(self.command_matches)
        return result
