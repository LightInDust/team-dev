#!/usr/bin/env python3
"""Local OMP RPC worker plus version-checked Git/SSH snapshot workflow."""
import argparse
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from worker_commands import logged_worker_test_command
from run_diagnostics import usable_final_text
import target_test_receipts
from worker_bash import validate as validate_bash_policy, resolve as resolve_bash_policy, commands as bash_commands
import worker_guidance

from worker_model import DEFAULT_MODEL, frozen_model, model_id

MODEL = DEFAULT_MODEL
CONFIG = Path.home() / '.config/codex-team-dev'
RUNS = Path.home() / '.local/state/codex-team-dev/runs'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o',
       'ClearAllForwardings=yes']

# Human-facing report format for every new worker prompt. These words are
# communication only; they never replace the acceptance ledger or set any
# machine/execution state.
WORKER_COMMUNICATION = (
    'WORKER COMMUNICATION REPORT: Begin the final report with exactly one status line: '
    'Status DONE, Status DONE_WITH_CONCERNS, Status NEEDS_CONTEXT or Status BLOCKED. '
    'These status words are human communication only; they are never execution or acceptance '
    'state and never replace required ledger coverage. Fill worker_handoff_template as evidence '
    'arrives: one shared evidence block followed by the compact COMPLETE acceptance ledger and '
    'the pending-job, scratch and deviation slots. Return this filled structure once, after '
    'inspecting actual checks and complete results. Only then add a short narrative if material '
    'information is still absent from those blocks. Reference T1/T2 from the prepared invocation '
    'catalog and E1/E2 from your evidence block instead of repeating commands, counts and paths '
    'in each row. T# and scaffold E# slots are not invocation counts. Identify each actual invocation '
    'by tool-call ID (unknown if unavailable), its own log directory, actor and result. A known '
    'target-only check is not_run and NOT VERIFIED locally, with its coordinator named; no invented '
    'exit code or log. Add evidence entries for distinct invocations, including failed attempts; '
    'never collapse an earlier failure into a later pass. Split compound requirements into '
    'suffixed rows as needed. Placeholders and an empty ledger are not evidence.\n'
    'A BLOCKED or NEEDS_CONTEXT report must name the missing fact, the checks already attempted, '
    'the available candidate/evidence and the exact help needed; do not invent completion or add '
    'probes to fill the gap. The final text is captured by the existing worker-result; do not '
    'create unauthorized report files.\n')


def transport_policy(target):
    """Resolve bounded command-connection options, without reading SSH config."""
    transport = target['transport']
    if transport not in ('local', 'ssh'):
        raise ValueError('Target requires local/ssh transport')
    policy = {'version': 1, 'transport': transport}
    if transport == 'local':
        if 'ssh_keepalive' in target:
            raise ValueError('ssh_keepalive is only valid for SSH targets')
        return policy
    values = target.get('ssh_keepalive', {})
    bounds = {'server_alive_interval': (15, 1, 300), 'server_alive_count_max': (2, 1, 10)}
    if not isinstance(values, dict) or set(values) - set(bounds):
        raise ValueError('ssh_keepalive requires only server_alive_interval/server_alive_count_max')
    resolved = {}
    for key, (default, low, high) in bounds.items():
        value = values.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('%s must be an integer from %d to %d' % (key, low, high))
        resolved[key] = value
    policy['ssh_keepalive'] = resolved
    return policy


def validate_transport_policy(task, meta):
    from process_evidence import enabled
    enabled(task, meta)
    resolved = transport_policy(task['target'])
    if 'transport_policy' in task or 'transport_policy' in meta:
        if task.get('transport_policy') != resolved or meta.get('transport_policy') != resolved:
            raise ValueError('Transport policy changed; prepare a fresh run')
    elif 'ssh_keepalive' in task['target']:
        raise ValueError('Legacy runs keep historical SSH options; prepare a fresh run')
    return resolved


def target_repo(task, meta):
    validate_transport_policy(task, meta)
    return Repo(task['target'])


def dump(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(0o600)
    os.replace(temp, path)


def load(path):
    return json.loads(Path(path).read_text())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True).encode())


def checked_path(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or str(p) != value or any(x in ('..', '.git') for x in p.parts):
        raise ValueError('Invalid repository-relative path: ' + repr(value))
    if '\x00' in value or '\n' in value or '\r' in value:
        raise ValueError('Control characters in path')
    return value


def cmd(argv, cwd=None, data=None, timeout=30, env=None, check=True, capture_limits=None):
    if capture_limits is not None:
        from ssh_capture import bounded_run
        return bounded_run(argv, cwd=cwd, data=data, timeout=timeout, env=env,
                           check=check, capture_limits=capture_limits)
    r = subprocess.run(argv, cwd=cwd, input=data, capture_output=True, timeout=timeout, env=env)
    if check and r.returncode:
        raise RuntimeError('Command failed (%s): %s' % (r.returncode, r.stderr.decode(errors='replace')[-2000:]))
    return r


class Repo:
    def __init__(self, target):
        self.target = target
        self.transport_policy = transport_policy(target)
        self.repo = target['repo']
        self.remote = target['transport'] == 'ssh'
        if target['transport'] not in ('local', 'ssh') or not self.repo.startswith('/'):
            raise ValueError('Target requires local/ssh transport and absolute repo path')
        self.host = target.get('host')
        if self.remote and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', self.host or ''):
            raise ValueError('Use a configured SSH host alias')

    def run(self, argv, data=None, timeout=30, check=True, capture_limits=None):
        extra = {} if capture_limits is None else {'capture_limits': capture_limits}
        if self.remote:
            command = 'cd -- ' + shlex.quote(self.repo) + ' && exec ' + shlex.join(argv)
            keepalive = self.transport_policy['ssh_keepalive']
            options = ['-o', 'ServerAliveInterval=%d' % keepalive['server_alive_interval'],
                       '-o', 'ServerAliveCountMax=%d' % keepalive['server_alive_count_max']]
            return cmd(SSH + options + [self.host, command], data=data, timeout=timeout, check=check, **extra)
        return cmd(argv, cwd=self.repo, data=data, timeout=timeout, check=check, **extra)

    def text(self, argv):
        return self.run(argv).stdout.decode().strip()

    def identity(self):
        if self.remote:
            return self._remote_capture([], contents=False)['identity']
        root = self.text(['git', 'rev-parse', '--show-toplevel'])
        real = self.text(['pwd', '-P'])
        if root != real:
            raise ValueError('Target must be the repository root')
        return {'host': self.text(['hostname']), 'user': self.text(['id', '-un']),
                'repo': real, 'head': self.text(['git', 'rev-parse', 'HEAD']),
                'status': self.text(['git', 'status', '--porcelain=v1', '--untracked-files=all'])}

    def _test(self, *args):
        result = self.run(['test', *args], check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError('Cannot inspect path (%s): %s' % (
                result.returncode, result.stderr.decode(errors='replace')[-1000:]))
        return result.returncode == 0

    def _remote_capture(self, paths, contents):
        from ssh_capture import canonical, decode_response, validate_request, MAX_RESPONSE, MAX_STDERR
        request = {'version': 1, 'nonce': uuid.uuid4().hex, 'repo': self.repo,
                   'paths': list(paths), 'contents': contents}
        validate_request(request)
        # Execute stdlib-only source without installing code on the target.
        source = Path(__file__).with_name('ssh_capture.py').read_text()
        result = self.run(['python3', '-I', '-B', '-c', source], data=canonical(request),
                          timeout=30, capture_limits=(MAX_RESPONSE, MAX_STDERR))
        # Even a complete-looking body accompanying a failed SSH is not evidence.
        if result.returncode != 0:
            raise RuntimeError('SSH capture transport failed (%s)' % result.returncode)
        return decode_response(result.stdout, request)

    def capture(self, paths):
        paths = list(paths)
        if self.remote:
            result = self._remote_capture(paths, contents=True)
            return result['identity'], result['manifest'], result['files']
        identity, baseline, files = self.identity(), {}, {}
        for name in paths:
            state, data = self.file(name)
            baseline[name] = state
            if data is not None:
                files[name] = data
        if self.manifest(paths) != baseline or self.identity() != identity:
            raise RuntimeError('Source changed while snapshotting; retry prepare')
        return identity, baseline, files

    def file(self, name):
        checked_path(name)
        if self.remote:
            result = self._remote_capture([name], contents=True)
            return result['manifest'][name], result['files'].get(name)
        # Refuse every symlink component; a worktree is not a security sandbox.
        parts = PurePosixPath(name).parts
        for i in range(1, len(parts) + 1):
            if self._test('-L', './' + '/'.join(parts[:i])):
                raise ValueError('Symlink is unsupported: ' + name)
            if i < len(parts) and self._test('-e', './' + '/'.join(parts[:i]) + '/.git'):
                raise ValueError('Submodule or nested Git repository is unsupported: ' + name)
        if not self._test('-e', './' + name):
            return {'exists': False}, None
        self.run(['test', '-f', name])
        size = int(self.text(['wc', '-c', './' + name]).split()[0])
        if size > 4 * 1024 * 1024:
            raise ValueError('File exceeds 4 MiB; select smaller source context: ' + name)
        data = self.run(['cat', './' + name]).stdout
        mode = '100755' if self._test('-x', './' + name) else '100644'
        return {'exists': True, 'sha256': digest(data), 'mode': mode}, data

    def manifest(self, paths):
        if self.remote:
            return self._remote_capture(paths, contents=False)['manifest']
        return {p: self.file(p)[0] for p in paths}


def git(snapshot, *args, data=None):
    return cmd(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=snapshot, data=data).stdout


def run_state(run):
    run = Path(run).resolve()
    return run, load(run / 'metadata.json'), load(run / 'task.json')


def require_worker(run, meta):
    task = load(run / 'task.json')
    expected_model = model_id(frozen_model(task, meta))
    validate_bash_policy(task, meta, run)
    result = load(run / 'worker-result.json') if (run / 'worker-result.json').exists() else {}
    if meta.get('worker_mode') == 'rpc' and not result.get('terminal_event'):
        raise RuntimeError('Terminal RPC agent_end evidence is required')
    if not (meta.get('process') == 'exited' and meta.get('exit_code') == 0 and
            result.get('models') == [expected_model] and not result.get('errors') and
            usable_final_text(result.get('final_text')) and result.get('tools')):
        raise RuntimeError('Worker success and expected model evidence are required')


def require_identity(actual, expected):
    if any(actual.get(k) != expected.get(k) for k in ('host', 'user', 'repo', 'head')):
        raise RuntimeError('Target identity/HEAD changed; prepare a fresh run')


@contextlib.contextmanager
def local_lock(run):
    lock = run / '.operation-lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise RuntimeError('Another operation owns this run; inspect .operation-lock before recovery')
    try:
        yield
    finally:
        lock.rmdir()



def acceptance_inventory(task):
    """Index original acceptance values without interpreting or duplicating them."""
    inventory = {'version': 1, 'source_state': 'absent', 'rows': []}
    if 'acceptance' not in task:
        return inventory
    value = task['acceptance']
    empty = value is None or value == [] or value == {} or (
        isinstance(value, str) and not value.strip())
    inventory['source_state'] = 'empty' if empty else 'present'
    if not empty:
        pointers = ([f'/acceptance/{i}' for i in range(len(value))]
                    if isinstance(value, list) else ['/acceptance'])
        inventory['rows'] = [{'id': f'A{i + 1}', 'source_pointer': pointer}
                             for i, pointer in enumerate(pointers)]
    return inventory


def handoff_template(task):
    """Unfilled communication scaffold; neither a validator nor a success signal."""
    inventory = acceptance_inventory(task)
    lines = ['Status <DONE|DONE_WITH_CONCERNS|NEEDS_CONTEXT|BLOCKED>']
    for i, _ in enumerate(task.get('test_commands', []), 1):
        lines.append(f'Evidence E{i}: T{i}; environment=<actual>; exit=<observed/unknown>; '
                     'counts=<executed/skipped/failures/errors or unknown>; logs=<paths/unavailable>; '
                     'kind/actor=<actual>; invocation=<tool-call ID/unknown/not_run>')
    lines.append('ID | implementation + file:symbol | verification + E#/case/boundary; '
                 'fault regression status if required | remaining blocker')
    for row in inventory['rows']:
        lines.append(f"{row['id']} | <complete/partial/not_implemented/unknown + location> | "
                     '<pass/fail/not_verified + evidence; required regression present/missing/unknown/not_required> | '
                     '<specific blocker/none if established>')
    if not inventory['rows']:
        lines.append(f"Acceptance: {inventory['source_state']}; NOT VERIFIED; <scope gap to resolve>")
    lines += ['Pending tools/jobs: <identities + observed states/none if established/unknown>',
              'Scratch: <paths + observed cleanup/authorized retained/residual/unverified/none if unused>',
              'Deviations/concerns: <all actual issues and help needed/none if established>']
    return '\n'.join(lines)


def prepare(task_path):
    from process_evidence import POLICY, POLICY_GATE, validate_test_argv
    from reviewer_provenance import POLICY as REVIEWER_POLICY
    from async_evidence import POLICY_KEY as ASYNC_KEY, POLICY as ASYNC_POLICY
    task = load(task_path)
    if task.get('agent_runtime', 'local') != 'local':
        raise ValueError('All model agents must run locally')
    allowed = [checked_path(p) for p in task['allowed_paths']]
    paths = sorted(set(allowed + [checked_path(p) for p in task.get('context_paths', [])]))
    if not allowed or len(paths) > 100:
        raise ValueError('Require allowed_paths and at most 100 explicit source files')
    deletions = task.get('allowed_deletions', [])
    if not isinstance(deletions, list) or any(not isinstance(p, str) or p not in allowed for p in deletions):
        raise ValueError('allowed_deletions must be explicit allowed_paths entries')
    task['allowed_deletions'] = sorted(set(deletions))
    task['snapshot_guard_version'] = 1
    task['process_evidence_policy'] = copy.deepcopy(POLICY)
    task['reviewer_provenance_policy'] = copy.deepcopy(REVIEWER_POLICY)
    task[ASYNC_KEY] = copy.deepcopy(ASYNC_POLICY)
    # Resolve selectable domain guidance before any source capture.
    task['worker_guidance_policy'] = worker_guidance.resolve(task)
    validate_task_contract(task, paths)
    for argv in task['test_commands']:
        validate_test_argv(argv)
    from worker_bash import mode as bash_mode
    if bash_mode(task) == 'declared_tests_only':
        task['process_evidence_policy'] = copy.deepcopy(POLICY_GATE)
    model = task.get('worker_model', MODEL)
    if model != MODEL:
        raise ValueError('This configured profile supports only ' + MODEL)
    validate_worker_options(task, resolve=True)
    repo = Repo(task['target'])
    task['transport_policy'] = copy.deepcopy(repo.transport_policy)
    if repo.remote:
        task['target']['ssh_keepalive'] = copy.deepcopy(repo.transport_policy['ssh_keepalive'])
    identity, baseline, captured = repo.capture(paths)
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = (RUNS / (time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])).resolve()
    run.mkdir(mode=0o700)
    snapshot = run / 'snapshot'
    snapshot.mkdir()
    for name in paths:
        state, data = baseline[name], captured.get(name)
        if data is not None:
            f = snapshot / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            f.chmod(0o755 if state['mode'] == '100755' else 0o644)
    for interface in task.get('interface_contracts', []):
        if any(not baseline[p]['exists'] for p in interface['source_paths']):
            raise ValueError('Interface source paths must exist in the captured snapshot')
    if any(not baseline[p]['exists'] for p in task['allowed_deletions']):
        raise ValueError('allowed_deletions must exist in the captured baseline')
    git(snapshot, 'init', '-q')
    (snapshot / '.git/info/exclude').write_text('__pycache__/\n*.pyc\n.pytest_cache/\n')
    git(snapshot, 'add', '-A')
    git(snapshot, '-c', 'user.name=Team Dev Snapshot', '-c', 'user.email=team-dev@localhost',
        'commit', '-q', '--allow-empty', '-m', 'Captured task source')
    task['worker_model'] = model
    task['worker_bash_policy'] = resolve_bash_policy(task, run)
    from bash_gate import prepare as prepare_gate, KEY as GATE_KEY
    prepare_gate(task, run)
    dump(run / 'task.json', task)
    dump(run / 'baseline.json', baseline)
    metadata = {'run': str(run), 'created_at': time.time(), 'source': identity, 'paths': paths,
                'snapshot_commit': git(snapshot, 'rev-parse', 'HEAD').decode().strip(),
                'process': 'not_started', 'delivery': 'prepared', 'worker_model': model,
                'execution_policy': task['execution_policy'],
                'transport_policy': task['transport_policy'],
                'worker_bash_policy': copy.deepcopy(task['worker_bash_policy']),
                'process_evidence_policy': copy.deepcopy(task['process_evidence_policy']),
                'reviewer_provenance_policy': copy.deepcopy(REVIEWER_POLICY),
                ASYNC_KEY: copy.deepcopy(ASYNC_POLICY),
                'worker_guidance_policy': copy.deepcopy(task['worker_guidance_policy']),
                'snapshot_guard': {'version': 1, 'allowed_deletions': task['allowed_deletions']}}
    if GATE_KEY in task:
        metadata[GATE_KEY] = copy.deepcopy(task[GATE_KEY])
    dump(run / 'metadata.json', metadata)
    worker_test_argv, worker_direct_commands, worker_logged_commands = bash_commands(task, run)
    from worker_test_catalog import catalog as test_catalog
    worker_log_root = run / 'worker-test-logs'
    worker_log_root.mkdir(mode=0o700)
    strict_bash = task['worker_bash_policy']['mode'] == 'declared_tests_only'
    strict_notice = ('STRICT BASH MODE: Select the logged worker_test_invocations reference (T1.logged for T1) and copy its complete arguments object into the Bash tool call. Use it for every acceptance test regardless of expected output length; it preserves the full stdout/stderr and the saved exit-code. The unchanged runtime catalog still permits T1.direct, including for frozen tasks, but new tasks use logged by default; a coordinator-authored exception may retain it with its evidence limitation. References are lookup labels, not executable commands. Do not spell out/reconstruct the environment prefix or logging envelope. The catalog contains exactly the corresponding worker_direct_test_commands and worker_logged_test_commands strings below. After a denial, inspect the exact-test hint and recopy the selected object; do not guess a replacement. Bash arguments may contain only command, optional finite timeout (0 < seconds <= 3600), and optional cwd copied exactly from worker_bash_snapshot_cwd. Omit cwd or use that exact absolute snapshot path; do not use dot, parent paths, aliases or another run. No pty, async or other controls. Use read/grep/glob tools for context and JSON; use the native read tool for completed stdout.log, stderr.log and exit-code. Do not invoke Bash cat, head, tail, grep or a shell wrapper to read these files, even after a successful test or for the one-line exit-code. Read-only Bash is still outside the allowed command list. No wc, Python probes, alternative interpreter, cd, suffix/pipeline or altered envelope. If read tools are insufficient, report missing context and finish. General resource and bytecode guidance never grants extra Bash permission. The pinned pre-execution gate blocks undeclared commands or invalid arguments before Bash. The first two denials return a tool error so you can correct the tool choice; the third denial stops this attempt. Do not repeat a denied request. Denials do not extend progress or closeout deadlines. Gate evidence failure stops the attempt without fallback. A created log directory without exit-code does not establish a test result.\n'
                     if strict_bash else '')
    edit_notice = ('SOURCE EDIT ROUTING: Make source changes only through the native edit or write tools within allowed_paths. Use read/grep/glob to locate and inspect the actual current text first. A named keyword and a positional string literal are different edit targets; do not guess a global replacement. Bash is only for the exact prepared tests, never sd, sed -i, perl, a Python/shell rewriting script, redirection or a patch command to edit files. Do not embed a repair in a test command. If edit/write fails or is unavailable, report the blocker; do not fall back to Bash, install an editor, or broaden the command list.\n'
                   if strict_bash else '')
    probe_notice = ('Every disposable image/output and all consumers must remain inside an owned TemporaryDirectory context; importing tempfile or catching a render exception does not establish that scope.\n' if strict_bash else
                    'The same full-output and explicit bytecode-guard requirements apply to dependency imports, inline Python diagnostics and manual render/layout probes. Run short probes directly with the declared interpreter and -B/env; never pipe their live output through head/tail/grep/tee. For large output use the same status-preserving logging recipe with the probe argv, clearly labelled exploratory, not an acceptance-test substitute. Every disposable image/output and all consumers must remain inside an owned TemporaryDirectory context; importing tempfile or catching a render exception does not establish that scope.\n')
    interpreter_notice = ('' if strict_bash else
                    'For an authorized exploratory Python check, use the exact declared test interpreter, including its absolute environment path; PATH python3 is not equivalent merely because both use -B/env. Report a missing dependency/runtime as NOT VERIFIED rather than switching interpreter or installing it. If the task forbids extra Bash/Python probes or allows only listed tests, obey that narrower scope; a dependency check is not an exception. The helper does not semantically enforce every probe-purpose or interpreter choice.\n')
    syntax_notice = ('STRICT CHECK ROUTING: Bash may run only an exact prepared test command; task constraints below may further restrict when to test. No separate read-only checksum, dependency/version, import, syntax or preflight probes.\nEVIDENCE ROUTING: Dependency/version and checksum preflight belongs to the coordinator within the authorized task. Use supplied evidence or declared-test results without re-probing. A hash read from a manifest is a declared value, not fresh verification of file bytes. Keep missing facts unknown and report actual blockers; do not invent versions or hashes.\nTOOL ROUTING: read/grep/glob inspect content; edit/write modify allowed source; Bash runs exact declared tests. The declared interpreter, bytecode guards and read-only intent do not grant extra commands. Do not alter a test command or hide a probe inside its envelope.\n' if strict_bash else
                    'For Python syntax-only checks, use env PYTHONDONTWRITEBYTECODE=1 python3 -B with ast.parse on the source text, without importing/executing the project. Follow any task-specific interpreter or flags.\n')
    inline_notice = ('Use the declared test files and exact prepared test commands; if cleanup ownership is uncertain, leave the temporary artifact and report it.\n' if strict_bash else
                    'Use inline tests or declared test files; if cleanup ownership is uncertain, leave the temporary artifact and report it.\n')
    # Common guidance: keep context reads native and bounded for strict and relaxed modes.
    context_notice = ('CONTEXT INSPECTION: Use the native read/grep/glob tools for context and structured logs; retain the task\'s existing Bash permissions. For large files or JSON, use narrow file:line selectors and specific patterns. grep takes pattern, path, case, gitignore and skip; there is no limit or max_results argument, but a file:50-100 selector is supported. Do not enumerate a large JSON schema unbounded; missing context remains a blocker and this routing only narrows how you look, it never weakens full saved test-log inspection.\n')
    entry_notice = (syntax_notice + 'TASK CONSTRAINTS: ' + json.dumps(task.get('constraints', []), ensure_ascii=False) + '\n'
                    if strict_bash else '')
    core_notice = ('IMPLEMENTATION ORDER: Build the smallest executable public path and its independent acceptance test before broad validation/reporting scaffolding. Exercise the actual backend selector/binding or producer-consumer boundary using only task-authorized sources and fixtures. New files can still be integration work; missing authoritative interfaces or units are blockers, not permission to invent a matching fixture protocol. Keep all required safety, identity and failure checks in the core. Once interfaces are known, produce that core/test or a concrete blocker instead of rereading unchanged context; repeated reads or reasoning alone are not executable progress.\n'
                   'FIRST TEST: A command start is activity, not the checkpoint. The intended test must load and exercise the real current-layer public call against an independent expectation; a behavior-level assertion/protocol failure is useful RED. Missing modules, import/setup-only failures and all-skipped suites leave the checkpoint unmet: keep diagnostics, implement/repair the owned core/test, or report BLOCKED/NOT VERIFIED for a genuinely missing prerequisite. Missing-function TDD RED is preserved but does not prove the public call ran. Use an exact prepared test invocation as soon as that core is executable. Follow the task checkpoint; absent a stricter stated checkpoint, aim to start by the earlier of one third of the total attempt budget or one third of the closeout window after the first change. These are planning checkpoints, not new timers or extra Bash permission. A failing core test is useful feedback; fix that boundary before expanding scope. Missing target-only execution remains NOT VERIFIED.\n'
                   'HANDOFF: Keep brief acceptance notes while implementing. After a test completes, inspect its actual exit and complete results next (saved logs for a logged call), then finish remaining required checks. Once ready, return the ledger and shared test summary directly; omit code walkthroughs, copied code and future-tense work narration. Do not reread unchanged source, rerun passing tests or polish without a concrete unresolved requirement, source change or new failure. Later edits need affected checks again; pending jobs, missing checks and blockers stay explicit, never waived.\n')
    guidance_policy = task['worker_guidance_policy']
    guidance_block = ''.join(worker_guidance.text(guidance_policy, name)
                             for name in guidance_policy['sections'])
    prompt = (entry_notice + core_notice + strict_notice + edit_notice + context_notice + 'You are the implementation worker. Work only in this local task snapshot.\n'
              'Do not SSH, access credentials, install packages, commit, publish, or spawn agents.\n'
              'Only modify the exact allowed paths. Ask the coordinator if required context is missing.\n'
              'Delete existing source files only when explicitly listed in allowed_deletions; never delete directories or snapshot/.git.\n'
              'For disposable test fixtures use with tempfile.TemporaryDirectory() as tmp: and let that context manager clean up.\n'
              'This includes manual smoke checks and public CLI invocations, not only test functions. Keep creation, every synchronous consumer subprocess, and assertions inside the same context.\n'
              'Do not use fixed /tmp names, mkdtemp without owned cleanup, detached jobs or separate tool calls that outlive the context. Check the temp parent is outside the snapshot before creating it.\n'
              'Keep scratch files outside the snapshot in that owned temporary directory; do not add ad-hoc scripts outside allowed_paths.\n'
              'Never use shell recursive cleanup or hand-written rmtree for test scratch. Never select cleanup paths using pwd, globs, ls/head, or command substitution that can be empty.\n'
              + inline_notice +
              'The test_commands below are the exact acceptance argv, run from the repository/snapshot root.\n'
              'Follow the T# local-executable/target-only/availability-unknown matrix in task constraints or selected contract. The catalog supplies exact syntax, not proof that every command runs on this Mac. Do not invoke a known target-only check locally to rediscover an established missing runtime; report NOT VERIFIED locally and its coordinator owner. Do not replace argv, install/search for a substitute runtime, or infer target success. Unknown availability remains unknown until supplied coordinator evidence or an authorized declared test establishes it.\n'
              'worker_test_commands prefix those exact argv with env PYTHONDONTWRITEBYTECODE=1. Run local checks through the corresponding worker_logged_test_commands (T#.logged) Bash command regardless of expected output length: it runs the same argv, stores full stdout/stderr and exit-code in a fresh private directory, and returns that test status. Run it unchanged from the snapshot root with a finite Bash timeout.\n'
              'The prepared worker_test_log_root is authorized retained evidence outside the snapshot, not disposable scratch. Each invocation prints its directory before starting; preserve every failed attempt and never overwrite/delete these logs. This is a logging envelope, not a replacement test runner or target-test receipt.\n'
              'After synchronous completion or the matching terminal async notification, use the native read tool directly for exit-code, stdout.log and stderr.log. Use worker_test_log_read_templates below: replace <WORKER_TEST_LOG_DIR> with the exact absolute directory printed after WORKER_TEST_LOG_DIR= by this invocation, not the parent worker_test_log_root, a guessed check-* path, a shell variable or another attempt. Pass each resulting arguments object to read; these are tool-call templates, not Bash commands. The stdout/stderr :1-200 suffix selects an initial bounded range; continue with the read tool as needed. Do not submit the placeholder literally or invoke Bash to construct/read paths. If the marker is missing or read cannot access a file, report the blocker and available evidence; do not substitute cat or rerun the test to recover a path. Console excerpts are navigation only; retain full files. A missing exit-code means outcome unverified, even if partial logs exist. Do not poll a running command for these files. Do not rerun tests just to shorten output; never pipe tests through tail/head/grep/tee or filters, append cleanup, or hide a failure behind a successful print/read.\n'
              'Do not change the generated envelope: keep its final exit "$team_dev_test_exit". Printing the saved status or log path is not returning it; a trailing echo can hide a failed test. Review the actual exit-code and complete logs even when the outer Bash returns zero.\n'
              + probe_notice +
              'A dependency import check is a live Python producer, even if it prints one line. Do not use python -c "import ..." 2>&1 | tail -1: the filter can return success after an import failure. New process policy v3 stops recognized Python-to-head/tail/grep/tee requests after observation, regardless of the outer Bash result. This is not pre-execution interception. Inspect already completed saved logs using the native read tool and the templates below. A successful log read is not a successful test and does not waive strict Bash matching.\n'
              + interpreter_notice +
              'This new task records exact Bash arguments/results privately for process review. Recognized recursive removal or direct test pipeline requests stop the worker after observation; unparsed commands are not automatically compliant. Do not try to bypass these constraints with quoting or wrappers.\n'
              'A Bash async running/queued response is not completion. Track the tool-call and job IDs, wait for its structured async-result notification, and report each actual outcome/exit code (unknown when absent). Follow OMP no-poll notices; never infer success from tool_execution_end or agent_end.\n'
              'An incoming steer may move an already-running Bash command into the background automatically, even if you did not request a detached job. Keep the same invocation/job and reported log path; do not launch a duplicate test or let dependent edits/cleanup race it. A steer ACK is not cancellation. Wait for the notice without polling; then inspect logs/status and report resource ownership separately.\n'
              'Use finite timeouts for waits/joins/IPC probes, keep readers and writers able to make progress, and print owned scratch paths before a potentially blocking operation. A completed/interrupt result or TemporaryDirectory syntax does not prove child-process exit or path removal. Cleanup without an observed path/postcondition remains unverified; do not claim all commands succeeded when a probe failed.\n'
              'Preserve failures and exact event ordering. Attribute a signal only to its observed sender; a delayed coordinator message does not establish when the underlying process observation occurred. Astra collects scoped lifecycle receipts for async jobs and Sol reviews them; do not fabricate those receipts or kill unknown processes.\n'
              'OMP can reuse a bg_N after delivery. Treat every later Bash invocation as separate work with its own tool-call/log identity and result; never use an earlier notification or saved exit code to close a new running return. Preserve all generations for coordinator review.\n'
              'Python -I/-E and environment-clearing wrappers can ignore that variable; report this limitation and use an explicitly declared -B command when needed, without silently replacing the acceptance argv. Existing caches must be left and reported, not recursively removed.\n'
              + ('' if strict_bash else syntax_notice) +
              'Neither -B nor PYTHONDONTWRITEBYTECODE prevents explicit py_compile/compileall output. Do not use those to perform a no-artifact syntax check. If explicit compilation is required, keep its output and every consumer inside an owned TemporaryDirectory context; never use a fixed /tmp path or a later rm command.\n'
              'Use that test framework and run those exact commands when this snapshot has the required environment.\n'
              'If imports/dependencies/target runtime are missing, report NOT VERIFIED and the specific blocker.\n'
              + guidance_block +
              'When the task specifies a REQUIRED test flag, every new module and import/fallback fixture must resolve and honor it before heavy imports. Required-mode import failures must fail, never skip; a sibling module does not cover this module. Optional local skips are only for task-approved missing dependencies, not project bugs or arbitrary exceptions. Exercise that policy in declared tests with synthetic import failures; do not install/uninstall dependencies, fabricate sys.modules or add Bash probes. Local all-skipped output is NOT VERIFIED, not source functional validation.\n'
              'Never substitute a pytest shim, fake dependency package, different runner or invented local package environment for acceptance.\n'
              'Implement the requested test files using the actual framework and producer/helper functions. Ad-hoc inline runners and copied reference implementations do not replace those tests; distinguish any exploratory check from acceptance. If test argv require SSH or a target-only environment, leave them to the coordinator and report NOT VERIFIED locally.\n'
              'Never directly mutate sys.modules in tests, including inside stub-builder functions. Use scoped mock.patch in test functions; import real project modules.\n'
              'Pure project modules and integration paths must be tested together with real implementations; mock only explicit external boundaries.\n'
              'Preserve the interface_contracts exactly: field names, dimensions, units, coordinate frame/origin/center convention, selector keys, call signatures and return shapes. Reuse the specified module path AND symbol, tracing the actual call/import; a same-named helper in another module may change thresholds, tie rules or reasons. Adapt only the necessary argument/return shape and test a distinguishing boundary with an independent expected value. Role names need not equal backend selector keys. Do not invent replacements or mock away the binding/conversion under test; forbidden imports remain forbidden.\n'
              'Map each contract acceptance row to its actual public entrypoint/gate and the declared test that exercises it. A stricter test-only helper does not protect real exports. For a required rejection, inject the defect through the public path and assert its specified rejection; for an output requirement, inspect the real serialized/saved result with an independently derived expectation. Do not weaken expected identities by applying the implementation normalization to both sides.\n'
              'Existing tests are regression evidence, not the scope definition. Do not defer an original acceptance requirement because the old suite lacks coverage, fixtures need repair, or a new case would fail an old oracle. Within allowed_paths, implement required behavior and update/add tests against the written contract using the frozen declared commands; never weaken the contract to make old tests pass. If requirements genuinely conflict or authorized files/context/commands are insufficient, preserve the requirement, report the exact blocker and finish with incomplete status. Only the coordinator can decide a scope change within user authorization and prepare a fresh task; later slices still retain the overall obligations.\n'
              + WORKER_COMMUNICATION +
              'That compact COMPLETE acceptance ledger has one row for every worker_acceptance_inventory row: ID; implementation (complete/partial/not_implemented/unknown) with file and public symbol; verification (pass/fail/not_verified) with exact declared check/case, environment and saved result; remaining blocker. Write shared commands/counts/log locations once and reference them from compact rows; keep each requirement and any distinct failure/blocker explicit. source_pointer points into the original acceptance value in this payload; IDs are local to this preparation. Split compound rows into suffixed subrows (A1.1 etc.) without losing any clause, including referenced selected contracts; preserve explicit coordinator exclusions. The index does not interpret prose or validate completeness. If acceptance is absent/empty/ambiguous, disclose that gap; an empty ledger is not acceptance.\n'
              'For each cited observation label evidence kind (persistent_test/coordinator_check/source_inspection/none) and actor. Name the retained test file/case, actual public or helper boundary and execution result for persistent_test; supplied coordinator_check evidence is not your execution or a persistent regression case. Cite its retained replay script/command when available and disclose missing replay material; never invent a recipe or rerun/expand work solely to improve the classification of old evidence. Keep source-only runtime claims and unexecuted checks not_verified. For each fault branch state regression_test (present/missing/unknown/not_required) according to the original contract: present requires a retained case at the required boundary; a coordinator-only runtime pass cannot fill a required missing test. Do not invent coordinator checks or add undeclared probes. Separate different mutations, helper/public faults and success/failure cleanup; one passing branch or a suite total does not cover the others. Report persistent coverage, coordinator-only/source-only evidence and gaps separately, not all pass while a required check/test is missing. Preserve prior evidence and attribute later additions separately; no new test obligation or run-state gate is introduced.\n'
              'Check concrete numeric/result-label statements against the exact saved structured fields and their label definitions, bound to this candidate/input; cite the path/field and supporting declared assertion or independent evidence. Test expectations alone are not observed output. Preserve unknowns and report discrepancies; do not invent values, overwrite prior reports or add undeclared probes/experiment reruns to improve the report.\n'
              'Distinguish implementation, static inspection and functional execution in every claim. For runtime criteria, static tracing, exit zero, normal agent_end or a green old suite do not establish tested acceptance. Record actual collected/executed/skipped counts and failures/errors when available, otherwise unknown; all-skipped means zero functional cases executed. A skipped or unexecuted required check stays not_verified even when code appears complete; a failing required check stays fail. Missing/partial requirements stay incomplete for coordinator/Sol review, not implicitly deferred or waived. This ledger is a self-report for independent review, not an automatic proof or a new Bash permission.\n'
              'After the first actual snapshot difference, the frozen closeout budget covers implementation, declared tests and final handoff. Edits, reads, prose and steering ACKs never reset it. Reserve time for tests, complete result reads and the concise ledger/report before expanding the candidate; do not defer assembling all evidence until the final seconds. When blocked, report NOT VERIFIED and finish without claiming acceptance. Do not fake progress or skip acceptance to beat the timer. An active test still consumes this budget, with no automatic extension.\n'
              'Use the coordinator-defined interfaces; implement one bounded change promptly.\n'
              'Do not expand into unrelated API/state transitions or write placeholders just to register progress; report scope blockers to the coordinator.\n'
              'If context is missing, state the blocker and finish; do not redesign the whole system.\n'
              'Use the handoff template slots for exact checks, scratch paths with cleanup status (cleaned, authorized retained evidence, residual, or unverified), and ALL task-constraint deviations/unresolved issues, even if tests pass.\n'
              'Cross-check those claims against actual tool requests and results. Disclose forbidden cleanup requests, distinguish requested/executed/unknown outcomes, and never use clean git status or ignored cache paths as proof that no cleanup occurred.\n'
              'If scratch was unused say none. A leftover or unknown cleanup is not harmless/compliant by assertion; preserve uncertain ownership and report the deviation for coordinator/Sol review.\n\n'
              + json.dumps(dict({k: task[k] for k in ('goal', 'allowed_paths', 'allowed_deletions', 'context_paths', 'acceptance', 'constraints',
                    'task_type', 'interface_contracts', 'test_commands', 'integration_test_commands', 'test_timeout_seconds') if k in task},
                    worker_acceptance_inventory=acceptance_inventory(task),
                    worker_handoff_template=handoff_template(task),
                    worker_guidance_policy=task['worker_guidance_policy'],
                    worker_closeout_budget=task['execution_policy']['closeout'],
                    worker_bash_mode=task['worker_bash_policy']['mode'],
                    worker_bash_snapshot_cwd=task['worker_bash_policy']['snapshot_cwd'],
                    worker_test_invocations=test_catalog(task, run),
                    worker_direct_test_commands=worker_direct_commands,
                    worker_test_commands=worker_test_argv,
                    worker_logged_test_commands=worker_logged_commands,
                    worker_test_log_read_templates=[
                        {'tool': 'read', 'arguments': {'path': '<WORKER_TEST_LOG_DIR>/exit-code'}},
                        {'tool': 'read', 'arguments': {'path': '<WORKER_TEST_LOG_DIR>/stdout.log:1-200'}},
                        {'tool': 'read', 'arguments': {'path': '<WORKER_TEST_LOG_DIR>/stderr.log:1-200'}},
                    ],
                    worker_test_log_root=str(worker_log_root)),
                           ensure_ascii=False, indent=2))
    (run / 'prompt.md').write_text(prompt)
    return metadata


def has_snapshot_guard(meta, task):
    # A partial/null marker is corrupted new policy, never a legacy downgrade.
    return 'snapshot_guard' in meta or 'snapshot_guard_version' in task


def snapshot_changes(run, meta, task):
    from snapshot_guard import SnapshotGuard, SnapshotViolation
    policy = meta.get('snapshot_guard')
    if has_snapshot_guard(meta, task):
        expected = {'version': task.get('snapshot_guard_version'),
                    'allowed_deletions': task.get('allowed_deletions', [])}
        if policy != expected or policy.get('version') != 1:
            raise SnapshotViolation('snapshot_guard_policy_changed')
        return SnapshotGuard(run/'snapshot', meta['snapshot_commit'],
                             task['allowed_paths'], policy['allowed_deletions']).inspect()
    # Already-prepared runs retain the previous scanning behavior.
    snapshot = run/'snapshot'
    names = git(snapshot, 'diff', '--name-only', '-z', meta['snapshot_commit'])
    names += git(snapshot, 'ls-files', '--others', '--exclude-standard', '-z')
    return sorted(set(x.decode() for x in names.split(b'\0') if x) & set(task['allowed_paths']))


def validate_task_contract(task, paths):
    """Version new dispatches; existing runs are not retroactively reclassified."""
    commands = task.get('test_commands')
    if not isinstance(commands, list) or not commands or any(
            not isinstance(argv, list) or not argv or
            any(not isinstance(v, str) or not v or '\x00' in v for v in argv) for argv in commands):
        raise ValueError('Provide nonempty test_commands as exact argv arrays')
    kind = task.setdefault('task_type', 'implementation')
    if kind not in ('implementation', 'integration'):
        raise ValueError('task_type must be implementation or integration')
    interfaces = task.get('interface_contracts', [])
    if not isinstance(interfaces, list):
        raise ValueError('interface_contracts must be a list')
    for interface in interfaces:
        if not isinstance(interface, dict) or any(not isinstance(interface.get(k), str) or
                not interface[k].strip() for k in ('name', 'inputs', 'returns')):
            raise ValueError('Each interface needs name, inputs and returns')
        sources = interface.get('source_paths')
        if not isinstance(sources, list) or not sources or any(not isinstance(p, str) or p not in paths for p in sources):
            raise ValueError('Interface source_paths must be explicit selected context/allowed paths')
    integration = task.get('integration_test_commands', [])
    if not isinstance(integration, list) or any(argv not in commands for argv in integration):
        raise ValueError('integration_test_commands must be included in test_commands')
    if kind == 'integration' and (not interfaces or not integration):
        raise ValueError('Integration tasks require interface_contracts and integration_test_commands')
    task['task_contract_version'] = 2


def check_test_isolation(repo, task, paths):
    if task.get('task_contract_version', 1) < 2:
        return
    from test_evidence import module_cache_writes
    for path in paths:
        name = PurePosixPath(path)
        if name.suffix != '.py' or not (name.name.startswith('test') or name.name.endswith('_test.py') or 'tests' in name.parts):
            continue
        state, data = repo.file(path)
        if not state['exists']:
            continue
        violations = module_cache_writes(data.decode('utf-8-sig'))
        if violations:
            raise ValueError('test_isolation_violation: ' + path + ' ' + json.dumps(violations))


def worker_env():
    key = load(CONFIG / 'credentials.json')['opencode_api_key']
    if not key or key.isspace():
        raise ValueError('Missing OpenCode credential')
    # Pass runtime/network basics, never inherit other providers' authentication.
    names = ('HOME', 'USER', 'LOGNAME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE',
             'SSL_CERT_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
             'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
    env = {k: os.environ[k] for k in names if k in os.environ}
    runtime = load(CONFIG / 'runtime.json') if (CONFIG / 'runtime.json').exists() else {}
    if runtime.get('http_proxy'):
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            env[name] = runtime['http_proxy']
    env.update({'OMP_OPENCODE_API_KEY': key, 'PYTHONDONTWRITEBYTECODE': '1'})
    return env


def summarize_events(run):
    from command_audit import CommandAudit
    from async_evidence import AsyncTracker, version_for_run
    audit = CommandAudit()
    from live_output import LiveOutputAudit
    live_output = LiveOutputAudit()
    live_observed = False
    from worker_bash import ExactBashAudit
    exact_bash = ExactBashAudit()
    exact_policy = validate_bash_policy(load(run/'task.json'), load(run/'metadata.json'), run) if (run/'task.json').exists() and (run/'metadata.json').exists() else None
    from bash_gate import for_run, audit_event, GateError
    gate, gate_summary = None, None
    try:
        gate = for_run(run)
        if gate:
            gate_summary = gate.refresh(complete=True)
    except (GateError, ValueError):
        gate = None
        gate_summary = {'status': 'invalid', 'scope': 'pre_execution_evidence_unverified'}
    async_tracker = AsyncTracker(version_for_run(run))
    async_observed = False
    types, texts, tools, models, errors = {}, [], [], set(), []
    for line in (run / 'events.jsonl').read_text(errors='replace').splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = item.get('type', 'unknown')
        audited = audit_event(item, gate)
        audit.event(audited)
        if exact_policy:
            exact_bash.event(audited)
        if 'liveOutputAudit' in item:
            live_observed = True
        live_output.event(audited)
        if item.get('asyncBash') is not None or item.get('asyncResult') is not None:
            async_observed = True
        async_tracker.event(item)
        types[typ] = types.get(typ, 0) + 1
        if typ == 'tool_execution_start':
            tools.append(item.get('toolName'))
        if typ == 'message_end':
            message = item.get('message', {})
            if message.get('model'):
                models.add(message['model'])
            if message.get('stopReason') in ('error', 'aborted'):
                errors.append(message.get('errorMessage', message.get('stopReason')))
            if message.get('role') == 'assistant':
                text_parts = [x.get('text', '') for x in message.get('content', []) if x.get('type') == 'text']
                if text_parts:
                    texts.append('\n'.join(text_parts))
    result = {'event_types': types, 'tools': tools, 'models': sorted(models),
              'errors': errors, 'final_text': texts[-1] if texts else '', 'command_audit': audit.summary()}
    if async_observed:
        result['async_execution'] = async_tracker.summary()
    if live_observed:
        result['live_output'] = live_output.summary()
    if exact_policy:
        result['exact_bash'] = exact_bash.summary()
    if gate_summary is not None:
        result['preexec_gate'] = gate_summary
    dump(run / 'command-audit.json', audit.summary())
    dump(run / 'worker-result.json', result)
    return result


def process_stamp(pid):
    r = cmd(['ps', '-p', str(pid), '-o', 'lstart='], check=False)
    return r.stdout.decode().strip() if r.returncode == 0 else None


def group_members(pid):
    result = {}
    output = cmd(['ps', '-axo', 'pid=,pgid=,stat=,lstart=']).stdout.decode()
    for line in output.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and fields[1] == str(pid) and not fields[2].startswith('Z'):
            result[int(fields[0])] = fields[3]
    return result


def stop_group(pid, stamp):
    # All callers launch a new session. On macOS getpgid() may reject an
    # unreaped zombie leader even though ps still verifies its creation stamp.
    if not stamp or process_stamp(pid) != stamp:
        raise RuntimeError('Process identity changed; refusing to signal')
    original = group_members(pid)
    if not original:
        return
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not group_members(pid):
            return
        time.sleep(0.1)
    remaining = group_members(pid)
    if remaining:
        if not any(original.get(p) == started for p, started in remaining.items()):
            raise RuntimeError('Group identity is no longer verifiable; inspect descendants')
        os.killpg(pid, signal.SIGKILL)


_UNSUPPLIED_EXECUTION_POLICY = object()


def validate_worker_options(task, resolve=False, expected_policy=_UNSUPPLIED_EXECUTION_POLICY):
    from progress_monitor import ProgressMonitor
    if resolve:
        from execution_policy import resolve_policy
        policy = resolve_policy(task)
        task.update(copy.deepcopy({k: v for k, v in policy.items() if k != 'policy_version'}))
        task['execution_policy'] = policy
        return
    # Existing/resumed runs keep their prepared budgets even if profiles change.
    frozen = task.get('execution_policy')
    # Omitted means a standalone validation; explicit None from metadata.get
    # means the persisted copy is missing/null, not permission to skip binding.
    if expected_policy is not _UNSUPPLIED_EXECUTION_POLICY and frozen != expected_policy:
        raise ValueError('Execution policy changed; prepare a fresh run')
    if frozen is not None:
        from execution_policy import resolve_policy
        if not isinstance(frozen, dict) or type(frozen.get('policy_version')) is not int or frozen.get('policy_version') not in (1, 2):
            raise ValueError('Unsupported execution policy; prepare a fresh run')
        keys = ('worker_thinking', 'budget_mode', 'progress', 'timeout_seconds', 'effort_assessment')
        if frozen['policy_version'] == 2:
            keys += ('closeout',)
        elif 'closeout' in task:
            raise ValueError('Legacy policy cannot acquire closeout; prepare a fresh run')
        for key in keys:
            if key not in task or task[key] != frozen.get(key):
                raise ValueError('Execution policy changed; prepare a fresh run')
        # Validate the saved numeric values as explicit overrides, not today's defaults.
        checked = resolve_policy(dict(task, budget_mode='custom'), version=frozen['policy_version'])
        checked['budget_mode'] = task['budget_mode']
        if task['budget_mode'] not in ('auto', 'custom') or checked != frozen:
            raise ValueError('Invalid frozen execution policy; prepare a fresh run')
        return
    # Legacy runs were prepared before policy snapshots existed. Do not migrate
    # them at launch: the historical monitor/worker defaults remain in effect.
    if 'closeout' in task:
        raise ValueError('Execution policy missing for closeout; prepare a fresh run')
    task.setdefault('worker_thinking', 'low')
    if task['worker_thinking'] not in ('low', 'high', 'max'):
        raise ValueError('DeepSeek supports worker_thinking low, high or max')
    ProgressMonitor(0, **task.get('progress', {}))


def rpc_python():
    if sys.version_info >= (3, 11):
        return sys.executable
    runtime = load(CONFIG / 'runtime.json') if (CONFIG / 'runtime.json').exists() else {}
    python = runtime.get('python')
    if not python or not Path(python).is_file():
        raise RuntimeError('OMP RPC requires Python 3.11+; configure runtime.json python or use Python 3.11+')
    cmd([python, '-c', 'import sys; assert sys.version_info >= (3, 11)'])
    return python


def worker(run_path):
    from rpc_worker import execute
    run, meta, task = run_state(run_path)
    with local_lock(run):
        if meta['process'] not in ('not_started', 'launching', 'launch_failed'):
            raise RuntimeError('Use resume or a new run for a new implementation attempt')
        try:
            validate_worker_options(task, expected_policy=meta.get('execution_policy'))
            validate_transport_policy(task, meta)
            validate_bash_policy(task, meta, run)
            selected_model = frozen_model(task, meta)
            env = worker_env()
            omp = shutil.which('omp')
            if not omp:
                raise RuntimeError('omp is not in PATH')
            argv = [omp, '--profile', 'codex-ds', '--cwd', str(run / 'snapshot'), '--model', selected_model,
                    '--thinking', task['worker_thinking'], '--mode', 'rpc', '--no-extensions',
                    '--no-skills', '--no-title', '--no-prewalk', '--no-lsp',
                    '--tools', 'read,grep,glob,edit,write,bash', '--session-dir', str(run / 'sessions')]
            if meta.get('resume_session'):
                argv += ['--resume', meta['resume_session']]
            meta['omp_version'] = cmd([omp, '--version']).stdout.decode().strip()
            from bash_gate import launch as launch_gate
            launch_gate(run, task, meta, env, argv, meta['omp_version'])
            dump(run / 'invocation.json', {'argv': argv, 'credential_env': 'OMP_OPENCODE_API_KEY'})
            def candidate_paths():
                return snapshot_changes(run, meta, task)
            state, code = execute(run, meta, task, env, argv, dump, process_stamp, candidate_paths,
                                  preflight=candidate_paths if has_snapshot_guard(meta, task) else None)
            result = summarize_events(run)
            result.update(worker_mode='rpc', thinking=task['worker_thinking'],
                          terminal_event=load(run / 'progress.json').get('terminal_event', False))
            termination = load(run / 'termination.json') if (run / 'termination.json').exists() else {}
            result['stop_reason'] = termination.get('reason')
            result['client_errors'] = termination.get('details', [])
            dump(run / 'worker-result.json', result)
            if state == 'exited' and (result['errors'] or not usable_final_text(result['final_text']) or
                                      result['models'] != [model_id(selected_model)] or not result['tools'] or
                                      not result['terminal_event']):
                state = 'failed'
                if not (run / 'failure.json').exists():
                    dump(run / 'failure.json', {'reason': 'worker_evidence_invalid', 'process': state})
            meta.update(process=state, exit_code=code, ended_at=time.time(),
                        delivery='pending_review' if state == 'exited' else 'worker_failed')
            if (run / 'failure.json').exists():
                meta['failure_reason'] = load(run / 'failure.json')['reason']
            dump(run / 'metadata.json', meta)
            return meta
        except Exception as error:
            meta.update(process='failed' if meta.get('worker_mode') == 'rpc' else 'launch_failed',
                        delivery='worker_failed', ended_at=time.time(), error_type=type(error).__name__)
            if (run / 'failure.json').exists():
                meta['failure_reason'] = load(run / 'failure.json')['reason']
            dump(run / 'metadata.json', meta)
            raise


def start(run_path):
    run, meta, task = run_state(run_path)
    with local_lock(run):
        if meta['process'] not in ('not_started', 'launch_failed'):
            raise RuntimeError('Run already started')
        validate_worker_options(task, expected_policy=meta.get('execution_policy'))
        validate_transport_policy(task, meta)
        validate_bash_policy(task, meta, run)
        frozen_model(task, meta)
        worker_env()
        python = rpc_python()
        if not shutil.which('omp'):
            raise RuntimeError('omp is not in PATH')
        meta['process'] = 'launching'
        dump(run / 'metadata.json', meta)
    try:
        with (run / 'supervisor.log').open('ab') as out:
            p = subprocess.Popen([python, '-B', str(Path(__file__).resolve()), 'run', '--run', str(run)],
                                 stdout=out, stderr=out, start_new_session=True)
        dump(run / 'supervisor.json', {'pid': p.pid, 'process_stamp': process_stamp(p.pid)})
    except Exception:
        meta.update(process='launch_failed', delivery='worker_failed', ended_at=time.time())
        dump(run / 'metadata.json', meta)
        raise
    return {'run': str(run), 'supervisor_pid': p.pid, 'process': 'launching'}


def status(run_path):
    run, meta, _ = run_state(run_path)
    if meta['process'] in ('launching', 'running'):
        owner = load(run / 'supervisor.json') if (run / 'supervisor.json').exists() else meta
        if not owner.get('pid') or process_stamp(owner['pid']) != owner.get('process_stamp'):
            meta['process'] = 'unknown_supervisor_state'
    if (run / 'progress.json').exists():
        meta['progress'] = load(run / 'progress.json')
        meta['progress']['age_seconds'] = max(0, time.time() - meta['progress']['updated_at'])
    if meta['process'] not in ('launching', 'running', 'not_started'):
        from run_diagnostics import diagnose_run
        meta['diagnostics'] = diagnose_run(run)
    return meta


def cancel(run_path):
    run, meta, _ = run_state(run_path)
    if meta['process'] not in ('running', 'launching'):
        raise RuntimeError('Worker is not active; inspect status')
    (run / 'cancel-requested').write_text('requested\n')
    # RPC supervisor observes the marker; old JSON workers need a group signal.
    if meta.get('worker_mode') != 'rpc' and meta['process'] == 'running':
        stop_group(meta['pid'], meta['process_stamp'])
    return {'run': str(run), 'cancel_requested': True, 'completed': False}


def steer(run_path, message_file):
    run, meta, _ = run_state(run_path)
    if meta['process'] != 'running' or meta.get('worker_mode') != 'rpc':
        raise RuntimeError('Steering requires a running RPC worker')
    message = Path(message_file).read_text().strip()
    if not message or len(message) > 16000:
        raise ValueError('Steering requires a nonempty message of at most 16000 characters')
    inbox = run / 'controls'
    inbox.mkdir(exist_ok=True, mode=0o700)
    request_id = uuid.uuid4().hex
    dump(inbox / (request_id + '.json'), {'message': message})
    return {'queued': True, 'request_id': request_id, 'completed': False}


def resume(run_path, message_file):
    from process_evidence import enabled, async_enabled, observed_blocked_requests, records, live_output_enabled
    run, meta, task = run_state(run_path)
    with local_lock(run):
        frozen_model(task, meta)
        exact_policy = validate_bash_policy(task, meta, run)
        if meta['process'] not in ('stalled', 'cancelled', 'timed_out', 'failed'):
            raise RuntimeError('Resume requires a stopped unsuccessful run')
        if meta.get('attempt', 1) >= 2:
            raise RuntimeError('Recovery budget exhausted; split the task and prepare a new run')
        reason = load(run / 'failure.json').get('reason') if (run / 'failure.json').exists() else None
        if reason == 'candidate_closeout_deadline':
            raise RuntimeError('Preserve candidate closeout failure; split into a fresh task or independent takeover')
        if enabled(task, meta):
            reason = load(run / 'failure.json').get('reason') if (run / 'failure.json').exists() else None
            if reason in ('process_constraint_request', 'undeclared_bash_request') or observed_blocked_requests(run, live_output_required=live_output_enabled(task, meta), exact_bash_policy=exact_policy):
                raise RuntimeError('Preserve prohibited-request failure; use a fresh task or independent takeover')
        from bash_gate import KEY as GATE_KEY
        if GATE_KEY in task:
            raise RuntimeError('Preserve pre-execution gate attempt and correction budget; prepare a fresh task '
                               'even with zero Bash calls or denials. Link the failed run and count this '
                               'against the coordinator recovery limit; same-run resume is unavailable.')
        if async_enabled(task, meta):
            from async_evidence import bash_async, async_notice
            for name in ('events.jsonl', 'tool-evidence.jsonl'):
                journal = run / name
                if journal.exists() or journal.is_symlink():
                    for item in records(journal)[1]:
                        if (item.get('asyncBash') is not None or item.get('asyncResult') is not None or
                                item.get('type') == 'tool_execution_end' and bash_async(item.get('result')) is not None or
                                item.get('type') == 'async_result' and async_notice(item.get('message')) is not None):
                            raise RuntimeError('Async attempt requires coordinator resource inspection and a fresh run')
        session = meta.get('session_file')
        if not session or not Path(session).is_file():
            raise RuntimeError('No saved RPC session to resume')
        message = Path(message_file).read_text().strip()
        if not message or len(message) > 16000:
            raise ValueError('Provide a bounded recovery instruction')
        validate_worker_options(task, expected_policy=meta.get('execution_policy'))
        repo = target_repo(task, meta)
        require_identity(repo.identity(), meta['source'])
        if repo.manifest(meta['paths']) != load(run / 'baseline.json'):
            raise RuntimeError('Target changed; prepare a fresh task')
        if has_snapshot_guard(meta, task):
            snapshot_changes(run, meta, task)
        # Copy the complete previous attempt before touching live metadata/evidence.
        # On any mutation failure restore from that archive; never move metadata away.
        original = (run / 'prompt.md').read_text()
        archive = run / ('attempt-' + str(meta.get('attempt', 1)))
        if archive.exists():
            raise RuntimeError('Recovery archive already exists; inspect before retrying')
        staging = run / ('.recovery-' + uuid.uuid4().hex)
        staging.mkdir()
        names = ('metadata.json', 'events.jsonl', 'worker-result.json', 'progress.json',
                 'stderr.log', 'invocation.json', 'rpc-state.json', 'watchdog-warning.json',
                 'termination.json', 'failure.json', 'startup.json', 'command-audit.json', 'tool-evidence.jsonl',
                 'cancel-requested', 'prompt.md', 'controls', 'supervisor.json', 'supervisor.log',
                 'closeout-warning.json')
        try:
            for name in names:
                path = run / name
                if path.exists():
                    if path.is_dir():
                        shutil.copytree(path, staging / name)
                    else:
                        shutil.copy2(path, staging / name)
            os.replace(staging, archive)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        new_meta = dict(meta, process='not_started', delivery='prepared', attempt=2, resume_session=session)
        for key in ('pid', 'process_stamp', 'exit_code', 'ended_at', 'error_type', 'failure_reason'):
            new_meta.pop(key, None)
        try:
            for name in names:
                if name in ('metadata.json', 'prompt.md'):
                    continue
                path = run / name
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            prompt_temp = run / '.recovery-prompt.tmp'
            prompt_temp.write_text(original + '\n\nCoordinator recovery instruction (original scope/acceptance still applies):\n' + message)
            os.replace(prompt_temp, run / 'prompt.md')
            dump(run / 'metadata.json', new_meta)
        except Exception:
            # Keep the complete archive if even restoration fails (e.g. disk failure).
            for name in names:
                saved = archive / name
                if saved.is_dir():
                    shutil.copytree(saved, run / name, dirs_exist_ok=True)
                elif saved.exists():
                    shutil.copy2(saved, run / name)
            shutil.rmtree(archive)
            raise
    return start(run)


def propose(run_path):
    run, meta, task = run_state(run_path)
    with local_lock(run):
        require_worker(run, meta)
        snapshot = run / 'snapshot'
        if has_snapshot_guard(meta, task):
            snapshot_changes(run, meta, task)
        git(snapshot, 'add', '-A')
        names = [x.decode() for x in git(snapshot, 'diff', '--cached', '--name-only', '-z',
                                         '--no-renames', meta['snapshot_commit']).split(b'\0') if x]
        outside = set(names) - set(task['allowed_paths'])
        if outside:
            raise ValueError('scope_violation: ' + ', '.join(sorted(outside)))
        local = Repo({'transport': 'local', 'repo': str(snapshot)})
        check_test_isolation(local, task, names)
        expected = local.manifest(meta['paths'])
        patch = git(snapshot, 'diff', '--cached', '--no-ext-diff', '--no-renames', '--binary',
                    meta['snapshot_commit'])
        (run / 'changes.patch').write_bytes(patch)
        proposal = {'paths': names, 'patch_sha256': digest(patch), 'expected': expected}
        dump(run / 'proposal.json', proposal)
        meta.update(delivery='proposed')
        dump(run / 'metadata.json', meta)
        return proposal


def target_lock(repo):
    common = repo.text(['git', 'rev-parse', '--git-common-dir'])
    return str(PurePosixPath(common) / 'team-dev-apply.lock')


def apply(run_path):
    run, meta, task = run_state(run_path)
    with local_lock(run):
        require_worker(run, meta)
        repo = target_repo(task, meta)
        identity = repo.identity()
        require_identity(identity, meta['source'])
        proposal, baseline = load(run / 'proposal.json'), load(run / 'baseline.json')
        patch = (run / 'changes.patch').read_bytes()
        if digest(patch) != proposal['patch_sha256']:
            raise ValueError('Patch changed since propose')
        lock = target_lock(repo)
        repo.run(['mkdir', lock])
        try:
            current = repo.manifest(meta['paths'])
            intent = load(run / 'apply-intent.json') if (run / 'apply-intent.json').exists() else {}
            if current == proposal['expected'] and (meta.get('applied') or intent.get('patch_sha256') == proposal['patch_sha256']):
                meta.update(applied=True, applied_fingerprint=fingerprint(current), delivery='pending_review')
                dump(run / 'applied.json', {'manifest': current, 'fingerprint': fingerprint(current),
                                           'source': identity, 'at': time.time(), 'recovered': True})
                dump(run / 'metadata.json', meta)
                return {'already_applied': True, 'fingerprint': fingerprint(current)}
            if repo.text(['git', 'rev-parse', 'HEAD']) != meta['source']['head'] or current != baseline:
                raise RuntimeError('baseline_conflict: target changed; prepare a fresh run')
            dump(run / 'before-apply.json', current)
            dump(run / 'apply-intent.json', {'patch_sha256': proposal['patch_sha256'], 'at': time.time()})
            if patch:
                repo.run(['git', 'apply', '--check', '--binary', '-'], data=patch)
                repo.run(['git', 'apply', '--binary', '-'], data=patch)
            actual = repo.manifest(meta['paths'])
            require_identity(repo.identity(), meta['source'])
            if actual != proposal['expected']:
                raise RuntimeError('post_apply_mismatch: preserve target and investigate')
            meta.update(applied=True, applied_fingerprint=fingerprint(actual), delivery='pending_review')
            dump(run / 'applied.json', {'manifest': actual, 'fingerprint': fingerprint(actual),
                                       'source': repo.identity(), 'at': time.time()})
            dump(run / 'metadata.json', meta)
            return {'applied': True, 'paths': proposal['paths'], 'fingerprint': fingerprint(actual)}
        finally:
            repo.run(['rmdir', lock])


def test_target(run_path):
    from process_evidence import enabled, validate_test_argv
    run, meta, task = run_state(run_path)
    with local_lock(run):
        require_worker(run, meta)
        validate_transport_policy(task, meta)
        if not meta.get('applied'):
            raise RuntimeError('Apply and verify the target first')
        # Never replay unresolved evidence; inspect it and hand off instead.
        target_test_receipts.require_retest_allowed(run)
        repo = target_repo(task, meta)
        strict_evidence = enabled(task, meta)
        meta['delivery'] = 'testing'
        dump(run / 'metadata.json', meta)
        previous = run / 'tests.json'
        if previous.exists():
            archive = run / ('tests-before-' + uuid.uuid4().hex)
            archive.mkdir()
            os.replace(previous, archive / 'tests.json')
            for pattern in ('test-*.stdout', 'test-*.stderr'):
                for path in run.glob(pattern):
                    os.replace(path, archive / path.name)
        applied = load(run / 'applied.json')
        commands = task.get('test_commands', [])
        if not commands:
            raise ValueError('No target test commands supplied')
        if strict_evidence:
            for argv in commands:
                validate_test_argv(argv)
        for argv in commands:
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                raise ValueError('Test commands must be argument arrays')
        attempt = target_test_receipts.begin(run, task, applied)
        seconds = min(max(int(task.get('test_timeout_seconds', 60)), 1), 300)
        results = []
        stage = 'preflight_identity'
        command_index = None
        try:
            # Durable run-level mark: later gates reject a run whose attempt records vanish.
            meta[target_test_receipts.RUN_MARKER] = target_test_receipts.RUN_MARKER_VERSION
            dump(run / 'metadata.json', meta)
            identity_before = repo.identity()
            attempt.stage(target_test_receipts.BEFORE_IDENTITY, {'identity': identity_before})
            require_identity(identity_before, applied['source'])
            require_identity(applied['source'], meta['source'])
            stage = 'before_manifest'
            before = repo.manifest(meta['paths'])
            before_fingerprint = fingerprint(before)
            attempt.stage(target_test_receipts.BEFORE_MANIFEST, {
                'fingerprint': before_fingerprint,
                'applied_fingerprint': meta['applied_fingerprint'],
                'matches': before_fingerprint == meta['applied_fingerprint']})
            if before_fingerprint != meta['applied_fingerprint']:
                raise RuntimeError('Target changed since apply')
            stage = 'isolation'
            check_test_isolation(repo, task, task['allowed_paths'])
            stage = 'command'
            for i, argv in enumerate(commands):
                command_index = i
                # On SSH targets require existing coreutils timeout, never install a daemon.
                actual = ['env', 'PYTHONDONTWRITEBYTECODE=1'] + argv
                if repo.remote:
                    actual = ['timeout', '--signal=TERM', '--kill-after=3s', str(seconds)] + actual
                attempt.command_intent(i, argv, actual)
                timed_out = False
                try:
                    r = repo.run(actual, timeout=seconds + 15, check=False) if repo.remote else local_test(actual, repo.repo, seconds)
                    code, out, err = r.returncode, r.stdout, r.stderr
                except subprocess.TimeoutExpired as error:
                    # Preserve received bytes while explicitly withholding a target exit code.
                    timed_out = True
                    code, out, err = None, error.stdout or b'', error.stderr or b''
                    err += b'\nlocal transport deadline exceeded; target result unknown'
                observed_code = None if timed_out else code
                transport_complete = not timed_out and not (repo.remote and code == 255)
                declared_exit = code if transport_complete else None
                # Persist the observed result and both streams before any next step.
                attempt.command_result(
                    i, transport='ssh' if repo.remote else 'local',
                    transport_complete=transport_complete,
                    declared_exit_code=declared_exit,
                    transport_returncode=observed_code, stdout=out, stderr=err)
                (run / ('test-%d.stdout' % i)).write_bytes(out)
                (run / ('test-%d.stderr' % i)).write_bytes(err)
                evidence = {'passed': True}
                if task.get('task_contract_version', 1) >= 2:
                    from test_evidence import unittest_evidence
                    evidence = unittest_evidence(argv, out.decode(errors='replace'), err.decode(errors='replace'))
                result = {'argv': argv, 'exit_code': declared_exit, 'stdout': 'test-%d.stdout' % i,
                          'stderr': 'test-%d.stderr' % i, 'collection': evidence}
                if strict_evidence:
                    result.update(stdout_sha256=digest(out), stderr_sha256=digest(err),
                                  transport_complete=transport_complete,
                                  transport_returncode=observed_code,
                                  exit_scope='declared_test_process')
                results.append(result)
            command_index = None
            stage = 'post_manifest'
            after = repo.manifest(meta['paths'])
            after_fingerprint = fingerprint(after)
            attempt.stage(target_test_receipts.POST_MANIFEST, {
                'fingerprint': after_fingerprint, 'unchanged': before == after})
            stage = 'post_identity'
            identity = repo.identity()
            attempt.stage(target_test_receipts.POST_IDENTITY, {'identity': identity})
            require_identity(identity, applied['source'])
            stage = 'publish'
            report = {'host': identity, 'fingerprint': after_fingerprint, 'unchanged': before == after,
                      'passed': before == after and all(x['exit_code'] == 0 and x['collection']['passed'] for x in results), 'tests': results}
            if strict_evidence:
                report['evidence_version'] = 1
            dump(run / 'tests.json', report)
            meta['delivery'] = 'pending_review' if report['passed'] else 'tests_failed'
            dump(run / 'metadata.json', meta)
            # The completion marker is written last, after the published report.
            attempt.complete(run / 'tests.json')
            return report
        except BaseException as error:
            # Preserve the original exception; only append evidence that is reachable.
            try:
                attempt.failure(stage, error, command_index)
            except Exception:
                pass
            try:
                meta['delivery'] = 'tests_incomplete'
                dump(run / 'metadata.json', meta)
            except Exception:
                pass
            raise


def record_review(run_path, review_path):
    from process_evidence import enabled, validate_review, validate_test_logs
    import reviewer_provenance
    run, meta, task = run_state(run_path)
    with local_lock(run):
        require_worker(run, meta)
        strict_provenance = reviewer_provenance.enabled(task, meta)
        review = load(review_path)
        if review.get('model') != 'gpt-5.6-sol' or review.get('verdict') not in ('pass', 'changes_requested', 'blocked'):
            raise ValueError('Require a Sol review with an explicit verdict')
        if review.get('fingerprint') != meta.get('applied_fingerprint'):
            raise ValueError('Review must bind to the applied fingerprint')
        if review.get('verdict') == 'pass':
            # New receipt evidence is acceptable only as a complete capture of this report.
            target_test_receipts.require_report_receipt(run)
            if enabled(task, meta):
                validate_test_logs(run, task, load(run / 'tests.json'))
                validate_review(run, task, meta, review)
        if review.get('verdict') == 'pass' and strict_provenance:
            reviewer_provenance.validate(run, review)
        # Invalidate a previous verification before changing review evidence.
        meta['delivery'] = 'pending_finalization' if review['verdict'] == 'pass' else review['verdict']
        dump(run / 'metadata.json', meta)
        dump(run / 'review.json', review)
        return review


def finalize(run_path):
    from process_evidence import enabled, validate_review, validate_test_logs
    import reviewer_provenance
    run, meta, task = run_state(run_path)
    with local_lock(run):
        previous_delivery = meta.get('delivery')
        strict_evidence = enabled(task, meta)
        strict_provenance = reviewer_provenance.enabled(task, meta)
        validate_transport_policy(task, meta)
        if (strict_evidence or strict_provenance) and previous_delivery in ('pending_finalization', 'verified'):
            meta['delivery'] = 'verification_pending'
            dump(run / 'metadata.json', meta)
        require_worker(run, meta)
        tests, review = load(run / 'tests.json'), load(run / 'review.json')
        target_test_receipts.require_report_receipt(run)
        if strict_evidence:
            validate_test_logs(run, task, tests)
            validate_review(run, task, meta, review)
        if strict_provenance:
            reviewer_provenance.validate(run, review)
        repo = target_repo(task, meta)
        identity = repo.identity()
        require_identity(identity, load(run / 'applied.json')['source'])
        require_identity(identity, meta['source'])
        require_identity(identity, tests['host'])
        current = fingerprint(repo.manifest(meta['paths']))
        expected = meta.get('applied_fingerprint')
        if not (previous_delivery in ('pending_finalization', 'verified') and
                tests['passed'] and review['verdict'] == 'pass' and
                current == expected == tests['fingerprint'] == review['fingerprint']):
            raise RuntimeError('Verification gates failed or target changed')
        meta['delivery'] = 'verified'
        dump(run / 'metadata.json', meta)
        return meta


def local_test(argv, cwd, seconds):
    # Avoid communicate()/poll() reaping the leader while inherited-pipe descendants
    # still run. Keep its unreaped PID as group identity until all live members stop.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=err, start_new_session=True)
        stamp = process_stamp(p.pid)
        deadline = time.monotonic() + seconds
        timed_out = False
        try:
            while group_members(p.pid):
                if time.monotonic() >= deadline:
                    timed_out = True
                    stop_group(p.pid, stamp)
                    break
                time.sleep(0.05)
            code = p.wait(timeout=5)
        except BaseException:
            stop_group(p.pid, stamp)
            p.wait(timeout=5)
            raise
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(argv, 124 if timed_out else code, out.read(),
                                           err.read() + (b'\nlocal test deadline exceeded' if timed_out else b''))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['doctor', 'policy', 'prepare', 'run', 'start', 'status', 'cancel',
                                     'propose', 'apply', 'test', 'record-review', 'finalize', 'steer', 'resume', 'diagnose', 'process-evidence', 'test-receipts'])
    p.add_argument('--task')
    p.add_argument('--run')
    p.add_argument('--review')
    p.add_argument('--message-file')
    a = p.parse_args()
    actions = {'run': worker, 'start': start, 'cancel': cancel, 'propose': propose,
               'apply': apply, 'test': test_target, 'finalize': finalize}
    try:
        if a.action == 'run' and sys.version_info < (3, 11):
            os.execv(rpc_python(), [rpc_python(), '-B', str(Path(__file__).resolve()), *sys.argv[1:]])
        if a.action in ('steer', 'resume'):
            result = {'steer': steer, 'resume': resume}[a.action](a.run, a.message_file)
        elif a.action == 'doctor':
            result = {'omp': shutil.which('omp'), 'credential_file_exists': (CONFIG / 'credentials.json').exists(),
                      'model': MODEL, 'agent_runtime': 'local', 'remote_omp_required': False}
        elif a.action == 'policy':
            from execution_policy import resolve_policy
            result = resolve_policy(load(a.task))
        elif a.action == 'prepare':
            result = prepare(a.task)
        elif a.action == 'status':
            result = status(a.run)
        elif a.action == 'diagnose':
            from run_diagnostics import diagnose_run
            result = diagnose_run(a.run)
        elif a.action == 'process-evidence':
            from process_evidence import enabled, async_enabled, binding, live_output_enabled
            run, meta, task = run_state(a.run)
            exact_policy = validate_bash_policy(task, meta, run)
            result = binding(run, async_required=async_enabled(task, meta), live_output_required=live_output_enabled(task, meta), exact_bash_policy=exact_policy) if enabled(task, meta) else {'enabled': False, 'reason': 'legacy_run'}
        elif a.action == 'test-receipts':
            # Local receipt bytes only: no Repo, SSH, config or provider access.
            result = target_test_receipts.test_receipts(a.run)
        elif a.action == 'record-review':
            result = record_review(a.run, a.review)
        else:
            result = actions[a.action](a.run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({'error': str(e), 'type': type(e).__name__}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
