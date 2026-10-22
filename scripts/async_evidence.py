"""OMP 18.2.8 async Bash delivery evidence, not child-process/cleanup proof."""
import hashlib
import json
from pathlib import Path

POLICY_KEY = 'async_execution_policy'
POLICY = {'version': 2, 'identity': 'tool_call_id', 'job_id_reuse': 'after_delivery',
          'duplicate_packets': 'reject'}


def protocol_version(task, meta):
    if POLICY_KEY not in task and POLICY_KEY not in meta:
        return 1
    markers = (task.get(POLICY_KEY), meta.get(POLICY_KEY))
    process = task.get('process_evidence_policy')
    if (any(not isinstance(value, dict) or type(value.get('version')) is not int or value != POLICY
            for value in markers) or not isinstance(process, dict) or
            type(process.get('version')) is not int or process['version'] < 2):
        raise ValueError('Async execution policy changed; prepare a fresh run')
    return 2


def version_for_run(run):
    run = Path(run)
    values = [json.loads((run/name).read_text()) if (run/name).exists() else {}
              for name in ('task.json', 'metadata.json')]
    return protocol_version(*values)


def value_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode()).hexdigest()


def job_id(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def bash_async(result):
    details = result.get('details') if isinstance(result, dict) else None
    if not isinstance(details, dict) or 'async' not in details:
        return None
    data = details['async']
    if (not isinstance(data, dict) or not job_id(data.get('jobId')) or data.get('type') != 'bash' or
            data.get('state') not in ('queued', 'running', 'completed', 'failed', 'cancelled')):
        return {'invalid': True}
    return {'job_id': data['jobId'], 'state': data['state'], 'type': 'bash',
            'result_sha256': value_sha(result)}


def async_notice(message):
    if not isinstance(message, dict) or message.get('role') != 'custom' or message.get('customType') != 'async-result':
        return None
    details = message.get('details')
    jobs = details.get('jobs') if isinstance(details, dict) else None
    if not isinstance(jobs, list) or not jobs or len(jobs) > 256:
        return {'invalid': True, 'message_sha256': value_sha(message)}
    selected, seen = [], set()
    for item in jobs:
        if (not isinstance(item, dict) or not job_id(item.get('jobId')) or item['jobId'] in seen or
                item.get('type') is not None and not isinstance(item['type'], str)):
            return {'invalid': True, 'message_sha256': value_sha(message)}
        seen.add(item['jobId'])
        selected.append({'job_id': item['jobId'], 'type': item.get('type')})
    content = message.get('content')
    has_text = isinstance(content, str) and bool(content.strip()) or (
        isinstance(content, list) and any(isinstance(p, dict) and p.get('type') == 'text' and
            isinstance(p.get('text'), str) and p['text'].strip() for p in content))
    return {'jobs': selected, 'message_sha256': value_sha(message), 'invalid': not has_text}


class LegacyAsyncTracker:
    """Match structured IDs only. A delivered result can describe failure."""
    def __init__(self):
        self.jobs = {}
        self.errors = []

    def event(self, item):
        if item.get('type') == 'tool_execution_end' and item.get('toolName') == 'bash':
            descriptor = item.get('asyncBash')
            if descriptor is not None:
                if descriptor.get('invalid') or not job_id(item.get('toolCallId')):
                    self.errors.append('invalid_async_bash_metadata')
                elif descriptor['job_id'] in self.jobs:
                    self.errors.append('duplicate_async_job_id')
                else:
                    self.jobs[descriptor['job_id']] = dict(descriptor, tool_call_id=item['toolCallId'],
                        delivery='pending' if descriptor['state'] in ('queued', 'running') else 'inline_terminal',
                        notice_sha256=None)
        notice = item.get('asyncResult')
        if notice is None:
            return
        if notice.get('invalid'):
            self.errors.append('invalid_async_result_metadata')
            return
        for entry in notice['jobs']:
            if entry['type'] not in (None, 'bash'):
                continue
            job = self.jobs.get(entry['job_id'])
            if job is None:
                self.errors.append('unmatched_async_result')
            elif job['delivery'] != 'pending':
                self.errors.append('duplicate_async_result')
            else:
                job.update(delivery='delivered', notice_sha256=notice['message_sha256'])

    def summary(self):
        return {'version': 1, 'jobs': list(self.jobs.values()),
                'pending': [k for k, v in self.jobs.items() if v['delivery'] == 'pending'],
                'errors': list(self.errors), 'scope': 'delivery_only_not_exit_success_or_resource_cleanup'}


class AsyncTracker(LegacyAsyncTracker):
    """V2 retains separate calls when OMP recycles an already delivered job ID."""
    def __init__(self, version=1):
        if type(version) is not int or version not in (1, 2):
            raise ValueError('Unsupported async execution protocol')
        super().__init__()
        self.version = version
        self.sequence = 0
        self.starts, self.current, self.closed = {}, {}, {}
        self.ends, self.notices = set(), set()
        self.generations = {}

    def event(self, item):
        if self.version == 1:
            return super().event(item)
        self.sequence += 1
        call = item.get('toolCallId')
        if item.get('toolName') == 'bash':
            if item.get('type') == 'tool_execution_start':
                if not job_id(call) or call in self.starts:
                    self.errors.append('invalid_or_duplicate_async_tool_call_id')
                else:
                    self.starts[call] = self.sequence
            elif item.get('type') == 'tool_execution_end':
                if not job_id(call) or call not in self.starts or call in self.ends:
                    self.errors.append('invalid_or_duplicate_async_tool_end')
                    return
                self.ends.add(call)
                descriptor = item.get('asyncBash')
                if descriptor is not None:
                    if (not isinstance(descriptor, dict) or descriptor.get('invalid') or
                            not job_id(descriptor.get('job_id')) or descriptor.get('type') != 'bash' or
                            descriptor.get('state') not in ('queued', 'running', 'completed', 'failed', 'cancelled') or
                            not isinstance(descriptor.get('result_sha256'), str)):
                        self.errors.append('invalid_async_bash_metadata')
                        return
                    jid = descriptor['job_id']
                    previous = self.current.get(jid)
                    if previous is not None and (previous not in self.closed or
                            self.starts[call] <= self.closed[previous]):
                        self.errors.append('async_job_id_reused_before_delivery')
                        return
                    generation = self.generations.get(jid, 0) + 1
                    self.generations[jid] = generation
                    self.current[jid] = call
                    pending = descriptor['state'] in ('queued', 'running')
                    self.jobs[call] = dict(descriptor, tool_call_id=call, generation=generation,
                        delivery='pending' if pending else 'inline_terminal', notice_sha256=None)
                    if not pending:
                        self.closed[call] = self.sequence
        notice = item.get('asyncResult')
        if notice is None:
            return
        if (not isinstance(notice, dict) or notice.get('invalid') or
                not isinstance(notice.get('jobs'), list) or not notice['jobs'] or
                not isinstance(notice.get('message_sha256'), str)):
            self.errors.append('invalid_async_result_metadata')
            return
        packet = notice['message_sha256']
        if packet in self.notices:
            self.errors.append('duplicate_async_result')
            return
        self.notices.add(packet)
        selected, seen = [], set()
        for entry in notice['jobs']:
            if not isinstance(entry, dict) or not job_id(entry.get('job_id')) or entry['job_id'] in seen:
                self.errors.append('invalid_async_result_metadata')
                return
            seen.add(entry['job_id'])
            if entry.get('type') not in (None, 'bash'):
                continue
            call = self.current.get(entry['job_id'])
            if call is None:
                self.errors.append('unmatched_async_result')
                return
            if self.jobs[call]['delivery'] != 'pending':
                self.errors.append('duplicate_async_result')
                return
            selected.append(call)
        # A malformed batch must not partially close jobs.
        for call in selected:
            self.jobs[call].update(delivery='delivered', notice_sha256=packet)
            self.closed[call] = self.sequence

    def summary(self):
        if self.version == 1:
            return super().summary()
        return {'version': 2, 'jobs': list(self.jobs.values()),
                'pending': [call for call, job in self.jobs.items() if job['delivery'] == 'pending'],
                'errors': list(self.errors), 'scope': 'delivery_only_not_exit_success_or_resource_cleanup'}


def validate_binding(events, tools, version=1):
    tracker = AsyncTracker(version)
    if version == 2:
        private_order, compact_order = [], []
        for row in tools:
            if row.get('type') in ('tool_execution_start', 'tool_execution_end'):
                private_order.append((row['type'], row.get('id')))
            elif row.get('type') == 'async_result':
                private_order.append(('async_result', value_sha(row.get('message'))))
        for row in events:
            if row.get('toolName') == 'bash' and row.get('type') in ('tool_execution_start', 'tool_execution_end'):
                compact_order.append((row['type'], row.get('toolCallId')))
            if 'asyncResult' in row:
                compact_order.append(('async_result', row['asyncResult'].get('message_sha256')))
        if private_order != compact_order:
            raise RuntimeError('Async private/compact event order differs')
    raw_ends = {row.get('id'): row for row in tools if row.get('type') == 'tool_execution_end'}
    notices = [async_notice(row.get('message')) for row in tools if row.get('type') == 'async_result']
    observed = []
    for event in events:
        if event.get('type') == 'tool_execution_end' and event.get('toolName') == 'bash':
            if ('asyncBash' not in event or event['asyncBash'] !=
                    bash_async(raw_ends[event['toolCallId']].get('result'))):
                raise RuntimeError('Async Bash result binding is missing or inconsistent')
        if 'asyncResult' in event:
            if event.get('type') != 'message_end' or event.get('message', {}).get('role') != 'custom':
                raise RuntimeError('Async notification has invalid provenance')
            observed.append(event['asyncResult'])
        tracker.event(event)
    if notices != observed or any(x is None for x in notices):
        raise RuntimeError('Private async notifications do not match compact evidence')
    summary = tracker.summary()
    if summary['pending'] or summary['errors']:
        raise RuntimeError('Async Bash terminal delivery is unresolved: ' + ','.join(summary['errors'] or ['pending']))
    return summary


def validate_lifecycle(run, summary, assessment):
    """Require scoped coordinator observations, hash-bound and read by Sol.

    Receipts remain cooperative evidence; this does not authenticate their author
    or inspect arbitrary host paths/processes.
    """
    entries = assessment.get('async_jobs')
    jobs = summary['jobs']
    if not isinstance(entries, list) or len(entries) != len(jobs):
        raise RuntimeError('Require explicit outcome and lifecycle review for every async Bash job')
    by_call = summary.get('version') == 2
    def identity(entry):
        return (entry.get('job_id'), entry.get('tool_call_id')) if by_call else entry.get('job_id')
    reviewed = {}
    for entry in entries:
        if (not isinstance(entry, dict) or not job_id(entry.get('job_id')) or
                by_call and not job_id(entry.get('tool_call_id')) or identity(entry) in reviewed):
            raise RuntimeError('Invalid/duplicate async job review')
        reviewed[identity(entry)] = entry
    if set(reviewed) != {identity(job) for job in jobs}:
        raise RuntimeError('Async job review identities differ')
    for job in jobs:
        entry = reviewed[identity(job)]
        outcome, code = entry.get('outcome'), entry.get('exit_code')
        if (outcome not in ('succeeded', 'failed', 'cancelled') or 'exit_code' not in entry or
                code is not None and type(code) is not int or outcome == 'succeeded' and code != 0 or
                outcome != 'succeeded' and code == 0 or
                not isinstance(entry.get('reason'), str) or not entry['reason'].strip()):
            raise RuntimeError('Async outcome/exit status is unverified')
        reference = entry.get('lifecycle_receipt')
        if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
            raise RuntimeError('Require coordinator lifecycle receipt')
        relative = Path(reference['path'])
        if relative.is_absolute() or not relative.parts or '..' in relative.parts:
            raise RuntimeError('Lifecycle receipt must be inside this run')
        path = Path(run)
        for part in relative.parts:
            path /= part
            if path.is_symlink():
                raise RuntimeError('Linked lifecycle receipt is invalid')
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise RuntimeError('Lifecycle receipt missing or oversized')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference.get('sha256'):
            raise RuntimeError('Lifecycle receipt changed')
        receipt = json.loads(raw)
        if (receipt.get('version') != 1 or receipt.get('observer') != 'coordinator' or
                receipt.get('run') != str(Path(run).resolve()) or receipt.get('execution') != job or
                not isinstance(receipt.get('observed_at'), str) or not receipt['observed_at'].strip()):
            raise RuntimeError('Lifecycle receipt identity/evidence is unbound')
        for key, statuses in (('children', ('not_created', 'stopped')),
                              ('scratch', ('not_used', 'removed', 'authorized_retained'))):
            detail = receipt.get(key)
            if (not isinstance(detail, dict) or detail.get('status') not in statuses or
                    not isinstance(detail.get('evidence'), str) or not detail['evidence'].strip()):
                raise RuntimeError('Async child/scratch lifecycle remains unverified')
            if key == 'scratch':
                paths = detail.get('paths')
                if (not isinstance(paths, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths) or
                        (detail['status'] != 'not_used' and not paths) or (detail['status'] == 'not_used' and paths)):
                    raise RuntimeError('Async scratch paths remain unverified')
