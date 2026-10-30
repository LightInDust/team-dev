"""Append-only receipts for target-test invocations plus read-only inspection.

A receipt records what was launched and what was actually observed. It never
claims that a test passed and it never reconstructs a report: an invocation
whose capture stopped is left incomplete and must not be replayed blindly.

Every record is created exclusively (no overwrite) and fsynced, so a later
step can only add new evidence. JSON records carry numeric phase prefixes:
intent, before identities/manifest, commands, post manifest/identity, report,
binding, failure and completion. Captured command logs share the command
phase number and are referenced from the immutable result record.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

ATTEMPTS = 'target-test-attempts'
INTENT = '000000-intent.json'
BEFORE_IDENTITY = '200000-before-identity.json'
BEFORE_MANIFEST = '300000-before-manifest.json'
POST_MANIFEST = '800000-post-manifest.json'
POST_IDENTITY = '800010-post-identity.json'
REPORT = '900000-report.json'
BINDING = '900010-report-binding.json'
FAILURE = '950000-failure.json'
COMPLETED = '990000-completed.json'
COMMAND_PHASE = 400000
COMMAND_STRIDE = 20
MAX_COMMANDS = 9999
# Durable run-level mark written when an invocation starts recording receipts.
RUN_MARKER = 'target_test_receipts'
RUN_MARKER_VERSION = 1

JSON_LIMIT = 1024 * 1024
_HEX = re.compile(r'\A[0-9a-f]{32}\Z')
_TEST_LOG = re.compile(r'\Atest-\d+\.(?:stdout|stderr)\Z')
_STREAMS = ('stdout', 'stderr')
_IDENTITY_KEYS = ('host', 'user', 'repo', 'head')
_STAGES = {BEFORE_IDENTITY: 'before_identity', BEFORE_MANIFEST: 'before_manifest',
           POST_MANIFEST: 'post_manifest', POST_IDENTITY: 'post_identity'}


class ReceiptError(RuntimeError):
    """Unresolved evidence: the caller must not replace or replay it."""


def _command_index(index):
    if type(index) is not int or not 0 <= index <= MAX_COMMANDS:
        raise ValueError('Command index must be an integer in 0..%d' % MAX_COMMANDS)
    return index


def command_intent_name(index):
    return '%06d-command-%03d-intent.json' % (
        COMMAND_PHASE + _command_index(index) * COMMAND_STRIDE, index)


def command_result_name(index):
    return '%06d-command-%03d-result.json' % (
        COMMAND_PHASE + _command_index(index) * COMMAND_STRIDE + 10, index)


def command_log_name(index, stream):
    if stream not in _STREAMS:
        raise ValueError('Unknown stream: ' + repr(stream))
    offset = _STREAMS.index(stream) + 1
    return '%06d-command-%03d.%s' % (
        COMMAND_PHASE + _command_index(index) * COMMAND_STRIDE + offset, index, stream)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    checksum = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def _fsync_directory(directory):
    try:
        handle = os.open(os.fspath(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        # Some filesystems refuse directory fsync; file fsync already succeeded.
        pass
    finally:
        os.close(handle)


def _exclusive_write(directory, name, data):
    path = Path(directory) / name
    handle = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(handle, view)
            if written <= 0:
                raise OSError('Short write to receipt record: ' + name)
            view = view[written:]
        os.fsync(handle)
    finally:
        os.close(handle)
    _fsync_directory(directory)
    return path


class Attempt:
    """One invocation; each method appends one immutable record."""

    def __init__(self, directory, attempt_id, sequence):
        self.directory = Path(directory)
        self.attempt_id = attempt_id
        self.sequence = sequence

    def _record(self, name, value):
        return _exclusive_write(self.directory, name, _json_bytes(value))

    def command_intent(self, index, declared_argv, actual_argv):
        if type(index) is not int or index < 0:
            raise ValueError('Command index must be a non-negative integer')
        return self._record(command_intent_name(index), {
            'version': 1, 'kind': 'command_intent', 'attempt_id': self.attempt_id,
            'index': index, 'declared_argv': list(declared_argv),
            'actual_argv': list(actual_argv)})

    def command_result(self, index, *, transport, transport_complete, declared_exit_code,
                       transport_returncode, stdout, stderr):
        if type(index) is not int or index < 0:
            raise ValueError('Command index must be a non-negative integer')
        if transport not in ('local', 'ssh'):
            raise ValueError('Unknown transport: ' + repr(transport))
        if type(transport_complete) is not bool:
            raise ValueError('transport_complete must be a boolean')
        if type(declared_exit_code) is not int and declared_exit_code is not None:
            raise ValueError('declared_exit_code must be an integer or null')
        if type(transport_returncode) is not int and transport_returncode is not None:
            raise ValueError('transport_returncode must be an integer or null')
        if transport_complete != (type(declared_exit_code) is int):
            raise ValueError('A declared exit exists only for a complete transport')
        if type(stdout) is not bytes or type(stderr) is not bytes:
            raise ValueError('Captured streams must be bytes')
        captured = {}
        for stream, data in (('stdout', stdout), ('stderr', stderr)):
            name = command_log_name(index, stream)
            _exclusive_write(self.directory, name, data)
            captured[stream] = {'name': name, 'sha256': _sha256(data), 'bytes': len(data)}
        record = {
            'version': 1, 'kind': 'command_result', 'attempt_id': self.attempt_id,
            'index': index, 'transport': transport,
            'transport_complete': transport_complete,
            'declared_exit_code': declared_exit_code,
            'transport_returncode': transport_returncode,
            'stdout': captured['stdout']['name'],
            'stdout_sha256': captured['stdout']['sha256'],
            'stdout_bytes': captured['stdout']['bytes'],
            'stderr': captured['stderr']['name'],
            'stderr_sha256': captured['stderr']['sha256'],
            'stderr_bytes': captured['stderr']['bytes']}
        self._record(command_result_name(index), record)
        return record

    def stage(self, name, value):
        if name not in _STAGES:
            raise ValueError('Unknown stage record: ' + repr(name))
        return self._record(name, {'version': 1, 'kind': 'stage',
                                   'attempt_id': self.attempt_id,
                                   'stage': _STAGES[name], 'value': value})

    def failure(self, stage, error, command_index=None):
        record = {'version': 1, 'kind': 'failure', 'attempt_id': self.attempt_id,
                  'stage': stage, 'error_type': type(error).__name__,
                  'message': str(error)[:2000]}
        if command_index is not None:
            record['command_index'] = command_index
        return self._record(FAILURE, record)

    def complete(self, report_path):
        data = Path(report_path).read_bytes()
        _exclusive_write(self.directory, REPORT, data)
        binding = {'version': 1, 'kind': 'report_binding', 'attempt_id': self.attempt_id,
                   'report': REPORT, 'report_sha256': _sha256(data),
                   'top_level': 'tests.json', 'top_level_sha256': _sha256(data)}
        binding_path = self._record(BINDING, binding)
        binding_sha = file_sha256(binding_path)
        _exclusive_write(self.directory, COMPLETED, _json_bytes({
            'version': 1, 'kind': 'completed', 'attempt_id': self.attempt_id,
            'binding': BINDING, 'binding_sha256': binding_sha,
            'report_sha256': binding['report_sha256']}))
        return binding


def begin(run, task, applied):
    """Allocate an exclusive attempt directory and durably record intent first."""
    run = Path(run)
    root = run / ATTEMPTS
    if root.is_symlink():
        raise ReceiptError('Refusing a symlinked target-test receipt directory')
    root.mkdir(mode=0o700, exist_ok=True)
    sequence = 1
    for entry in root.iterdir():
        if not (_HEX.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()):
            continue
        try:
            value = json.loads((entry / INTENT).read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and type(value.get('sequence')) is int:
            sequence = max(sequence, value['sequence'] + 1)
    identity = applied.get('source') if isinstance(applied, dict) else None
    commands = task.get('test_commands', []) if isinstance(task, dict) else []
    intent = {'version': 1, 'kind': 'target_test_intent', 'attempt_id': None,
              'sequence': sequence,
              'task_sha256': file_sha256(run / 'task.json'),
              'applied_sha256': file_sha256(run / 'applied.json'),
              'applied_fingerprint': applied.get('fingerprint') if isinstance(applied, dict) else None,
              'identity': identity,
              'declared_commands': [list(argv) for argv in commands]}
    directory = None
    for _ in range(8):
        candidate = root / uuid.uuid4().hex
        try:
            os.mkdir(candidate, 0o700)
        except FileExistsError:
            continue
        directory = candidate
        break
    if directory is None:
        raise ReceiptError('Cannot allocate a unique target-test attempt directory')
    intent['attempt_id'] = directory.name
    try:
        _exclusive_write(directory, INTENT, _json_bytes(intent))
    except BaseException:
        # Only an empty directory we just created is removed; partial records stay.
        try:
            os.rmdir(directory)
        except OSError:
            pass
        raise
    return Attempt(directory, directory.name, sequence)


def _load_file(path, problems, stage, missing='invalid'):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        if missing is not None:
            problems.append({'severity': missing, 'stage': stage,
                             'error': 'missing or symlinked record'})
        return None
    try:
        if path.stat().st_size > JSON_LIMIT:
            raise ValueError('oversized record')
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError('record is not an object')
        return value
    except (OSError, ValueError, UnicodeDecodeError) as error:
        problems.append({'severity': 'invalid', 'stage': stage,
                         'error': '%s: %s' % (type(error).__name__, error)})
        return None


def _load(directory, name, problems, stage, missing='incomplete'):
    return _load_file(Path(directory) / name, problems, stage, missing)


def _attempt_record(attempt_id, sequence, problems, stages, commands, report, completed):
    invalid = any(problem['severity'] == 'invalid' for problem in problems)
    unresolved = list(problems)
    for command in commands:
        if command['exit_state'] != 'known':
            unresolved.append({'severity': 'incomplete',
                               'stage': 'command-%03d' % command['index'],
                               'error': 'declared test exit is unknown'})
    if invalid:
        state = 'invalid'
    elif not completed or problems:
        # A completed capture with an unknown transport stays completed here;
        # the guard rejects its replay explicitly instead of hiding the phase.
        state = 'incomplete'
    else:
        state = 'completed'
    return {'attempt_id': attempt_id, 'sequence': sequence, 'state': state,
            'stages': stages, 'commands': commands, 'report': report,
            'completed': bool(completed),
            'unresolved': [{'severity': item['severity'], 'stage': item['stage'],
                            'error': item['error']} for item in unresolved]}


def _same_identity(actual, expected):
    """Match the existing identity contract: only the selected binding keys."""
    return (isinstance(actual, dict) and isinstance(expected, dict) and
            all(actual.get(key) == expected.get(key) for key in _IDENTITY_KEYS))


def _check_intent_bindings(run, intent, problems):
    """Bind the recorded intent to the frozen run bytes it claims to describe."""
    for filename, key, label in (('task.json', 'task_sha256', 'task'),
                                 ('applied.json', 'applied_sha256', 'applied')):
        declared = intent.get(key)
        if not isinstance(declared, str):
            problems.append({'severity': 'invalid', 'stage': 'intent',
                             'error': 'intent %s hash is not a digest' % label})
            continue
        try:
            actual = file_sha256(Path(run) / filename)
        except OSError as error:
            problems.append({'severity': 'invalid', 'stage': 'intent',
                             'error': '%s is unavailable: %s' % (filename, error)})
            continue
        if actual != declared:
            problems.append({'severity': 'invalid', 'stage': 'intent',
                             'error': 'intent %s hash does not match %s' % (label, filename)})
    applied = _load_file(Path(run) / 'applied.json', problems, 'applied')
    if applied is None:
        return
    if applied.get('fingerprint') != intent.get('applied_fingerprint'):
        problems.append({'severity': 'invalid', 'stage': 'intent',
                         'error': 'intent applied fingerprint differs from applied.json'})
    if not _same_identity(applied.get('source'), intent.get('identity')):
        problems.append({'severity': 'invalid', 'stage': 'intent',
                         'error': 'intent identity differs from applied.json'})


def _actual_argv_matches(actual, declared):
    return (isinstance(actual, list) and
            all(isinstance(token, str) and token for token in actual) and
            len(actual) >= len(declared) and actual[-len(declared):] == declared)


def _check_report_binding(intent, values, declared, results, report, problems):
    """Require the saved report to agree with the captured command records."""
    def bad(stage, message):
        problems.append({'severity': 'invalid', 'stage': stage, 'error': message})

    identity = intent.get('identity')
    expected = intent.get('applied_fingerprint')
    before_identity = values.get('before_identity')
    before_manifest = values.get('before_manifest')
    post_manifest = values.get('post_manifest')
    post_identity = values.get('post_identity')
    if before_identity is None or not _same_identity(before_identity.get('identity'), identity):
        bad('before_identity', 'captured identity does not match the applied identity')
    if post_identity is None or not _same_identity(post_identity.get('identity'), identity):
        bad('post_identity', 'captured identity does not match the applied identity')
    if (before_manifest is None or before_manifest.get('fingerprint') != expected or
            before_manifest.get('applied_fingerprint') != expected or
            before_manifest.get('matches') is not True):
        bad('before_manifest', 'captured manifest does not match the applied fingerprint')
    if post_manifest is None:
        bad('post_manifest', 'missing manifest capture')
    elif type(post_manifest.get('unchanged')) is not bool:
        bad('post_manifest', 'malformed unchanged flag')
    elif post_manifest['unchanged'] and post_manifest.get('fingerprint') != expected:
        bad('post_manifest', 'unchanged capture has a different fingerprint')
    tests = report.get('tests')
    if not isinstance(tests, list) or len(tests) != len(declared):
        bad('report', 'report does not cover the declared commands')
        tests = []
    for index, (entry, argv) in enumerate(zip(tests, declared)):
        label = 'command-%03d' % index
        result = results.get(index)
        if not isinstance(entry, dict):
            bad(label, 'malformed report command entry')
            continue
        if entry.get('argv') != argv:
            bad(label, 'report argv differs from the declared command')
        if (entry.get('stdout') != 'test-%d.stdout' % index or
                entry.get('stderr') != 'test-%d.stderr' % index):
            bad(label, 'report log reference differs from the declared test log name')
        if result is None:
            bad(label, 'report command has no captured result')
            continue
        if entry.get('exit_code') != result.get('declared_exit_code'):
            bad(label, 'report exit differs from the captured result')
        version = report.get('evidence_version')
        if version == 1:
            if entry.get('stdout_sha256') != result.get('stdout_sha256'):
                bad(label, 'report stdout hash differs from the captured log')
            if entry.get('stderr_sha256') != result.get('stderr_sha256'):
                bad(label, 'report stderr hash differs from the captured log')
            if entry.get('transport_complete') != result.get('transport_complete'):
                bad(label, 'report transport state differs from the captured result')
            if entry.get('transport_returncode') != result.get('transport_returncode'):
                bad(label, 'report transport code differs from the captured result')
        elif version is not None:
            bad('report', 'unsupported report evidence version')
    if post_manifest is not None and post_manifest.get('fingerprint') != report.get('fingerprint'):
        bad('report', 'report fingerprint differs from the post-manifest capture')
    if post_identity is not None and not _same_identity(report.get('host'),
                                                        post_identity.get('identity')):
        bad('report', 'report host differs from the post-identity capture')
    passed = report.get('passed')
    if type(passed) is not bool:
        bad('report', 'malformed passed flag')
    elif passed:
        if post_manifest is not None and post_manifest.get('unchanged') is not True:
            bad('report', 'a changed manifest cannot report a passed test')
        for index, result in sorted(results.items()):
            if result.get('declared_exit_code') != 0 or result.get('transport_complete') is not True:
                bad('command-%03d' % index, 'a passed report requires a known zero exit')


def _inspect(directory, run):
    attempt_id = directory.name
    problems = []
    stages = []
    intent = _load(directory, INTENT, problems, 'intent')
    if intent is None:
        return _attempt_record(attempt_id, None, problems, stages, [], None, False)
    # A completion marker turns every earlier phase into a required record.
    completed = _load(directory, COMPLETED, problems, 'completed', missing=None)
    require_chain = completed is not None
    declared = intent.get('declared_commands')
    usable = (isinstance(declared, list) and len(declared) <= MAX_COMMANDS + 1)
    malformed = (intent.get('version') != 1 or intent.get('kind') != 'target_test_intent' or
                 intent.get('attempt_id') != attempt_id or
                 type(intent.get('sequence')) is not int or
                 not isinstance(declared, list) or not usable or
                 any(not isinstance(argv, list) or
                     not all(isinstance(token, str) for token in argv) for argv in declared))
    if malformed:
        problems.append({'severity': 'invalid', 'stage': 'intent',
                         'error': 'malformed invocation intent'})
    else:
        _check_intent_bindings(run, intent, problems)
    sequence = intent.get('sequence') if type(intent.get('sequence')) is int else None
    commands = []
    results = {}
    for index, argv in enumerate(declared if usable else []):
        label = 'command-%03d' % index
        entry = {'index': index, 'declared_argv': argv, 'actual_argv': None,
                 'status': 'not_started', 'exit_state': 'unknown',
                 'transport': None, 'transport_complete': None,
                 'declared_exit_code': None, 'transport_returncode': None}
        command_intent = _load(directory, command_intent_name(index), problems,
                               label + '-intent', missing=None)
        intent_ok = False
        if command_intent is None:
            if require_chain:
                problems.append({'severity': 'invalid', 'stage': label + '-intent',
                                 'error': 'completed attempt has no launch intent'})
        elif (command_intent.get('version') != 1 or
                command_intent.get('kind') != 'command_intent' or
                command_intent.get('attempt_id') != attempt_id or
                command_intent.get('index') != index or
                command_intent.get('declared_argv') != argv):
            problems.append({'severity': 'invalid', 'stage': label + '-intent',
                             'error': 'malformed launch intent'})
        elif not _actual_argv_matches(command_intent.get('actual_argv'), argv):
            problems.append({'severity': 'invalid', 'stage': label + '-intent',
                             'error': 'launch intent does not carry the declared command'})
        else:
            entry['actual_argv'] = command_intent['actual_argv']
            entry['status'] = 'intent_without_result'
            intent_ok = True
        result = _load(directory, command_result_name(index), problems, label, missing=None)
        if result is None:
            if intent_ok:
                problems.append({'severity': 'incomplete', 'stage': label,
                                 'error': 'launch intent has no captured result'})
            elif require_chain and command_intent is None:
                problems.append({'severity': 'incomplete', 'stage': label,
                                 'error': 'completed attempt has no captured result'})
            commands.append(entry)
            continue
        if command_intent is None:
            problems.append({'severity': 'invalid', 'stage': label,
                             'error': 'captured result has no launch intent'})
        complete = result.get('transport_complete')
        declared_exit = result.get('declared_exit_code')
        observed_exit = result.get('transport_returncode')
        valid = (result.get('version') == 1 and result.get('kind') == 'command_result' and
                 result.get('attempt_id') == attempt_id and result.get('index') == index and
                 result.get('transport') in ('local', 'ssh') and
                 type(complete) is bool and
                 (type(declared_exit) is int or declared_exit is None) and
                 (type(observed_exit) is int or observed_exit is None) and
                 complete == (type(declared_exit) is int))
        if not valid:
            problems.append({'severity': 'invalid', 'stage': label,
                             'error': 'malformed command result'})
        for stream in _STREAMS:
            name = command_log_name(index, stream)
            digest = result.get(stream + '_sha256')
            if result.get(stream) != name or not isinstance(digest, str):
                problems.append({'severity': 'invalid', 'stage': label,
                                 'error': 'malformed captured log reference'})
                continue
            log = directory / name
            if log.is_symlink() or not log.is_file():
                problems.append({'severity': 'invalid', 'stage': label,
                                 'error': 'captured %s log is missing' % stream})
            elif file_sha256(log) != digest:
                problems.append({'severity': 'invalid', 'stage': label,
                                 'error': 'captured %s log hash mismatch' % stream})
            elif type(result.get(stream + '_bytes')) is not int or \
                    log.stat().st_size != result[stream + '_bytes']:
                problems.append({'severity': 'invalid', 'stage': label,
                                 'error': 'captured %s log size mismatch' % stream})
        results[index] = result
        entry.update(status='captured', transport=result.get('transport'),
                     transport_complete=complete, declared_exit_code=declared_exit,
                     transport_returncode=observed_exit,
                     exit_state='known' if complete and type(declared_exit) is int else 'unknown')
        commands.append(entry)
    values = {}
    for name in (BEFORE_IDENTITY, BEFORE_MANIFEST, POST_MANIFEST, POST_IDENTITY):
        stage = _load(directory, name, problems, name, missing=None)
        if stage is None:
            if require_chain:
                problems.append({'severity': 'invalid', 'stage': _STAGES[name],
                                 'error': 'completed attempt is missing this stage'})
            continue
        if (stage.get('version') != 1 or stage.get('kind') != 'stage' or
                stage.get('attempt_id') != attempt_id or
                stage.get('stage') != _STAGES[name] or
                not isinstance(stage.get('value'), dict)):
            problems.append({'severity': 'invalid', 'stage': name,
                             'error': 'malformed stage record'})
        else:
            stages.append(stage['stage'])
            values[_STAGES[name]] = stage['value']
    report = None
    report_value = None
    binding = _load(directory, BINDING, problems, 'report-binding', missing=None)
    if binding is None:
        problems.append({'severity': 'incomplete', 'stage': 'report-binding',
                         'error': 'no report binding'})
    else:
        report_path = directory / REPORT
        valid = (binding.get('version') == 1 and binding.get('kind') == 'report_binding' and
                 binding.get('attempt_id') == attempt_id and
                 binding.get('report') == REPORT and
                 isinstance(binding.get('report_sha256'), str) and
                 binding.get('top_level') == 'tests.json' and
                 binding.get('top_level_sha256') == binding.get('report_sha256') and
                 report_path.is_file() and not report_path.is_symlink())
        if valid:
            valid = file_sha256(report_path) == binding['report_sha256']
        if valid:
            report_value = _load_file(report_path, problems, 'report')
            valid = report_value is not None
        if not valid:
            problems.append({'severity': 'invalid', 'stage': 'report-binding',
                             'error': 'malformed or mismatched report binding'})
        report = {'name': REPORT, 'sha256': binding.get('report_sha256'),
                  'top_level': binding.get('top_level'),
                  'top_level_sha256': binding.get('top_level_sha256'), 'valid': valid}
    completed_ok = False
    if completed is None:
        problems.append({'severity': 'incomplete', 'stage': 'completed',
                         'error': 'no completion marker'})
    else:
        binding_path = directory / BINDING
        completed_ok = (completed.get('version') == 1 and
                        completed.get('kind') == 'completed' and
                        completed.get('attempt_id') == attempt_id and
                        completed.get('binding') == BINDING and
                        isinstance(completed.get('binding_sha256'), str) and
                        isinstance(completed.get('report_sha256'), str) and
                        binding_path.is_file() and not binding_path.is_symlink() and
                        file_sha256(binding_path) == completed['binding_sha256'] and
                        (report is None or completed['report_sha256'] == report['sha256']))
        if not completed_ok:
            problems.append({'severity': 'invalid', 'stage': 'completed',
                             'error': 'malformed or mismatched completion marker'})
    if completed_ok and report_value is not None and usable:
        _check_report_binding(intent, values, declared, results, report_value, problems)
    failure = _load(directory, FAILURE, problems, 'failure', missing=None)
    if failure is not None:
        problems.append({'severity': 'incomplete', 'stage': 'failure',
                         'error': '%s: %s at %s' % (failure.get('error_type'),
                                                   failure.get('message'), failure.get('stage'))})
    return _attempt_record(attempt_id, sequence, problems, stages, commands,
                           report, completed_ok)


def _legacy_report_known(run, report, logs, problems):
    """A pre-receipt report counts as known only if every declared result is bound."""
    def bad(stage, message):
        problems.append({'severity': 'invalid', 'stage': stage, 'error': message})

    task = _load_file(Path(run) / 'task.json', problems, 'task')
    applied = _load_file(Path(run) / 'applied.json', problems, 'applied')
    declared = task.get('test_commands') if task is not None else None
    tests = report.get('tests')
    if not isinstance(declared, list) or not isinstance(tests, list):
        bad('tests.json', 'legacy report or task commands are not arrays')
        return False
    if len(tests) != len(declared):
        bad('tests.json', 'legacy report does not cover the declared commands')
    version = report.get('evidence_version')
    if version not in (None, 1):
        bad('tests.json', 'unsupported legacy report evidence version')
    strict = version == 1
    referenced = set()
    for index, (result, argv) in enumerate(zip(tests, declared)):
        label = 'test-%d' % index
        if not isinstance(result, dict):
            bad(label, 'malformed legacy result')
            continue
        if result.get('argv') != argv:
            bad(label, 'legacy result argv differs from the declared command')
        if type(result.get('exit_code')) is not int:
            bad(label, 'legacy result has no known numeric exit')
        if strict:
            # The strict pre-receipt writer recorded these fields; a report
            # missing any of them never captured a complete result.
            if result.get('transport_complete') is not True:
                bad(label, 'strict legacy result transport did not complete')
            if result.get('exit_scope') != 'declared_test_process':
                bad(label, 'strict legacy result exit scope is not the declared test process')
        elif result.get('transport_complete') is False:
            bad(label, 'legacy result transport did not complete')
        for stream in _STREAMS:
            name = 'test-%d.%s' % (index, stream)
            if result.get(stream) != name:
                bad(label, 'legacy result log reference differs from the declared log name')
                continue
            referenced.add(name)
            path = Path(run) / name
            if path.is_symlink() or not path.is_file():
                bad(label, 'legacy %s log is missing' % stream)
                continue
            digest = result.get(stream + '_sha256')
            if strict and not isinstance(digest, str):
                bad(label, 'strict legacy %s log hash is missing' % stream)
            elif isinstance(digest, str) and file_sha256(path) != digest:
                bad(label, 'legacy %s log hash mismatch' % stream)
    if type(report.get('passed')) is not bool:
        bad('tests.json', 'malformed legacy passed flag')
    elif report['passed']:
        if report.get('unchanged') is not True:
            bad('tests.json', 'a changed manifest cannot report a passed test')
        for index, result in enumerate(tests):
            if not isinstance(result, dict) or result.get('exit_code') != 0:
                bad('test-%d' % index, 'a passed report requires a known zero exit')
    if set(logs) != referenced:
        bad('tests.json', 'legacy test logs do not match the saved report')
    if applied is not None:
        if not _same_identity(report.get('host'), applied.get('source')):
            bad('tests.json', 'legacy report host differs from applied.json')
        if report.get('fingerprint') != applied.get('fingerprint'):
            bad('tests.json', 'legacy report fingerprint differs from applied.json')
    return not problems


def _legacy(run, recorded, marked):
    delivery = None
    metadata = run / 'metadata.json'
    if metadata.is_file() and not metadata.is_symlink():
        try:
            value = json.loads(metadata.read_bytes())
            if isinstance(value, dict):
                delivery = value.get('delivery')
        except (OSError, ValueError, UnicodeDecodeError):
            delivery = None
    tests_path = run / 'tests.json'
    tests_present = tests_path.is_file() and not tests_path.is_symlink()
    logs = []
    try:
        for entry in run.iterdir():
            if _TEST_LOG.fullmatch(entry.name) and entry.is_file() and not entry.is_symlink():
                logs.append(entry.name)
    except OSError:
        pass
    logs.sort()
    base = {'attempts_recorded': recorded, 'delivery': delivery,
            'tests_json_present': tests_present, 'test_logs': logs}
    if recorded:
        return dict(base, tests_json_known=False, unresolved=False, reason=None, errors=[])
    if marked:
        # The run recorded receipt evidence, so an empty attempt tree means the
        # modern capture disappeared; a legacy-shaped tests.json cannot stand in.
        return dict(base, tests_json_known=False, unresolved=True,
                    reason='modern_receipt_evidence_missing', errors=[])
    if not tests_present:
        unresolved = bool(logs) or delivery == 'testing'
        reason = None
        if unresolved:
            reason = ('orphan_test_logs_without_receipts' if logs
                      else 'delivery_testing_without_tests_json')
        return dict(base, tests_json_known=False, unresolved=unresolved,
                    reason=reason, errors=[])
    problems = []
    report = _load_file(tests_path, problems, 'tests.json')
    known = report is not None and _legacy_report_known(run, report, logs, problems)
    if known and not problems:
        return dict(base, tests_json_known=True, unresolved=False, reason=None, errors=[])
    return dict(base, tests_json_known=False, unresolved=True,
                reason='malformed_or_incomplete_legacy_tests_json',
                errors=[item['error'] for item in problems])


def test_receipts(run_path):
    """Read only local receipt/log bytes; never touch the target or a config."""
    run = Path(run_path)
    root = run / ATTEMPTS
    attempts = []
    if root.is_dir() and not root.is_symlink():
        for entry in root.iterdir():
            if _HEX.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink():
                attempts.append(_inspect(entry, run))
    attempts.sort(key=lambda item: (item['sequence'] is None, item['sequence'] or 0,
                                    item['attempt_id']))
    marked = _run_marker(run) == RUN_MARKER_VERSION
    return {'version': 1, 'root': ATTEMPTS, 'attempts': attempts,
            'modern_receipts_expected': marked,
            'legacy': _legacy(run, bool(attempts), marked)}


def _run_marker(run):
    meta = _load_file(Path(run) / 'metadata.json', [], 'metadata', missing=None)
    return meta.get(RUN_MARKER) if isinstance(meta, dict) else None


def require_retest_allowed(run):
    """Fail closed instead of replaying evidence whose outcome was not captured."""
    state = test_receipts(run)
    attempts = state['attempts']
    if attempts:
        latest = attempts[-1]
        if latest['state'] == 'invalid':
            raise ReceiptError(
                'Latest target-test attempt evidence is malformed; unresolved target-test '
                'evidence must not be replayed (inspect test-receipts and use a fresh authorized task)')
        if latest['state'] != 'completed':
            raise ReceiptError(
                'Latest target-test attempt is incomplete; unresolved target-test evidence '
                'must not be replayed (inspect test-receipts and use a fresh authorized task)')
        unknown = [command['index'] for command in latest['commands']
                   if command['exit_state'] != 'known']
        if unknown:
            raise ReceiptError(
                'Latest target-test attempt has unknown transport for command(s) %s; '
                'unresolved target-test evidence must not be replayed '
                '(inspect test-receipts and use a fresh authorized task)' % (unknown,))
    elif state['modern_receipts_expected']:
        # The run recorded receipts, so a complete top-level report alone is not
        # evidence: its attempt records disappeared and must not be replayed.
        raise ReceiptError(
            'This run recorded target-test receipt evidence but its attempt records are '
            'missing; unresolved target-test evidence must not be replayed '
            '(inspect test-receipts and use a fresh authorized task)')
    elif state['legacy']['unresolved']:
        raise ReceiptError(
            'Legacy target-test evidence (%s) is unresolved; do not replay it '
            '(inspect test-receipts and use a fresh authorized task)'
            % state['legacy']['reason'])
    return state


def require_report_receipt(run):
    """Accept new evidence only through a complete receipt bound to tests.json.

    Runs whose delivery predates receipts keep their existing review/finalize
    gates; this gate only governs attempts that recorded receipt evidence.
    """
    state = test_receipts(run)
    attempts = state['attempts']
    if not attempts:
        if not state['modern_receipts_expected']:
            return None
        raise ReceiptError(
            'This run recorded target-test receipt evidence but its attempt records are '
            'missing; acceptance requires the original captured evidence '
            '(use a fresh authorized task)')
    latest = attempts[-1]
    if latest['state'] != 'completed':
        raise ReceiptError(
            'Latest target-test attempt is %s; acceptance requires a complete receipt '
            '(inspect test-receipts and use a fresh authorized task)' % latest['state'])
    unknown = [command['index'] for command in latest['commands']
               if command['exit_state'] != 'known']
    if unknown:
        raise ReceiptError(
            'Latest target-test attempt has unknown transport for command(s) %s; '
            'acceptance requires a known declared-test exit' % (unknown,))
    report = latest.get('report') or {}
    try:
        digest = file_sha256(Path(run) / 'tests.json')
    except OSError:
        raise ReceiptError(
            'Current target-test tests.json is missing; its target-test receipt cannot '
            'be validated') from None
    if (report.get('valid') is not True or report.get('top_level') != 'tests.json' or
            report.get('sha256') != digest or report.get('top_level_sha256') != digest):
        raise ReceiptError(
            'Latest target-test receipt does not bind the current tests.json; '
            'acceptance requires matching captured evidence')
    return latest
