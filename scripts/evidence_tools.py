"""Read-only recovery index and review package over saved coordinator run evidence.

This tool never inspects live processes, executes tools, uses the network or
writes inside a run. It reads only a fixed allowlist of evidence filenames from
each explicitly named run and reports *recorded* state. It never asserts current
worker liveness, current target identity or scientific acceptance.

    python3 -B scripts/evidence_tools.py index --manifest manifest.json [--output index.json]
    python3 -B scripts/evidence_tools.py bundle --run /absolute/run [--output package.json]
    python3 -B scripts/evidence_tools.py invocations --run /absolute/run [--output ledger.json]

An index is navigation, not acceptance. A bundle is a read-only view of stored
evidence, not a fresh verification of the target or of the recorded verdict. A
ledger joins stored request/gate/terminal records and retained worker test logs;
it never attests execution, test success or acceptance.
"""
import argparse
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

import invocation_async

JSON_LIMIT = 1024 * 1024
BLOB_LIMIT = 4 * 1024 * 1024
MAX_RUNS = 16
MAX_TEST_PAIRS = 16
UNKNOWN = 'unknown'

# Fixed allowlist: (basename, byte limit, parse as JSON).
EVIDENCE_FILES = (
    ('metadata.json', JSON_LIMIT, True),
    ('task.json', JSON_LIMIT, True),
    ('proposal.json', JSON_LIMIT, True),
    ('applied.json', JSON_LIMIT, True),
    ('tests.json', JSON_LIMIT, True),
    ('review.json', JSON_LIMIT, True),
    ('failure.json', JSON_LIMIT, True),
    ('termination.json', JSON_LIMIT, True),
    ('progress.json', JSON_LIMIT, True),
    ('command-audit.json', JSON_LIMIT, True),
    ('worker-result.json', JSON_LIMIT, False),  # pointer/hash only
    ('changes.patch', BLOB_LIMIT, False),
)
REQUIRED = ('metadata.json', 'task.json')
WORKFLOW = ('proposal.json', 'applied.json', 'tests.json', 'review.json')
# Fixed canonical log basenames: exactly test-0 .. test-15 for each stream kind.
# No other basename is read, so no unbounded digit string is ever converted.
LOG_BASENAMES = {kind: frozenset('test-%d.%s' % (index, kind)
                                 for index in range(MAX_TEST_PAIRS))
                 for kind in ('stdout', 'stderr')}
_DIGEST_CHARS = frozenset('0123456789abcdefABCDEF')


def _issue(kind, detail, severity='error'):
    return {'kind': kind, 'detail': detail, 'severity': severity}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(token):
    raise ValueError('non-finite JSON constant: ' + token)


def _nonfinite(value):
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_nonfinite(item) for item in value.values())
    if isinstance(value, list):
        return any(_nonfinite(item) for item in value)
    return False


def _strict_json(raw):
    """Parse JSON, rejecting duplicate keys and every non-finite number."""
    value = json.loads(raw, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
    if _nonfinite(value):
        raise ValueError('non-finite JSON number')
    return value


def _recorded_digest(value):
    """Classify a recorded log digest; never invent a mismatch for absent data."""
    if value is None:
        return UNKNOWN, 'absent', None
    projection = value if isinstance(value, str) else UNKNOWN
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _DIGEST_CHARS:
        return projection, 'malformed', None
    return value, 'present', value.lower()


def _read_bytes(path, limit, dir_fd=None):
    """Read one regular file through a single no-follow descriptor, bounded by limit."""
    try:
        descriptor = os.open(os.fspath(path),
                             os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0),
                             dir_fd=dir_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            return None, 'symlink'
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return None, 'missing'
        if error.errno == errno.EISDIR:
            return None, 'nonregular'
        return None, 'unreadable'
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, 'nonregular'
        if info.st_size > limit:
            return None, 'oversized'
        chunks, remaining = [], limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None, 'unreadable'
    finally:
        os.close(descriptor)
    raw = b''.join(chunks)
    if len(raw) > limit:
        return None, 'oversized'
    return raw, None


def load_run_evidence(run_path):
    """Read only the fixed evidence allowlist from one explicit run directory."""
    run = Path(run_path)
    files, issues = {}, []
    if not run.is_dir():
        issues.append(_issue('run', 'run path is missing or not a directory'))
        return {'run': str(run), 'files': files, 'issues': issues}
    for name, limit, parse in EVIDENCE_FILES:
        record = {'file': name, 'status': 'read', 'sha256': None, 'bytes': None, 'document': None}
        raw, problem = _read_bytes(run / name, limit)
        if problem is not None:
            record['status'] = problem
            detail = name + ': ' + problem
            if name in REQUIRED:
                issues.append(_issue('required', detail))
            elif problem == 'missing' and name in WORKFLOW:
                issues.append(_issue('gap', detail, severity='gap'))
            elif problem != 'missing':
                issues.append(_issue('unsafe', detail))
            files[name] = record
            continue
        record['sha256'], record['bytes'] = _digest(raw), len(raw)
        if parse:
            try:
                document = _strict_json(raw)
            except (ValueError, RecursionError):
                document = None
            if not isinstance(document, dict):
                record['status'] = 'invalid'
                issues.append(_issue('invalid', name + ': not a JSON object'))
            else:
                record['document'] = document
        files[name] = record
    return {'run': str(run), 'files': files, 'issues': issues}


def _document(evidence, name):
    record = evidence['files'].get(name)
    return record['document'] if record else None


def metadata_issues(meta):
    if not isinstance(meta, dict):
        return [_issue('required', 'metadata.json: missing or not an object')]
    issues = []
    if not isinstance(meta.get('source'), dict):
        issues.append(_issue('field', 'metadata.source: expected object'))
    paths = meta.get('paths')
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        issues.append(_issue('field', 'metadata.paths: expected list of strings'))
    for field in ('process', 'delivery'):
        value = meta.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(_issue('field', 'metadata.%s: expected nonblank string' % field))
    return issues


def task_issues(task):
    if not isinstance(task, dict):
        return [_issue('required', 'task.json: missing or not an object')]
    issues = []
    if not isinstance(task.get('target'), dict):
        issues.append(_issue('field', 'task.target: expected object'))
    goal = task.get('goal')
    if not isinstance(goal, str) or not goal.strip():
        issues.append(_issue('field', 'task.goal: expected nonblank string'))
    allowed = task.get('allowed_paths')
    if not isinstance(allowed, list) or not allowed or any(not isinstance(p, str) or not p for p in allowed):
        issues.append(_issue('field', 'task.allowed_paths: expected nonempty strings'))
    commands = task.get('test_commands')
    if (not isinstance(commands, list) or not commands or
            any(not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv)
                for argv in commands)):
        issues.append(_issue('field', 'task.test_commands: expected argv string arrays'))
    return issues


def _fingerprints(evidence):
    def field(name, key):
        doc = _document(evidence, name)
        value = doc.get(key) if isinstance(doc, dict) else None
        return value if isinstance(value, str) and value else UNKNOWN
    return {'candidate_patch_sha256': field('proposal.json', 'patch_sha256'),
            'applied': field('applied.json', 'fingerprint'),
            'metadata_applied': field('metadata.json', 'applied_fingerprint'),
            'tests': field('tests.json', 'fingerprint'),
            'review': field('review.json', 'fingerprint')}


def _conflicts(evidence, fingerprints):
    conflicts = []
    # Every known fingerprint describes the applied target identity, so compare
    # them pairwise even when an intermediate workflow file is absent.
    named = (('metadata_applied', 'metadata.applied_fingerprint'),
             ('applied', 'applied.fingerprint'),
             ('tests', 'tests.fingerprint'),
             ('review', 'review.fingerprint'))
    for position, (left, left_name) in enumerate(named):
        for right, right_name in named[position + 1:]:
            left_value, right_value = fingerprints[left], fingerprints[right]
            if UNKNOWN not in (left_value, right_value) and left_value != right_value:
                conflicts.append(_issue('conflict', '%s differs from %s' % (left_name, right_name)))
    patch = evidence['files'].get('changes.patch', {})
    candidate = fingerprints['candidate_patch_sha256']
    if isinstance(patch.get('sha256'), str) and candidate not in (UNKNOWN, patch['sha256']):
        conflicts.append(_issue('conflict', 'proposal.patch_sha256 differs from changes.patch bytes'))
    return conflicts


def _test_receipts(run, tests_doc, issues):
    receipts = []
    entries = tests_doc.get('tests') if isinstance(tests_doc, dict) else None
    if entries is None:
        return receipts
    if not isinstance(entries, list):
        issues.append(_issue('field', 'tests.tests: expected list'))
        return receipts
    if len(entries) > MAX_TEST_PAIRS:
        issues.append(_issue('limit', 'recorded tests exceed the fixed test-0..test-15 log range'))
    for entry in entries[:MAX_TEST_PAIRS]:
        if not isinstance(entry, dict):
            issues.append(_issue('field', 'tests.tests entry: expected object'))
            continue
        argv = entry.get('argv')
        code = entry.get('exit_code')
        collection = entry.get('collection')
        item = {'argv': argv if isinstance(argv, list) and all(isinstance(x, str) for x in argv) else UNKNOWN,
                'exit_code': code if isinstance(code, int) and not isinstance(code, bool) else UNKNOWN,
                'collection_passed': collection.get('passed') if isinstance(collection, dict) else UNKNOWN}
        for kind in ('stdout', 'stderr'):
            name = entry.get(kind)
            if not isinstance(name, str) or name not in LOG_BASENAMES[kind]:
                item[kind] = {'file': name if isinstance(name, str) else UNKNOWN,
                              'issue': 'not a canonical test-0..test-15 log name'}
                issues.append(_issue('path', 'tests.%s reference is not a canonical log name: %r'
                                     % (kind, name)))
                continue
            raw, problem = _read_bytes(run / name, BLOB_LIMIT)
            if problem is not None:
                item[kind] = {'file': name, 'issue': problem}
                issues.append(_issue('log', name + ': ' + problem))
                continue
            observed = _digest(raw)
            expected, state, normalized = _recorded_digest(entry.get(kind + '_sha256'))
            digest_status = {'absent': UNKNOWN, 'malformed': 'malformed',
                             'present': 'mismatch' if normalized != observed else 'match'}[state]
            item[kind] = {'file': name, 'sha256': observed, 'bytes': len(raw),
                          'expected_sha256': expected, 'digest_status': digest_status}
            if state == 'absent':
                issues.append(_issue('digest', name + ': recorded digest is absent; observed bytes '
                                     'hashed as ' + observed, severity='gap'))
            elif state == 'malformed':
                issues.append(_issue('digest', name + ': recorded digest is malformed'))
            elif digest_status == 'mismatch':
                issues.append(_issue('conflict', name + ': recorded digest differs from observed bytes'))
        receipts.append(item)
    return receipts


def _blocking(issues):
    """True when a visible error or workflow/digest gap keeps the view incomplete."""
    return any(issue['severity'] in ('error', 'gap') for issue in issues)


def describe_run(run_path):
    """Gather the fixed-evidence view of one run without mutating it."""
    run = Path(run_path)
    evidence = load_run_evidence(run)
    docs = {name: record['document'] for name, record in evidence['files'].items()}
    issues = list(evidence['issues'])
    meta, task = docs.get('metadata.json'), docs.get('task.json')
    if isinstance(meta, dict):
        issues.extend(metadata_issues(meta))
    if isinstance(task, dict):
        issues.extend(task_issues(task))
    fingerprints = _fingerprints(evidence)
    issues.extend(_conflicts(evidence, fingerprints))
    receipts = _test_receipts(run, docs.get('tests.json'), issues)
    return {'run': run, 'evidence': evidence, 'docs': docs, 'issues': issues,
            'fingerprints': fingerprints, 'receipts': receipts}


def _pointers(evidence):
    return [{'file': record['file'], 'status': record['status'],
             'sha256': record['sha256'], 'bytes': record['bytes']}
            for record in evidence['files'].values()]


def _suggestions(meta, failure):
    process = meta.get('process') if isinstance(meta, dict) else None
    delivery = meta.get('delivery') if isinstance(meta, dict) else None
    out = []
    if process in ('running', 'launching'):
        out.append('Recorded process is %s: inspect the existing run; do not start a duplicate or '
                   'assume current liveness.' % process)
    if delivery == 'verified':
        out.append('Recorded delivery is verified: reconcile the current target and acceptance; '
                   'never auto-skip re-verification.')
    if delivery == 'tests_failed' or isinstance(failure, dict):
        out.append('A failed candidate is recorded: inspect the preserved candidate and failure '
                   'evidence before any retry.')
    if not out:
        out.append('Recorded status is historical only: verify the current target before reusing this run.')
    return out


def _run_entry(run_path):
    described = describe_run(run_path)
    evidence, docs = described['evidence'], described['docs']
    meta, task = docs.get('metadata.json'), docs.get('task.json')
    issues = described['issues']
    meta_ok, task_ok = isinstance(meta, dict), isinstance(task, dict)
    if meta_ok and task_ok:
        suggestions = _suggestions(meta, docs.get('failure.json'))
    else:
        suggestions = ['Required evidence is missing or invalid: recorded state is unknown; '
                       'inspect the run manually.']
    return {
        'run': str(described['run']),
        'evidence_complete': bool(meta_ok and task_ok and not _blocking(issues)),
        'recorded': {
            'process': meta.get('process') if meta_ok and isinstance(meta.get('process'), str) else UNKNOWN,
            'delivery': meta.get('delivery') if meta_ok and isinstance(meta.get('delivery'), str) else UNKNOWN,
            'worker_model': meta.get('worker_model', UNKNOWN) if meta_ok else UNKNOWN,
            'snapshot_commit': meta.get('snapshot_commit', UNKNOWN) if meta_ok else UNKNOWN,
            'source': meta.get('source') if meta_ok and isinstance(meta.get('source'), dict) else UNKNOWN,
        },
        'task': {
            'target': task.get('target') if task_ok and isinstance(task.get('target'), dict) else UNKNOWN,
            'goal': task.get('goal') if task_ok and isinstance(task.get('goal'), str) else UNKNOWN,
            'allowed_paths': task.get('allowed_paths') if task_ok and isinstance(task.get('allowed_paths'), list) else UNKNOWN,
            'test_commands': task.get('test_commands') if task_ok and isinstance(task.get('test_commands'), list) else UNKNOWN,
        },
        'fingerprints': described['fingerprints'],
        'evidence': _pointers(evidence),
        'test_receipts': described['receipts'],
        'issues': issues,
        'suggested_next_inspection': suggestions,
    }


def _load_manifest(path):
    manifest = Path(path)
    raw, problem = _read_bytes(manifest, JSON_LIMIT)
    if problem is not None:
        raise ValueError('manifest ' + problem)
    try:
        value = _strict_json(raw)
    except (ValueError, RecursionError):
        raise ValueError('manifest is not valid JSON')
    if not isinstance(value, dict):
        raise ValueError('manifest must be a JSON object')
    return value, _digest(raw)


def _canonical(path):
    return os.path.realpath(os.fspath(path))


def _validated_runs(manifest):
    if type(manifest.get('version')) is not int or manifest['version'] != 1:
        raise ValueError('manifest.version must be the integer 1')
    agent = manifest.get('coordinator_agent_id')
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError('manifest.coordinator_agent_id must be a nonblank string')
    slices = manifest.get('slices')
    if not isinstance(slices, list) or not slices:
        raise ValueError('manifest.slices must be a nonempty list')
    seen_ids, seen_runs, total = set(), {}, 0
    for entry in slices:
        if not isinstance(entry, dict):
            raise ValueError('each slice must be a JSON object')
        ident = entry.get('id')
        if not isinstance(ident, str) or not ident.strip():
            raise ValueError('each slice id must be a nonblank string')
        if ident in seen_ids:
            raise ValueError('duplicate slice id: ' + ident)
        seen_ids.add(ident)
        runs = entry.get('runs')
        if not isinstance(runs, list) or not runs:
            raise ValueError('each slice must name a nonempty run list')
        for reference in runs:
            if not isinstance(reference, str) or not reference:
                raise ValueError('each run reference must be a nonempty string')
            if not Path(reference).is_absolute():
                raise ValueError('each run reference must be an absolute path')
            canonical = _canonical(reference)
            if canonical in seen_runs:
                raise ValueError('duplicate run reference: ' + reference)
            seen_runs[canonical] = reference
        total += len(runs)
    if total > MAX_RUNS:
        raise ValueError('manifest names more than %d runs' % MAX_RUNS)
    return slices


def recovery_index(manifest_path):
    """Return a read-only navigation index over explicitly named runs."""
    manifest, manifest_sha = _load_manifest(manifest_path)
    slices = _validated_runs(manifest)
    return {
        'tool': 'evidence_tools.recovery_index',
        'index_version': 1,
        'manifest': {'path': str(Path(manifest_path)), 'sha256': manifest_sha},
        'coordinator_agent_id': manifest['coordinator_agent_id'],
        'recorded_evidence_only': True,
        'notes': [
            'Every entry reports recorded evidence bytes only; no process, target, network or '
            'acceptance check was performed.',
            'current_liveness_verified/current_target_verified/acceptance_certified apply to every entry.',
        ],
        'limits': {'max_runs': MAX_RUNS, 'max_test_log_pairs': MAX_TEST_PAIRS,
                   'json_bytes': JSON_LIMIT, 'blob_bytes': BLOB_LIMIT},
        'slices': [{'id': entry['id'], 'runs': [_run_entry(r) for r in entry['runs']]}
                   for entry in slices],
        'current_liveness_verified': False,
        'current_target_verified': False,
        'acceptance_certified': False,
    }


def review_package(run_path):
    """Return one read-only indexed view of a run's stored review evidence."""
    described = describe_run(run_path)
    evidence, docs = described['evidence'], described['docs']
    meta, task = docs.get('metadata.json'), docs.get('task.json')
    review, tests = docs.get('review.json'), docs.get('tests.json')
    issues = described['issues']
    meta_ok, task_ok = isinstance(meta, dict), isinstance(task, dict)
    complete = bool(meta_ok and task_ok and not _blocking(issues))

    def evidence_view(name):
        record = evidence['files'].get(name)
        if record is None:
            return None
        if record['status'] != 'read':
            return {'file': name, 'status': record['status']}
        return {'file': name, 'sha256': record['sha256'], 'bytes': record['bytes']}

    def pointer(name):
        return evidence_view(name)

    tests_view = None
    if isinstance(tests, dict):
        tests_view = dict(pointer('tests.json'), host=tests.get('host', UNKNOWN),
                          fingerprint=tests.get('fingerprint', UNKNOWN),
                          unchanged=tests.get('unchanged', UNKNOWN),
                          passed=tests.get('passed', UNKNOWN),
                          receipts=described['receipts'])
    else:
        tests_view = pointer('tests.json')

    review_view = None
    if isinstance(review, dict):
        findings = review.get('findings')
        review_view = dict(pointer('review.json'),
                           recorded_verdict=review.get('verdict', UNKNOWN),
                           fingerprint=review.get('fingerprint', UNKNOWN),
                           findings_count=len(findings) if isinstance(findings, list) else UNKNOWN,
                           process_evidence=review.get('process_evidence', UNKNOWN),
                           reviewer_provenance_present=isinstance(review.get('reviewer_provenance'), dict))
    else:
        review_view = pointer('review.json')

    process_evidence = {}
    for name, fields in (('failure.json', ('reason',)),
                         ('termination.json', ('reason', 'terminal_event')),
                         ('progress.json', ('terminal_event', 'updated_at')),
                         ('command-audit.json', ())):
        view = pointer(name)
        if view is not None and 'sha256' in view:
            doc = _document(evidence, name) or {}
            view['fields'] = {key: doc.get(key, UNKNOWN) for key in fields}
        process_evidence[name] = view

    worker_report = pointer('worker-result.json')
    if worker_report is not None:
        worker_report['authoritative_output'] = False
        worker_report['note'] = 'pointer/hash only; the worker final report is not authoritative output'

    return {
        'tool': 'evidence_tools.review_package',
        'package_version': 1,
        'run': {'path': str(described['run']), 'recorded_from': 'saved evidence files only'},
        'complete': complete,
        'task': {
            'goal': task.get('goal', UNKNOWN) if task_ok else UNKNOWN,
            'acceptance': task.get('acceptance', UNKNOWN) if task_ok else UNKNOWN,
            'interface_contracts': task.get('interface_contracts', UNKNOWN) if task_ok else UNKNOWN,
            'allowed_paths': task.get('allowed_paths', UNKNOWN) if task_ok else UNKNOWN,
            'context_paths': task.get('context_paths', UNKNOWN) if task_ok else UNKNOWN,
            'test_commands': task.get('test_commands', UNKNOWN) if task_ok else UNKNOWN,
        },
        'target': {'declared': task.get('target', UNKNOWN) if task_ok else UNKNOWN,
                   'recorded_source': meta.get('source', UNKNOWN) if meta_ok else UNKNOWN},
        'fingerprints': described['fingerprints'],
        'diff': pointer('changes.patch'),
        'tests': tests_view,
        'review': review_view,
        'process_evidence': process_evidence,
        'worker_final_report': worker_report,
        'evidence': _pointers(evidence),
        'known_gaps': ['%s: %s' % (issue['severity'], issue['detail']) for issue in issues],
        'issues': issues,
        'suggested_next_inspection': (_suggestions(meta, docs.get('failure.json')) if meta_ok and task_ok
                                      else ['Required evidence is missing or invalid: inspect the run manually.']),
        'current_liveness_verified': False,
        'current_target_verified': False,
        'acceptance_certified': False,
    }


# --- Read-only invocation ledger -------------------------------------------

LEDGER_VERSION = 2
LEDGER_JSONL_LIMIT = 4 * 1024 * 1024
LEDGER_MAX_RECORDS = 2048
LEDGER_MAX_INVOCATIONS = 128
LEDGER_EVENTS_LIMIT = 4 * 1024 * 1024
LEDGER_EVENTS_MAX_RECORDS = 32768
EXIT_CODE_LIMIT = 64
LEDGER_JOURNALS = ('tool-evidence.jsonl', 'bash-gate-events.jsonl')
LOG_DIR_PARENT = 'worker-test-logs'
LOG_DIR_MARKER = 'WORKER_TEST_LOG_DIR='
LOG_DIR_PATTERN = re.compile(r'^check-[A-Za-z0-9]{1,64}$')
LOG_STREAMS = (('exit-code', EXIT_CODE_LIMIT), ('stdout.log', BLOB_LIMIT), ('stderr.log', BLOB_LIMIT))
TOOL_START = 'tool_execution_start'
TOOL_END = 'tool_execution_end'
GATE_LOCKOUTS = ('gate_lockout_start', 'gate_lockout_end')
GATE_STATUSES = ('allowed', 'undeclared_command', 'invalid_arguments', 'unavailable', 'denied')
LOCKDOWN_ARGUMENTS = ('async', 'pty')
ASYNC_JOB_KEYS = ('jobId', 'job_id', 'backgroundJobId', 'asyncJobId')
EXIT_CODE_PATTERN = re.compile(r'-?\d{1,20}')
_TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


def _token(value):
    """A bounded type label for diagnostics; never a raw body."""
    return value if isinstance(value, str) and _TOKEN_PATTERN.match(value) else 'unrecognized'


def _exact_command_hash(command):
    """sha256 over the exact recorded command bytes; no trimming, no normalization."""
    return _digest(command.encode('utf-8', 'surrogatepass'))


def _policy_command_hash(command):
    """The existing native-frozen normalization: sha256 over the stripped command bytes."""
    return _digest(command.strip().encode('utf-8', 'surrogatepass'))


def _usable_command(args):
    """The recorded command when the required args shape carries a nonempty one."""
    command = args.get('command') if isinstance(args, dict) else None
    return command if isinstance(command, str) and command.strip() else None


def _canonical_args(args):
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
    except (TypeError, ValueError):
        return None


def _args_agree(request, decision):
    if not isinstance(request, dict) or not isinstance(decision, dict):
        return UNKNOWN
    left, right = _canonical_args(request), _canonical_args(decision)
    if left is None or right is None:
        return UNKNOWN
    return left == right


def _allowed_commands(task):
    """The frozen strict-mode command hashes; nothing is inferred when absent."""
    policy = task.get('worker_bash_policy') if isinstance(task, dict) else None
    values = policy.get('allowed_command_sha256') if isinstance(policy, dict) else None
    if not isinstance(values, list) or not values:
        return None, 'absent'
    if any(not isinstance(value, str) or len(value) != 64 or not set(value) <= _DIGEST_CHARS
           for value in values):
        return None, 'malformed'
    return frozenset(value.lower() for value in values), 'present'


def _jsonl_records(raw, name, issues, max_records):
    """Parse bounded JSONL bytes; every unusable record stays a visible issue."""
    if raw and not raw.endswith(b'\n'):
        issues.append(_issue('evidence', name + ': final record is incomplete', severity='gap'))
    lines = raw.splitlines()
    if len(lines) > max_records:
        issues.append(_issue('limit', name + ': more than %d records' % max_records))
        lines = lines[:max_records]
    documents = []
    for line in lines:
        if not line.strip():
            issues.append(_issue('evidence', name + ': blank record', severity='gap'))
            continue
        if len(line) > JSON_LIMIT:
            issues.append(_issue('limit', name + ': record exceeds the fixed record bound'))
            continue
        try:
            item = _strict_json(line)
        except (ValueError, RecursionError):
            issues.append(_issue('evidence', name + ': record is not valid strict JSON'))
            continue
        if not isinstance(item, dict):
            issues.append(_issue('evidence', name + ': record is not a JSON object'))
            continue
        documents.append(item)
    return documents


def _jsonl(path, issues, max_records=LEDGER_MAX_RECORDS):
    """Read one bounded JSONL journal; every unusable record stays a visible issue."""
    raw, problem = _read_bytes(path, LEDGER_JSONL_LIMIT)
    if problem is not None:
        issues.append(_issue('evidence', path.name + ': ' + problem, severity='gap'))
        return None, []
    return raw, _jsonl_records(raw, path.name, issues, max_records)


def _markers(result):
    """Exact marker lines from a terminal result; the surrounding body is never emitted."""
    content = result.get('content') if isinstance(result, dict) else None
    if not isinstance(content, list):
        return []
    values = []
    for item in content:
        if not isinstance(item, dict) or item.get('type') != 'text':
            continue
        text = item.get('text')
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            if line.startswith(LOG_DIR_MARKER):
                values.append(line[len(LOG_DIR_MARKER):])
                if len(values) > 1:
                    return values
    return values


def _bound_log_dir(result, run):
    """Validate the one canonical evidence directory without opening the claimed path."""
    values = _markers(result)
    if not values:
        return None, None
    if len(values) != 1:
        return None, _issue('ambiguous', 'terminal result carries multiple log directory markers')
    value = values[0]
    if (not value or any(character.isspace() for character in value) or
            not value.startswith('/') or value != os.path.normpath(value) or
            not LOG_DIR_PATTERN.match(os.path.basename(value)) or
            _canonical(os.path.dirname(value)) != _canonical(os.path.join(str(run), LOG_DIR_PARENT))):
        return None, _issue('path', 'terminal result log directory is not the canonical run evidence '
                                    'directory')
    return value, None


def _directory_flags():
    return (os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_DIRECTORY', 0) |
            getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))


def _open_kind(error, linked):
    if error.errno == errno.ELOOP or (error.errno == errno.ENOTDIR and linked):
        return 'symlink'
    if error.errno == errno.ENOENT:
        return 'missing'
    if error.errno in (errno.ENOTDIR, errno.EISDIR):
        return 'nonregular'
    return 'unreadable'


def _linked(path, dir_fd=None):
    """Non-following link probe for an already-validated naming path."""
    try:
        info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode)


def _parse_exit_code(raw):
    try:
        text = raw.decode('ascii').strip()
    except UnicodeDecodeError:
        return UNKNOWN
    if not EXIT_CODE_PATTERN.fullmatch(text):
        return UNKNOWN
    return int(text)


def _read_log_dir(run, log_dir):
    """Read the three retained log files through no-follow directory descriptors."""
    files, issues, exit_code = {}, [], UNKNOWN
    parent_fd = check_fd = None
    parent_path = os.path.join(str(run), LOG_DIR_PARENT)
    try:
        try:
            parent_fd = os.open(parent_path, _directory_flags())
        except OSError as error:
            issues.append(_issue('log', LOG_DIR_PARENT + ': ' + _open_kind(error, _linked(parent_path))))
            return files, exit_code, issues
        try:
            check_fd = os.open(os.path.basename(log_dir), _directory_flags(), dir_fd=parent_fd)
        except OSError as error:
            issues.append(_issue('log', 'log directory: ' +
                                 _open_kind(error, _linked(os.path.basename(log_dir),
                                                           dir_fd=parent_fd))))
            return files, exit_code, issues
        for name, limit in LOG_STREAMS:
            raw, problem = _read_bytes(name, limit, dir_fd=check_fd)
            if problem is not None:
                files[name] = {'status': problem, 'sha256': None, 'bytes': None}
                issues.append(_issue('log', name + ': ' + problem))
                continue
            files[name] = {'status': 'read', 'sha256': _digest(raw), 'bytes': len(raw)}
            if name == 'exit-code':
                exit_code = _parse_exit_code(raw)
                if exit_code == UNKNOWN:
                    issues.append(_issue('log', 'exit-code: retained file is not a plain numeric status'))
    finally:
        for descriptor in (check_fd, parent_fd):
            if descriptor is not None:
                os.close(descriptor)
    return files, exit_code, issues


def _async_document(run, sources):
    """Bounded no-follow metadata read for the frozen async policy."""
    record = {'file': 'metadata.json', 'status': 'read', 'sha256': None, 'bytes': None}
    raw, problem = _read_bytes(run / 'metadata.json', JSON_LIMIT)
    if problem is not None:
        record['status'] = problem
        sources.append(record)
        return None, problem
    record['sha256'], record['bytes'] = _digest(raw), len(raw)
    try:
        value = _strict_json(raw)
    except (ValueError, RecursionError):
        value = None
    if not isinstance(value, dict):
        record['status'] = 'invalid'
        sources.append(record)
        return None, 'not a strict JSON object'
    sources.append(record)
    return value, None


def _async_events(run, sources, issues):
    """Bounded no-follow compact event read; any unusable row leaves the join unavailable."""
    record = {'file': 'events.jsonl', 'status': 'read', 'sha256': None, 'bytes': None}
    local = []
    raw, problem = _read_bytes(run / 'events.jsonl', LEDGER_EVENTS_LIMIT)
    if problem is not None:
        record['status'] = problem
        local.append(_issue('evidence', 'events.jsonl: ' + problem, severity='gap'))
        documents = None
    else:
        record['sha256'], record['bytes'] = _digest(raw), len(raw)
        documents = _jsonl_records(raw, 'events.jsonl', local, LEDGER_EVENTS_MAX_RECORDS)
    sources.append(record)
    if local:
        issues.extend(local)
        return None
    return documents


def _async_join(run, task_doc, tools, sources, issues):
    """Read only the fixed async allowlist and join one structured v2 delivery."""
    metadata, problem = _async_document(run, sources)
    if problem is not None:
        issues.append(_issue('async', 'metadata.json: ' + problem))
        return invocation_async.unsupported('async_metadata_unavailable')
    events = _async_events(run, sources, issues)
    if events is None:
        issues.append(_issue('async', 'events.jsonl is unavailable for the async join'))
        return invocation_async.unsupported('async_compact_events_unavailable')
    version, problem = invocation_async.protocol(task_doc, metadata)
    if problem is not None:
        issues.append(_issue('async', 'async execution policy is unsupported'))
        return invocation_async.unsupported(problem)
    result = invocation_async.join(tools, events, version)
    if result['state'] != 'supported':
        issues.append(_issue('async', 'async delivery is unsupported: ' + result['reason']))
    return result


def _async_result(result):
    """Any details.async marker (native or malformed) or legacy job key stays unsupported."""
    details = result.get('details') if isinstance(result, dict) else None
    if not isinstance(details, dict):
        return False
    if 'async' in details:
        return True
    return any(key in details for key in ASYNC_JOB_KEYS)


def _gate_shape(decision, journal_present, local, max_denials):
    """Read the native classification separately from its preexecution decision."""
    if decision is None:
        if journal_present:
            local.append(_issue('gate', 'no gate decision record for this call ID'))
            return 'missing', UNKNOWN, UNKNOWN
        return 'absent', UNKNOWN, UNKNOWN
    status = decision.get('status')
    recorded = decision.get('decision')
    denials = decision.get('denials')
    if status not in GATE_STATUSES or recorded not in ('allow', 'block'):
        local.append(_issue('conflict', 'gate status or decision is unsupported'))
        return UNKNOWN, UNKNOWN, UNKNOWN
    if type(denials) is not int or denials < 0:
        local.append(_issue('record', 'gate decision denials is missing or not a nonnegative integer'))
        return status, recorded, UNKNOWN
    if status == 'allowed':
        if recorded == 'block':
            coherent = max_denials is not None and denials >= max_denials
        else:
            coherent = max_denials is None or denials < max_denials
    else:
        coherent = recorded == 'block' and denials >= 1
    if not coherent:
        local.append(_issue('conflict', 'gate decision disagrees with its status or saved denial limit'))
        return UNKNOWN, UNKNOWN, denials
    return status, recorded, denials


def _row_binding_complete(row):
    if any(issue['severity'] in ('error', 'gap') for issue in row['issues']):
        return False
    if row['gate_decision'] == 'block':
        # OMP can retain start/end wrappers for a request blocked before Bash execution.
        return type(row['gate_denials']) is int and row['gate_denials'] >= 1
    if row['gate_status'] != 'allowed' or row['gate_decision'] != 'allow':
        return False
    return bool(row['start_present'] and row['end_present'] and row['terminal_result_present'] and
                row['args_agree'] is True and row['command_sha256'] != UNKNOWN and
                row['log_status'] == 'bound' and row['exit_code'] != UNKNOWN and
                len(row['logs']) == len(LOG_STREAMS) and
                all(record['status'] == 'read' for record in row['logs'].values()))


def _ledger_document(run, sources, rows, issues, policy_state, allowed_count, ready=False,
                     async_state=None):
    async_state = async_state or {'state': 'absent', 'reason': None, 'version': None}
    complete = (bool(rows) and all(row['binding_complete'] for row in rows) and
                not any(issue['severity'] in ('error', 'gap') for issue in issues))
    return {
        'tool': 'evidence_tools.invocation_ledger',
        'ledger_version': LEDGER_VERSION,
        'run': str(run),
        'label': ('stored evidence only: not a current execution attestation, not test success and '
                  'not acceptance'),
        'policy': {'worker_bash_policy': policy_state, 'allowed_commands': allowed_count,
                   'command_hash_input': 'exact recorded command bytes (UTF-8, surrogatepass)',
                   'policy_command_hash_input': 'stripped command bytes (UTF-8, surrogatepass)'},
        'ordering': {'row_order': ('tool journal start order, then gate-journal-only decisions, then '
                                   'tool journal end order for rows with no start record'),
                     'cross_journal_chronology': UNKNOWN},
        'gate_ready': ready,
        'async': {'state': async_state['state'],
                  'protocol_version': async_state.get('version'),
                  'reason': async_state.get('reason'),
                  'joined_jobs': sum(1 for row in rows if row['async_binding'] is not None)},
        'limits': {'jsonl_bytes': LEDGER_JSONL_LIMIT, 'records_per_file': LEDGER_MAX_RECORDS,
                   'invocation_ids': LEDGER_MAX_INVOCATIONS, 'log_bytes': BLOB_LIMIT,
                   'exit_code_bytes': EXIT_CODE_LIMIT,
                   'async_events_bytes': LEDGER_EVENTS_LIMIT,
                   'async_events_records': LEDGER_EVENTS_MAX_RECORDS},
        'sources': sources,
        'invocations': sum(1 for row in rows if row['executed'] is True),
        'observed_starts': sum(1 for row in rows if row['start_present'] is True),
        'retained_requests': len(rows),
        'rows': rows,
        'issues': issues,
        'complete': complete,
        'current_liveness_verified': False,
        'current_target_verified': False,
        'acceptance_certified': False,
    }


def invocation_ledger(run_path):
    """Join stored request, gate and terminal records for one run without executing anything."""
    run = Path(_canonical(run_path))
    issues, sources = [], []
    if not run.is_dir():
        issues.append(_issue('run', 'run path is missing or not a directory'))
        return _ledger_document(run, sources, [], issues, 'unavailable', 0)

    task_record = {'file': 'task.json', 'status': 'read', 'sha256': None, 'bytes': None}
    task_doc = None
    raw, problem = _read_bytes(run / 'task.json', JSON_LIMIT)
    if problem is not None:
        task_record['status'] = problem
        issues.append(_issue('required', 'task.json: ' + problem))
    else:
        task_record['sha256'], task_record['bytes'] = _digest(raw), len(raw)
        try:
            value = _strict_json(raw)
        except (ValueError, RecursionError):
            value = None
        if not isinstance(value, dict):
            task_record['status'] = 'invalid'
            issues.append(_issue('invalid', 'task.json: not a strict JSON object'))
        else:
            task_doc = value
    sources.append(task_record)
    allowed, policy_state = _allowed_commands(task_doc)
    gate_policy = task_doc.get('worker_bash_gate') if isinstance(task_doc, dict) else None
    limit = gate_policy.get('max_denials') if isinstance(gate_policy, dict) else None
    max_denials = limit if type(limit) is int and limit > 0 else None
    if policy_state == 'malformed':
        issues.append(_issue('policy', 'task.worker_bash_policy.allowed_command_sha256 is malformed'))
    elif policy_state == 'absent':
        issues.append(_issue('policy', 'task.worker_bash_policy.allowed_command_sha256 is absent; '
                                       'command matching stays unknown', severity='gap'))

    journals = {}
    input_faults = False
    for name in LEDGER_JOURNALS:
        mark = len(issues)
        raw, documents = _jsonl(run / name, issues)
        if len(issues) > mark:
            input_faults = True
        journals[name] = documents
        sources.append({'file': name, 'status': 'read' if raw is not None else 'missing',
                        'sha256': _digest(raw) if raw is not None else None,
                        'bytes': len(raw) if raw is not None else None})
    tool_records = journals[LEDGER_JOURNALS[0]]
    gate_records = journals[LEDGER_JOURNALS[1]]
    gate_journal = sources[-1]['status'] == 'read'
    row_issues = {}

    def note(ident, kind, detail, severity='error'):
        row_issues.setdefault(ident, []).append(_issue(kind, detail, severity=severity))

    starts, ends, decisions = {}, {}, {}
    ready = False
    for item in tool_records:
        kind = item.get('type')
        ident = item.get('id')
        envelope = item.get('version')
        if type(envelope) is not int or envelope != 1:
            issues.append(_issue('unsupported', 'tool evidence record with an unsupported envelope '
                                                'version is retained as unsupported'))
            input_faults = True
            continue
        if kind in GATE_LOCKOUTS:
            issues.append(_issue('unsupported', 'gate lockout record is retained as unsupported'))
            input_faults = True
            continue
        if kind == 'async_result':
            # Retained privately for the v2 join; a packet never becomes a ledger row.
            continue
        if kind not in (TOOL_START, TOOL_END):
            issues.append(_issue('unsupported', 'unsupported tool evidence record type: ' + _token(kind)))
            input_faults = True
            continue
        if not isinstance(ident, str) or not ident:
            issues.append(_issue('record', 'tool evidence record without a usable call ID'))
            input_faults = True
            continue
        target = starts if kind == TOOL_START else ends
        if ident in target:
            note(ident, 'duplicate', 'duplicate ' + kind + ' record for one call ID')
            input_faults = True
            continue
        target[ident] = item
        if kind == TOOL_START and isinstance(item.get('args'), dict):
            for key in LOCKDOWN_ARGUMENTS:
                if item['args'].get(key) is True:
                    note(ident, 'unsupported', 'requested %s execution is retained as unsupported' % key)
    for item in gate_records:
        kind = item.get('type')
        ident = item.get('id')
        if kind == 'ready':
            ready = True  # Metadata only: no invocation, no execution claim, no body copied.
            continue
        if kind != 'decision':
            issues.append(_issue('unsupported', 'unsupported gate record type: ' + _token(kind)))
            input_faults = True
            continue
        if not isinstance(ident, str) or not ident:
            issues.append(_issue('record', 'gate record without a usable call ID'))
            input_faults = True
            continue
        if ident in decisions:
            note(ident, 'duplicate', 'duplicate gate decision for one call ID')
            input_faults = True
            continue
        decisions[ident] = item

    order = ([(ident, 'tool_journal_start') for ident in starts] +
             [(ident, 'gate_journal_decision') for ident in decisions if ident not in starts] +
             [(ident, 'tool_journal_end')
              for ident in ends if ident not in starts and ident not in decisions])
    if len(order) > LEDGER_MAX_INVOCATIONS:
        issues.append(_issue('limit', 'more than %d invocation IDs; only the first set is retained'
                             % LEDGER_MAX_INVOCATIONS))
        input_faults = True
        order = order[:LEDGER_MAX_INVOCATIONS]

    async_state = {'state': 'absent', 'reason': None, 'version': None}
    async_jobs = {}
    if invocation_async.presence(tool_records):
        if input_faults:
            issues.append(_issue('async', 'required journal evidence is incomplete for the async join'))
            issues.append(_issue('unsupported', 'async delivery evidence is retained as unsupported; '
                                                'no job is joined'))
            async_state = invocation_async.unsupported('async_inputs_incomplete')
        else:
            async_state = _async_join(run, task_doc, tool_records, sources, issues)
            if async_state['state'] == 'supported':
                async_jobs = {job['tool_call_id']: job for job in async_state['jobs']}
            else:
                issues.append(_issue('unsupported', 'async delivery evidence is retained as '
                                                    'unsupported; no job is joined'))

    rows = []
    for position, (ident, order_basis) in enumerate(order):
        start, end, decision = starts.get(ident), ends.get(ident), decisions.get(ident)
        local = list(row_issues.get(ident, []))
        start_args = start.get('args') if start is not None else None
        gate_args = decision.get('args') if decision is not None else None
        start_command = _usable_command(start_args)
        gate_command = _usable_command(gate_args)
        if start is not None and start_command is None:
            local.append(_issue('record', 'request start record has no usable nonempty command '
                                          'argument'))
        if decision is not None and gate_command is None:
            local.append(_issue('record', 'gate decision record has no usable nonempty command '
                                          'argument'))
        agreement = (_args_agree(start_args, gate_args)
                     if (start is not None and decision is not None) else UNKNOWN)
        if agreement is False:
            local.append(_issue('conflict', 'recorded request arguments differ from the gate decision '
                                            'arguments'))
        if start is not None and end is None:
            local.append(_issue('orphan', 'terminal result record is missing for this call ID'))
        if end is not None and start is None:
            local.append(_issue('orphan', 'request start record is missing for this call ID'))
        if start_command is not None:
            command_source, command = 'request', start_command
        elif gate_command is not None:
            # Explicitly labelled source: raw start arguments never fall back to gate arguments.
            command_source, command = 'gate', gate_command
        else:
            command_source, command = 'absent', None
        command_hash = (_exact_command_hash(command) if command_source == 'request' else UNKNOWN)
        policy_hash = _policy_command_hash(command) if command is not None else UNKNOWN
        if policy_hash == UNKNOWN or allowed is None:
            command_match = UNKNOWN
        elif policy_hash in allowed:
            command_match = True
        else:
            command_match = False

        gate_status, gate_decision, gate_denials = _gate_shape(decision, gate_journal, local, max_denials)
        if gate_decision == 'block':
            local.append(_issue('denied', 'gate denied this request before execution; retained as not '
                                          'executed', severity='note'))

        result = end.get('result') if end is not None else None
        terminal_present = isinstance(result, dict)
        if end is not None and not terminal_present:
            local.append(_issue('record', 'terminal result record is missing or not a JSON object'))
        recorded_error = end.get('is_error') if end is not None else None
        if end is not None and type(recorded_error) is not bool:
            local.append(_issue('record', 'terminal result is_error is missing or not a boolean'))
            recorded_error = UNKNOWN
        job = async_jobs.get(ident)
        log_dir, log_status = None, 'absent'
        if terminal_present:
            if gate_decision == 'block':
                if _markers(result):
                    local.append(_issue('conflict', 'blocked request carries a test log marker'))
            elif job is not None:
                log_dir, marker_issue = _bound_log_dir({'content': job['content']}, run)
                if marker_issue is not None:
                    log_status = 'invalid'
                    local.append(marker_issue)
                elif log_dir is None:
                    local.append(_issue('log', 'async notification carries no canonical '
                                               'WORKER_TEST_LOG_DIR marker'))
            else:
                log_dir, marker_issue = _bound_log_dir(result, run)
                if marker_issue is not None:
                    log_status = 'invalid'
                    local.append(marker_issue)
                elif log_dir is None:
                    local.append(_issue('log', 'terminal result carries no canonical '
                                               'WORKER_TEST_LOG_DIR marker'))
            if job is None and _async_result(result):
                local.append(_issue('unsupported', 'terminal result reports a background job; retained '
                                                   'as unsupported and no job is joined'))
        details = result.get('details') if terminal_present else None
        if terminal_present and not isinstance(details, dict):
            local.append(_issue('record', 'terminal result details is missing or not a JSON object'))
        recorded_exit = details.get('exitCode') if isinstance(details, dict) else None
        if terminal_present and (job is not None or _async_result(result)):
            # An async acknowledgement (joined, unsupported, malformed or legacy) is
            # never a synchronous terminal result: its exitCode is not provenance.
            terminal_exit_code = UNKNOWN
        elif not isinstance(details, dict) or 'exitCode' not in details:
            terminal_exit_code = UNKNOWN
        elif type(recorded_exit) is int:
            terminal_exit_code = recorded_exit
        else:
            local.append(_issue('record', 'terminal result exitCode is not an integer'))
            terminal_exit_code = UNKNOWN
        files, exit_code, log_issues = {}, UNKNOWN, []
        if log_dir is not None:
            log_status = 'bound'
            files, exit_code, log_issues = _read_log_dir(run, log_dir)
            local.extend(log_issues)
        if type(exit_code) is int and type(terminal_exit_code) is int and exit_code != terminal_exit_code:
            local.append(_issue('conflict', 'retained exit-code file differs from the terminal result '
                                            'exitCode'))
        row = {
            'row_index': position,
            'tool_call_id': ident,
            'order_basis': order_basis,
            'executed': UNKNOWN,
            'start_present': start is not None,
            'end_present': end is not None,
            'terminal_result_present': terminal_present,
            'result_source': ('async-notice' if job is not None else
                              'terminal' if terminal_present else 'absent'),
            'async_binding': ({'job_id': job['job_id'], 'generation': job['generation'],
                               'ack_state': job['ack_state'],
                               'result_sha256': job['result_sha256'],
                               'notice_sha256': job['notice_sha256']}
                              if job is not None else None),
            'recorded_is_error': recorded_error,
            'command_sha256': command_hash,
            'policy_command_sha256': policy_hash,
            'command_source': command_source,
            'command_match': command_match,
            'args_agree': agreement,
            'gate_status': gate_status,
            'gate_decision': gate_decision,
            'gate_denials': gate_denials,
            'log_dir': log_dir if log_dir is not None else UNKNOWN,
            'log_status': log_status,
            'logs': files,
            'exit_code': exit_code,
            'terminal_exit_code': terminal_exit_code,
            'issues': local,
            'binding_complete': False,
        }
        if (gate_decision == 'block' and type(gate_denials) is int and gate_denials >= 1 and
                not any(issue['kind'] in ('conflict', 'duplicate', 'unsupported') for issue in local)):
            row['executed'] = False
        elif (_row_binding_complete(row) and gate_status == 'allowed' and
              gate_decision == 'allow' and command_match is True):
            row['executed'] = True
        rows.append(row)
    shared = {}
    for row in rows:
        if row['log_dir'] != UNKNOWN:
            shared.setdefault(row['log_dir'], []).append(row)
    for group in shared.values():
        if len(group) < 2:
            continue
        for row in group:
            row['issues'].append(_issue('conflict', 'log directory is shared across distinct call IDs'))
            row['executed'] = UNKNOWN
    for row in rows:
        row['binding_complete'] = _row_binding_complete(row)
        if row['binding_complete'] and row['gate_decision'] == 'allow' and row['command_match'] is False:
            issues.append(_issue('command', 'bound allowed request command is not one of the frozen '
                                            'allowed command hashes'))
    return _ledger_document(run, sources, rows, issues, policy_state,
                            len(allowed) if allowed is not None else 0, ready, async_state)


def _reject_output(output, forbidden):
    target = Path(_canonical(output))
    for candidate in forbidden:
        resolved = Path(_canonical(candidate))
        if target == resolved:
            raise ValueError('output must not replace an input path')
        if resolved.is_dir():
            try:
                target.relative_to(resolved)
            except ValueError:
                continue
            raise ValueError('output must not be written inside an input run directory')


def _write_exclusive(output, payload):
    output = Path(output)
    parent = output.parent
    if not parent.is_dir():
        raise ValueError('output directory does not exist')
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.evidence-', dir=parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)  # Atomic exclusive create; EEXIST never overwrites.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'output': str(output), 'sha256': _digest(raw)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    index = sub.add_parser('index', help='Read-only recovery index over explicitly named runs')
    index.add_argument('--manifest', required=True)
    index.add_argument('--output')
    bundle = sub.add_parser('bundle', help='Read-only review package for one run')
    bundle.add_argument('--run', required=True)
    bundle.add_argument('--output')
    invocations = sub.add_parser('invocations',
                                 help='Read-only saved invocation ledger for one run')
    invocations.add_argument('--run', required=True)
    invocations.add_argument('--output')
    args = parser.parse_args(argv)
    try:
        if args.action == 'index':
            result = recovery_index(args.manifest)
            forbidden = [args.manifest] + [item['run'] for entry in result['slices'] for item in entry['runs']]
        elif args.action == 'invocations':
            result = invocation_ledger(args.run)
            forbidden = [args.run]
        else:
            result = review_package(args.run)
            forbidden = [args.run]
        if args.output:
            _reject_output(args.output, forbidden)
            summary = _write_exclusive(args.output, result)
        else:
            summary = result
        print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, 'Evidence tool failed: %s\n' % error)


if __name__ == '__main__':
    main()
