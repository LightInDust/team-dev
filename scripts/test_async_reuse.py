"""Synthetic protocol and receipt checks; no model or business-code execution."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

import async_evidence as a
import process_evidence as p
from rpc_worker import compact_event
from test_async_evidence import notification


def call_events(call, jid='bg_6', state='running'):
    return [NS(type='tool_execution_start', tool_name='bash', tool_call_id=call,
               args={'command': 'echo fixture'}),
            NS(type='tool_execution_end', tool_name='bash', tool_call_id=call, is_error=False,
               result={'content': [{'type': 'text', 'text': 'fixture background result'}],
                       'details': {'async': {'jobId': jid, 'state': state, 'type': 'bash'}}})]


def notice(number, ids=('bg_6',)):
    return NS(type='message_end', message=dict(notification(ids, 'result '+str(number)), timestamp=number))


class AsyncReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.task = {'process_evidence_policy': copy.deepcopy(p.POLICY), a.POLICY_KEY: copy.deepcopy(a.POLICY)}
        for name in ('task.json', 'metadata.json'):
            (self.run/name).write_text(json.dumps(self.task))

    def record(self, events, terminal=True):
        events = list(events) + ([NS(type='agent_end', is_terminal=True)] if terminal else [])
        recorder = p.ToolEvidence(self.run, capture_async=True)
        try:
            for event in events: recorder.observe(event)
        finally:
            recorder.close()
        compact = [compact_event(event) for event in events]
        (self.run/'events.jsonl').write_text(''.join(json.dumps(v)+'\n' for v in compact))
        return compact

    def sequential(self):
        return [*call_events('first'), notice(1), *call_events('second'), notice(2)]

    def test_reused_id_retains_two_calls_and_requires_separate_bound_lifecycle_receipts(self):
        self.record(self.sequential())
        bound = p.binding(self.run, async_required=True)
        summary = bound['async_execution']
        self.assertEqual(summary['version'], 2)
        self.assertEqual([(j['job_id'], j['tool_call_id'], j['generation'], j['delivery']) for j in summary['jobs']],
                         [('bg_6', 'first', 1, 'delivered'), ('bg_6', 'second', 2, 'delivered')])
        self.assertEqual(summary['pending'], []); self.assertEqual(summary['errors'], [])
        entries = []
        for job in summary['jobs']:
            receipt = {'version': 1, 'observer': 'coordinator', 'run': str(self.run.resolve()),
                       'observed_at': 'synthetic receipt', 'execution': job,
                       'children': {'status': 'not_created', 'evidence': 'Synthetic protocol only'},
                       'scratch': {'status': 'not_used', 'paths': [], 'evidence': 'No command executed'}}
            path = self.run/(job['tool_call_id']+'.json'); path.write_text(json.dumps(receipt))
            entries.append({'job_id': job['job_id'], 'tool_call_id': job['tool_call_id'],
                            'outcome': 'failed', 'exit_code': None, 'reason': 'Synthetic failed result, no success inferred',
                            'lifecycle_receipt': {'path': path.name, 'sha256': p.file_sha(path)}})
        a.validate_lifecycle(self.run, summary, {'async_jobs': entries})
        for mutation in ('missing_call', 'wrong_generation_receipt', 'same_call_twice'):
            bad = copy.deepcopy(entries)
            if mutation == 'missing_call': bad[1].pop('tool_call_id')
            elif mutation == 'wrong_generation_receipt': bad[1]['lifecycle_receipt'] = bad[0]['lifecycle_receipt']
            else: bad[1]['tool_call_id'] = 'first'
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                a.validate_lifecycle(self.run, summary, {'async_jobs': bad})

    def test_second_generation_running_return_cannot_complete_or_inherit_old_delivery(self):
        compact = self.record([*call_events('first'), notice(1), *call_events('second')])
        tracker = a.AsyncTracker(2)
        for event in compact: tracker.event(event)
        summary = tracker.summary()
        self.assertEqual(summary['errors'], [])
        self.assertEqual(summary['pending'], ['second'])
        self.assertIsNone(summary['jobs'][1]['notice_sha256'])
        with self.assertRaisesRegex(RuntimeError, 'terminal delivery is unresolved'):
            p.binding(self.run, async_required=True)

    def test_concurrent_reuse_or_start_before_prior_delivery_fails_closed(self):
        second = call_events('second')
        streams = [call_events('first') + call_events('second'),
                   call_events('first') + [second[0], notice(1), second[1]]]
        for stream in streams:
            with self.subTest(events=len(stream)):
                compact = self.record(stream)
                tracker = a.AsyncTracker(2)
                for event in compact: tracker.event(event)
                self.assertIn('async_job_id_reused_before_delivery', tracker.summary()['errors'])
                with self.assertRaises(RuntimeError): p.binding(self.run, async_required=True)

    def test_replayed_old_packet_cannot_close_reused_pending_job(self):
        compact = self.record([*call_events('first'), notice(1), *call_events('second'), notice(1)])
        tracker = a.AsyncTracker(2)
        for event in compact: tracker.event(event)
        self.assertEqual(tracker.summary()['pending'], ['second'])
        self.assertIn('duplicate_async_result', tracker.summary()['errors'])
        with self.assertRaises(RuntimeError): p.binding(self.run, async_required=True)

    def test_duplicate_call_and_bad_notice_batch_do_not_close_unrelated_pending_work(self):
        cases = [call_events('same') + [notice(1)] + call_events('same'),
                 call_events('first') + [notice(1, ('bg_6', 'unknown'))],
                 call_events('first') + [notice(1, ('bg_6', 'bg_6'))]]
        for stream in cases:
            compact = self.record(stream); tracker = a.AsyncTracker(2)
            for event in compact: tracker.event(event)
            self.assertTrue(tracker.summary()['errors'])
            if len(stream) == 3: self.assertEqual(tracker.summary()['pending'], ['first'])
            with self.assertRaises(RuntimeError): p.binding(self.run, async_required=True)

    def test_private_and_compact_order_must_agree_before_reuse_is_accepted(self):
        self.record(self.sequential())
        path = self.run/'tool-evidence.jsonl'; rows = path.read_text().splitlines()
        rows[2], rows[3] = rows[3], rows[2]  # Move next start ahead of prior delivery.
        path.write_text('\n'.join(rows)+'\n')
        with self.assertRaisesRegex(RuntimeError, 'event order differs'):
            p.binding(self.run, async_required=True)

    def test_policy_marker_is_frozen_and_legacy_replay_keeps_duplicate_failure(self):
        compact = self.record(self.sequential())
        legacy = a.AsyncTracker()
        for event in compact: legacy.event(event)
        self.assertIn('duplicate_async_job_id', legacy.summary()['errors'])
        self.assertEqual(legacy.summary()['version'], 1)
        self.assertEqual(a.protocol_version({}, {}), 1)
        for value in (None, {}, dict(a.POLICY, version=1), dict(a.POLICY, version=True)):
            invalid = dict(self.task, **{a.POLICY_KEY: value})
            with self.subTest(value=value), self.assertRaises(ValueError):
                a.protocol_version(invalid, invalid)
        with self.assertRaises(ValueError): a.protocol_version(self.task, {})

    def test_inline_terminal_reuse_still_keeps_each_distinct_result(self):
        self.record([*call_events('first', state='failed'), *call_events('second', state='completed')])
        jobs = p.binding(self.run, async_required=True)['async_execution']['jobs']
        self.assertEqual([j['delivery'] for j in jobs], ['inline_terminal', 'inline_terminal'])
        self.assertEqual([j['state'] for j in jobs], ['failed', 'completed'])
        self.assertTrue(all('exit_code' not in job for job in jobs))

    def test_both_policy_copies_reject_float_versions_and_malformed_marker_types(self):
        for side in ('task', 'metadata'):
            for marker in (dict(a.POLICY, version=2.0), dict(a.POLICY, version=True), None, [], '2'):
                task, meta = copy.deepcopy(self.task), copy.deepcopy(self.task)
                (task if side == 'task' else meta)[a.POLICY_KEY] = marker
                with self.subTest(side=side, marker=marker), self.assertRaises(ValueError):
                    a.protocol_version(task, meta)
        # Check the file-based acceptance path as well as the direct resolver.
        self.record(self.sequential())
        meta = copy.deepcopy(self.task); meta[a.POLICY_KEY]['version'] = 2.0
        (self.run/'metadata.json').write_text(json.dumps(meta))
        with self.assertRaisesRegex(ValueError, 'Async execution policy changed'):
            p.binding(self.run, async_required=True)


if __name__ == '__main__': unittest.main()
