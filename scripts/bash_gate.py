"""Frozen, bounded pre-execution Bash gate for NEW strict OMP runs only."""
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

KEY = 'worker_bash_gate'
EXTENSION = 'bash-gate.ts'
CONFIG = 'bash-gate-config.json'
JOURNAL = 'bash-gate-events.jsonl'
OMP_VERSION = '18.2.8'
MAX_BYTES = 16 * 1024 * 1024


class GateError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise GateError('Missing, linked or oversized gate evidence: ' + path.name)
    return path.read_bytes()


def prepare(task, run):
    rules = task['worker_bash_policy']
    if rules['mode'] != 'declared_tests_only':
        task.pop(KEY, None)
        return
    data = Path(__file__).with_suffix('.ts').read_bytes()
    (run / EXTENSION).write_bytes(data)
    from worker_test_catalog import catalog
    task[KEY] = {'version': 2, 'omp_version': OMP_VERSION, 'max_denials': 3,
                 'extension_sha256': sha(data), 'rules_sha256': sha(encoded(rules)),
                 'catalog_sha256': sha(encoded(catalog(task, run)))}


def validate(task, meta, run):
    if KEY not in task and KEY not in meta:
        if any((owner.get('process_evidence_policy') or {}).get('version') == 4 for owner in (task, meta)):
            raise GateError('Process evidence v4 requires frozen pre-execution gate')
        return None
    saved = task.get(KEY)
    rules = task.get('worker_bash_policy', {})
    from process_evidence import POLICY_GATE
    if (not isinstance(saved, dict) or not isinstance(rules, dict) or rules.get('version') != 2 or rules.get('mode') != 'declared_tests_only' or
            task.get('process_evidence_policy') != POLICY_GATE or meta.get('process_evidence_policy') != POLICY_GATE):
        raise GateError('Invalid frozen gate policy')
    version = saved.get('version')
    if type(version) is not int or version not in (1, 2):
        raise GateError('Unsupported frozen gate version')
    expected = {'version': version, 'omp_version': OMP_VERSION, 'max_denials': 3,
                'extension_sha256': sha(read(run / EXTENSION)), 'rules_sha256': sha(encoded(rules))}
    if version == 2:
        from worker_test_catalog import catalog, validate_catalog
        try:
            entries = validate_catalog(catalog(task, run), rules)
        except ValueError as error:
            raise GateError('Invalid exact-test catalog') from error
        expected['catalog_sha256'] = sha(encoded(entries))
    if encoded(saved) != encoded(expected) or encoded(meta.get(KEY)) != encoded(expected):
        raise GateError('Gate policy or extension changed; prepare a fresh run')
    return expected


def launch(run, task, meta, env, argv, version):
    policy = validate(task, meta, run)
    if policy is None:
        return
    if version not in (OMP_VERSION, 'omp v' + OMP_VERSION, 'omp/' + OMP_VERSION):
        raise GateError('Gate requires tested OMP ' + OMP_VERSION)
    if (run / JOURNAL).exists() or (run / JOURNAL).is_symlink():
        raise GateError('Preserve existing gate attempt; prepare a fresh run')
    config = {'version': policy['version'], 'nonce': uuid.uuid4().hex, 'policy_sha256': sha(encoded(policy)),
              'rules': task['worker_bash_policy'], 'max_denials': policy['max_denials'],
              'journal': str(run / JOURNAL)}
    if policy['version'] == 2:
        from worker_test_catalog import catalog
        config['test_catalog'] = catalog(task, run)
    fd = os.open(run / CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(encoded(config))
    env['TEAM_DEV_GATE_CONFIG'] = str(run / CONFIG)
    argv.extend(['--extension', str(run / EXTENSION)])


class Gate:
    def __init__(self, run, policy, rules):
        self.run, self.policy, self.rules = Path(run), policy, rules
        self.config_bytes = read(self.run / CONFIG)
        self.config = json.loads(self.config_bytes)
        expected = dict(version=policy['version'], nonce=self.config.get('nonce'), policy_sha256=sha(encoded(policy)),
                        rules=rules, max_denials=3, journal=str(self.run / JOURNAL))
        self.catalog = []
        if policy['version'] == 2:
            from worker_test_catalog import validate_catalog
            try:
                self.catalog = validate_catalog(self.config.get('test_catalog'), rules)
            except ValueError as error:
                raise GateError('Invalid exact-test catalog') from error
            if sha(encoded(self.catalog)) != policy.get('catalog_sha256'):
                raise GateError('Exact-test catalog changed')
            expected['test_catalog'] = self.catalog
        if (encoded(self.config) != encoded(expected) or not isinstance(self.config['nonce'], str) or
                len(self.config['nonce']) != 32 or any(c not in '0123456789abcdef' for c in self.config['nonce'])):
            raise GateError('Gate launch config mismatch')
        self.entries, self.lockouts, self.denials, self.raw = {}, {}, 0, b''
        self.lock = threading.RLock()

    def refresh(self, *, complete=False):
        with self.lock:
            return self._refresh(complete=complete)

    def _refresh(self, *, complete=False):
        from worker_bash import inspect
        raw = read(self.run / JOURNAL)
        if not raw.startswith(self.raw):
            raise GateError('Gate journal changed')
        if complete and not raw.endswith(b'\n'):
            raise GateError('Incomplete gate journal')
        raw = raw[:raw.rfind(b'\n') + 1]
        if any(len(line) > 1024 * 1024 for line in raw.splitlines()):
            raise GateError('Oversized gate record')
        rows = [json.loads(line) for line in raw.splitlines()]
        ready = dict(type='ready', version=1, nonce=self.config['nonce'], policy_sha256=sha(encoded(self.policy)))
        if not rows or encoded(rows[0]) != encoded(ready):
            raise GateError('Gate did not register before prompt')
        entries, lockouts, denials = {}, {}, 0
        for row in rows[1:]:
            if not isinstance(row, dict) or row.get('type') not in ('decision','lockout'):
                raise GateError('Invalid gate record')
            call_id = row.get('id')
            if not isinstance(call_id, str) or not call_id or call_id in entries or call_id in lockouts:
                raise GateError('Missing or duplicate gate call id')
            if row['type'] == 'lockout':
                name=row.get('tool_name')
                reason=f"TEAM_DEV_GATE_EXHAUSTED:{self.config['nonce']}:{call_id}: Tool NOT executed; correction budget exhausted."
                expected=dict(type='lockout',tool_name=name,id=call_id,args=row.get('args'),reason=reason)
                if (denials < 3 or not isinstance(name,str) or not name or name == 'bash' or
                        not isinstance(row.get('args'),dict) or encoded(row) != encoded(expected)):
                    raise GateError('Invalid non-Bash lockout record')
                lockouts[call_id]=row
                continue
            status = inspect(row.get('args'), self.rules)['status']
            denials += status != 'allowed'
            decision = 'allow' if status == 'allowed' and denials < 3 else 'block'
            reason = (f"TEAM_DEV_GATE_BLOCK:{self.config['nonce']}:{call_id}:{denials}: Command was NOT executed. "
                      'Use read/grep/glob for inspection, edit/write for source, or copy an exact prepared test command and its permitted arguments. ' +
                      ('Correction budget exhausted; stop.' if denials >= 3 else 'Correct the tool choice; do not repeat this request.'))
            if self.policy['version'] == 2 and decision == 'block':
                from worker_test_catalog import correction_hint
                reason += ' ' + correction_hint(row.get('args'), self.rules, self.catalog)
            expected = dict(type='decision', id=call_id, args=row.get('args'), status=status,
                            decision=decision, denials=denials, reason=reason if decision == 'block' else None)
            if encoded(row) != encoded(expected):
                raise GateError('Gate decision does not match frozen rules')
            entries[call_id] = row
        self.entries, self.lockouts, self.denials, self.raw = entries, lockouts, denials, raw
        return self.summary()

    def annotation(self, call_id, args, tool_name='bash'):
        self.refresh()
        row = self.entries.get(call_id) if tool_name == 'bash' else self.lockouts.get(call_id)
        if row is None or row.get('tool_name','bash') != tool_name or encoded(row['args']) != encoded(args):
            raise GateError('Bash event missing matching pre-execution decision')
        return {'version': 1, 'decision': row.get('decision','block'), 'record_sha256': sha(encoded(row))}

    def prevented(self, call_id):
        return call_id in self.lockouts or self.entries.get(call_id, {}).get('decision') == 'block'

    def summary(self):
        return {'version': 1, 'bash_decisions': len(self.entries), 'denied_requests': self.denials,
                'exhausted': self.denials >= 3, 'non_bash_lockouts': len(self.lockouts),
                'scope': 'pre_execution_denials_not_executed_violations'}

    def failure_evidence(self):
        """Correlate lockouts after shutdown, without treating an abort as completion."""
        from process_evidence import records
        self.refresh(complete=True)
        if read(self.run / CONFIG) != self.config_bytes:
            raise GateError('Gate config changed')
        event_bytes,events=records(self.run/'events.jsonl')
        tool_bytes,tools=records(self.run/'tool-evidence.jsonl')
        outcomes=[]
        for ident,row in self.lockouts.items():
            start=[x for x in tools if x.get('type')=='gate_lockout_start' and x.get('id')==ident]
            end=[x for x in tools if x.get('type')=='gate_lockout_end' and x.get('id')==ident]
            compact_start=[x for x in events if x.get('type')=='tool_execution_start' and x.get('toolCallId')==ident]
            compact_end=[x for x in events if x.get('type')=='tool_execution_end' and x.get('toolCallId')==ident]
            status='incomplete'
            if all(len(x)==1 for x in (start,end,compact_start,compact_end)):
                annotation={'version':1,'decision':'block','record_sha256':sha(encoded(row))}
                expected_result={'content':[{'type':'text','text':row['reason']}],'details':{}}
                matched=(encoded(start[0].get('args'))==encoded(row['args']) and start[0].get('tool_name')==row['tool_name'] and
                         end[0].get('tool_name')==row['tool_name'] and end[0].get('is_error') is True and
                         end[0].get('result')==expected_result and encoded(compact_start[0].get('preexecGate'))==encoded(annotation) and
                         compact_start[0].get('toolName')==row['tool_name'] and compact_end[0].get('toolName')==row['tool_name'] and
                         compact_end[0].get('isError') is True)
                status='matched' if matched else 'mismatch'
            elif any(len(x)>1 for x in (start,end,compact_start,compact_end)):
                status='mismatch'
            outcomes.append({'id':ident,'tool_name':row['tool_name'],'gate_record_sha256':sha(encoded(row)),
                             'rpc_correlation':status})
        return dict(self.summary(), lockouts=outcomes, journal_sha256=sha(self.raw),
                    events_sha256=sha(event_bytes),tool_evidence_sha256=sha(tool_bytes),
                    policy_sha256=sha(encoded(self.policy)),extension_sha256=self.policy['extension_sha256'],
                    config_sha256=sha(self.config_bytes),acceptance='failed_attempt_not_eligible')

    def binding(self, starts, ends, tool_starts, tool_ends):
        self.refresh(complete=True)
        if read(self.run / CONFIG) != self.config_bytes:
            raise GateError('Gate config changed')
        if set(self.entries) != set(starts):
            raise GateError('Incomplete gate/Bash coverage')
        for call_id, start in starts.items():
            args = tool_starts[call_id].get('args')
            if self.annotation(call_id, args) != start.get('preexecGate'):
                raise GateError('Gate annotation mismatch')
            if self.prevented(call_id):
                end = tool_ends[call_id]
                expected = {'content': [{'type': 'text', 'text': self.entries[call_id]['reason']}], 'details': {}}
                if end.get('is_error') is not True or end.get('result') != expected or ends[call_id].get('isError') is not True:
                    raise GateError('Gate denial result is missing or inconsistent')
        if self.denials >= 3:
            raise GateError('Gate correction budget exhausted')
        return dict(self.summary(), journal_sha256=sha(self.raw), config_sha256=sha(self.config_bytes),
                    policy_sha256=sha(encoded(self.policy)), extension_sha256=self.policy['extension_sha256'])


def audit_event(item, gate):
    """Only a validated gate can remove a prevented request from execution audits."""
    if gate is not None and gate.prevented(item.get('toolCallId')):
        return {}
    return item


def for_run(run):
    run = Path(run)
    task = json.loads(read(run / 'task.json')) if (run / 'task.json').exists() else {}
    meta = json.loads(read(run / 'metadata.json')) if (run / 'metadata.json').exists() else {}
    policy = validate(task, meta, run)
    return Gate(run, policy, task['worker_bash_policy']) if policy else None
