import copy
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from command_audit import inspect_bash
import process_evidence as p


class ProcessEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.task = {'process_evidence_policy':copy.deepcopy(p.POLICY), 'test_commands':[['/usr/bin/true']]}
        self.meta = {'process_evidence_policy':copy.deepcopy(p.POLICY)}

    def evidence(self, command='python3 -B -m unittest -v test_code', args_missing=False):
        args = None if args_missing else {'command':command, 'timeout':10000}
        recorder = p.ToolEvidence(self.run)
        try:
            recorder.observe(NS(type='tool_execution_start',tool_name='bash',tool_call_id='one',args=args))
            recorder.observe(NS(type='tool_execution_end',tool_name='bash',tool_call_id='one',
                                is_error=False,result={'content':[{'type':'text','text':'fixture result'}]}))
        finally:
            recorder.close()
        events = [{'type':'tool_execution_start','toolName':'bash','toolCallId':'one','commandAudit':inspect_bash(args)},
                  {'type':'tool_execution_end','toolName':'bash','toolCallId':'one','isError':False,'asyncBash':None},
                  {'type':'agent_end','isTerminal':True}]
        if p.live_output_enabled(self.task,self.meta):
            from live_output import inspect
            events[0]['liveOutputAudit']=inspect(args)
        self.write_events(events)
        return events

    def write_events(self, events):
        (self.run/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))

    def review(self):
        return {'model':'gpt-5.6-sol','effort':'max','verdict':'pass',
                'process_review':{'verdict':'pass','binding':p.binding(self.run,async_required=True,live_output_required=p.live_output_enabled(self.task,self.meta)),
                                  'async_jobs':[],'reason':'Reviewed fixture requests and results.'}}

    def test_complete_private_parameters_match_compact_hashes(self):
        self.evidence()
        self.assertEqual(stat.S_IMODE((self.run/'tool-evidence.jsonl').stat().st_mode),0o600)
        raw=(self.run/'tool-evidence.jsonl').read_text()
        self.assertIn('timeout',raw)
        self.assertIn('fixture result',raw)
        result=p.binding(self.run)
        self.assertEqual(result['bash_calls'],1)
        p.validate_review(self.run,self.task,self.meta,self.review())

    def test_historical_v1_audit_binding_is_replayed_using_recorded_version(self):
        self.task['process_evidence_policy']=dict(p.POLICY_V2)
        self.meta['process_evidence_policy']=dict(p.POLICY_V2)
        command='python3 -c "print(1)" | head -1'
        events=self.evidence(command)
        events[0]['commandAudit']=inspect_bash({'command':command},version=1)
        self.write_events(events)
        bound=p.binding(self.run)
        self.assertEqual(bound['audit']['version'],1)
        self.assertNotIn('review_cue_counts',bound['audit'])
        review=self.review()
        review['process_review']['coverage_resolution']='Fixture v1 exact args inspected; original review retained.'
        p.validate_review(self.run,self.task,self.meta,review)
        for version in (None, True, 3):
            events[0]['commandAudit']['version']=version
            self.write_events(events)
            with self.assertRaisesRegex(RuntimeError,'Unsupported recorded'):
                p.binding(self.run)

    def test_probe_cues_bind_to_review_without_becoming_automatic_blocked_requests(self):
        self.task['process_evidence_policy']=dict(p.POLICY_V2)
        self.meta['process_evidence_policy']=dict(p.POLICY_V2)
        self.evidence('python3 -c "render(\'/tmp/fixture.png\')" 2>&1 | head -20')
        bound=p.binding(self.run)
        self.assertEqual(bound['blocked_requests'],[])
        self.assertEqual(bound['audit']['review_cue_counts']['output_filter_pipeline_mention'],1)
        self.assertEqual(bound['audit']['review_cue_counts']['python_header_without_explicit_guard_mention'],1)
        with self.assertRaisesRegex(RuntimeError,'explicit Sol disposition'):
            p.validate_review(self.run,self.task,self.meta,self.review())
        events=[json.loads(x) for x in (self.run/'events.jsonl').read_text().splitlines()]
        events[0]['commandAudit']['review_cues']=[]
        self.write_events(events)
        with self.assertRaisesRegex(RuntimeError,'do not match'):
            p.binding(self.run)

    def test_pipeline_request_cannot_be_overridden_by_review_pass(self):
        self.evidence('env PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_code -v 2>&1 | tail -60')
        self.assertEqual(p.binding(self.run)['blocked_requests'],['test_output_pipeline'])
        with self.assertRaisesRegex(RuntimeError,'Forbidden request'):
            p.validate_review(self.run,self.task,self.meta,self.review())

    def test_v3_dependency_filter_cannot_pass_or_resume_and_flag_is_bound(self):
        self.evidence('python3 -B -c "import missing_dependency" 2>&1 | tail -1')
        bound=p.binding(self.run,async_required=True,live_output_required=True)
        self.assertEqual(bound['blocked_requests'],['python_live_output_filter'])
        self.assertEqual(bound['live_output']['python_live_output_filter_count'],1)
        self.assertEqual(p.observed_blocked_requests(self.run,live_output_required=True),['python_live_output_filter'])
        self.assertEqual(p.observed_blocked_requests(self.run),[])
        with self.assertRaisesRegex(RuntimeError,'Forbidden request'):
            p.validate_review(self.run,self.task,self.meta,self.review())
        events=[json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        events[0]['liveOutputAudit']['observations']=[];self.write_events(events)
        with self.assertRaisesRegex(RuntimeError,'Live output parameters'):
            p.binding(self.run,live_output_required=True)

    def test_v3_passive_log_read_keeps_status_distinct_and_can_be_reviewed(self):
        self.evidence('cat saved.log | tail -1')
        bound=p.binding(self.run,live_output_required=True)
        self.assertEqual(bound['blocked_requests'],[])
        self.assertEqual(bound['live_output']['python_live_output_filter_count'],0)
        p.validate_review(self.run,self.task,self.meta,self.review())

    def test_only_v3_enables_live_output_rule_while_v2_retains_async(self):
        for policy,live,async_required in ((p.POLICY_V1,False,False),(p.POLICY_V2,False,True),(p.POLICY,True,True)):
            task={'process_evidence_policy':dict(policy)}
            self.assertEqual(p.live_output_enabled(task,task),live)
            self.assertEqual(p.async_enabled(task,task),async_required)

    def test_unsupported_and_tmp_need_actual_parameters_and_disposition(self):
        self.evidence('cat "/tmp/authorized-input"')
        review=self.review()
        with self.assertRaisesRegex(RuntimeError,'explicit Sol disposition'):
            p.validate_review(self.run,self.task,self.meta,review)
        review['process_review'].update(coverage_resolution='Exact quoted Bash args reviewed; simple input read.',
                                       temporary_path_resolution='Synthetic authorized read only; no scratch created.')
        p.validate_review(self.run,self.task,self.meta,review)
        for verdict in ('unresolved','violations',None):
            review['process_review']['verdict']=verdict
            with self.assertRaises(RuntimeError):p.validate_review(self.run,self.task,self.meta,review)

    def test_missing_parameters_do_not_become_clean_from_zero_counts(self):
        self.evidence(args_missing=True)
        with self.assertRaisesRegex(RuntimeError,'parameters unavailable'):p.binding(self.run)

    def test_quoted_dependency_probe_zero_guard_count_is_not_reviewed_compliance(self):
        self.evidence('ls -la && /usr/bin/python3 -c "import PIL; print(PIL.__version__)"')
        review=self.review();audit=review['process_review']['binding']['audit']
        self.assertEqual(audit['observation_counts']['python_without_explicit_bytecode_guard'],0)
        self.assertEqual(audit['status_counts']['unsupported_syntax'],1)
        self.assertEqual(audit['compliance'],'not_assessed')
        with self.assertRaisesRegex(RuntimeError,'explicit Sol disposition'):
            p.validate_review(self.run,self.task,self.meta,review)

    def test_incomplete_mismatched_duplicate_and_reordered_events_fail(self):
        events=self.evidence()
        for modified in (events[:-1], [events[0],events[-1]], [events[0],events[0],*events[1:]],
                         [events[1],events[0],events[2]], [events[2],events[0],events[1]]):
            with self.subTest(modified=modified):
                self.write_events(modified)
                with self.assertRaises(RuntimeError):p.binding(self.run)
        self.write_events(events)
        tool=self.run/'tool-evidence.jsonl'; saved=tool.read_bytes();tool.write_bytes(saved[:-1])
        with self.assertRaisesRegex(RuntimeError,'Incomplete'):p.binding(self.run)
        tool.write_bytes(saved.replace(b'python3',b'python9'))
        with self.assertRaisesRegex(RuntimeError,'do not match'):p.binding(self.run)

    def test_missing_result_and_error_disagreement_fail(self):
        self.evidence()
        path=self.run/'tool-evidence.jsonl'
        entries=[json.loads(x) for x in path.read_text().splitlines()]
        for change in ({'result':None},{'is_error':True},{'is_error':None}):
            modified=copy.deepcopy(entries);modified[1].update(change)
            path.write_text(''.join(json.dumps(x)+'\n' for x in modified))
            with self.assertRaisesRegex(RuntimeError,'completion/result'):p.binding(self.run)

    def test_stale_or_missing_review_binding_and_wrong_effort_fail(self):
        self.evidence();review=self.review()
        for change in ({'effort':'high'},{'process_review':{}},
                       {'process_review':dict(review['process_review'],binding={})}):
            with self.subTest(change=change),self.assertRaises(RuntimeError):
                p.validate_review(self.run,self.task,self.meta,dict(review,**change))
        (self.run/'tests.json').write_text('{}')
        with self.assertRaises(RuntimeError):p.validate_review(self.run,self.task,self.meta,review)

    def test_binding_diagnostic_names_missing_complete_and_nested_audit(self):
        self.evidence()
        for path in (('audit',), ('audit', 'status_counts')):
            with self.subTest(path=path):
                review = self.review()
                parent = review['process_review']['binding']
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                before = copy.deepcopy(review)
                with self.assertRaisesRegex(RuntimeError, 'missing fields: ' + r'\.'.join(path)):
                    p.validate_review(self.run, self.task, self.meta, review)
                self.assertEqual(review, before)
        p.validate_review(self.run, self.task, self.meta, self.review())

    def test_binding_diagnostic_preserves_rejection_without_copying_payloads(self):
        self.evidence()
        review = self.review()
        review['process_review']['binding']['events_sha256'] = 'PRIVATE_WRONG_HASH'
        review['process_review']['binding']['PRIVATE_EXTRA_KEY'] = 'PRIVATE_VALUE'
        before = copy.deepcopy(review)
        with self.assertRaises(RuntimeError) as raised:
            p.validate_review(self.run, self.task, self.meta, review)
        message = str(raised.exception)
        self.assertIn('changed fields: events_sha256', message)
        self.assertIn('unexpected field count: 1', message)
        self.assertNotIn('PRIVATE', message)
        self.assertEqual(review, before)
        review['process_review']['binding'] = None
        with self.assertRaisesRegex(RuntimeError, 'binding must be a complete object'):
            p.validate_review(self.run, self.task, self.meta, review)

    def test_limits_raise_without_truncating_to_success(self):
        with mock.patch.object(p,'MAX_RECORD',100):
            recorder=p.ToolEvidence(self.run)
            try:
                with self.assertRaisesRegex(RuntimeError,'limit'):
                    recorder.observe(NS(type='tool_execution_start',tool_name='bash',tool_call_id='one',args={'command':'x'*200}))
            finally:recorder.close()
        self.evidence()
        with mock.patch.object(p,'MAX_JOURNAL',1),self.assertRaisesRegex(RuntimeError,'oversized'):p.binding(self.run)

    def test_new_policy_cannot_be_partly_removed_or_rewritten(self):
        self.assertFalse(p.enabled({},{}))
        for task,meta in ((self.task,{}),({},self.meta),
                          ({'process_evidence_policy':dict(p.POLICY,version=True)},self.meta),
                          ({'process_evidence_policy':None},self.meta)):
            with self.subTest(task=task,meta=meta),self.assertRaises(ValueError):p.enabled(task,meta)

    def test_shell_and_ssh_wrappers_are_not_direct_test_process_evidence(self):
        for argv in (['sh','-c','python3 -m unittest | tail -60'], ['ssh','host','python3 -m unittest'],
                     ['env','FLAG=1','bash','-lc','tests'], ['env','-i','pytest']):
            with self.subTest(argv=argv),self.assertRaises(ValueError):p.validate_test_argv(argv)
        for argv in (['python3','-B','-m','unittest','-v'],['env','FLAG=1','pytest','-v'],['./test-script']):
            p.validate_test_argv(argv)

    def test_bound_full_logs_reject_tampering_missing_hash_and_unknown_exit(self):
        self.evidence()
        (self.run/'test-0.stdout').write_text('FIRST\n'+'line\n'*200+'LAST\n')
        (self.run/'test-0.stderr').write_text('stderr\n')
        item={'argv':['/usr/bin/true'],'exit_code':0,'transport_complete':True,'exit_scope':'declared_test_process',
              'stdout':'test-0.stdout','stderr':'test-0.stderr',
              'stdout_sha256':p.file_sha(self.run/'test-0.stdout'),'stderr_sha256':p.file_sha(self.run/'test-0.stderr')}
        report={'evidence_version':1,'tests':[item]}
        p.validate_test_logs(self.run,self.task,report)
        for change in ({'exit_code':None},{'exit_code':False},{'transport_complete':False},
                       {'stdout_sha256':None},{'stdout':'../elsewhere'}):
            with self.subTest(change=change),self.assertRaises(RuntimeError):
                p.validate_test_logs(self.run,self.task,{'evidence_version':1,'tests':[dict(item,**change)]})
        (self.run/'test-0.stdout').write_text('LAST\n')
        with self.assertRaisesRegex(RuntimeError,'log missing or changed'):p.validate_test_logs(self.run,self.task,report)


if __name__=='__main__':unittest.main()
