import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

import async_evidence as a
import process_evidence as p
from rpc_worker import compact_event


def notification(ids=('bg_3',), text='Command exited with code 130'):
    return {'role':'custom','customType':'async-result','content':text,
            'details':{'jobs':[{'jobId':i,'type':'bash'} for i in ids]},'timestamp':123}


class AsyncEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.run=Path(self.temp.name)

    def evidence(self, *, deliver=True, message=None, ids=('bg_3',), early_terminal=False):
        recorder=p.ToolEvidence(self.run,capture_async=True)
        events=[]
        def emit(event):
            recorder.observe(event);events.append(compact_event(event))
        try:
            for index, jid in enumerate(ids):
                emit(NS(type='tool_execution_start',tool_name='bash',tool_call_id='call_'+str(index),args={'command':'echo fixture'}))
                emit(NS(type='tool_execution_end',tool_name='bash',tool_call_id='call_'+str(index),is_error=False,
                    result={'content':[{'type':'text','text':'Backgrounded; output pending'}],
                            'details':{'async':{'jobId':jid,'state':'running','type':'bash'}}}))
            if early_terminal:emit(NS(type='agent_end',is_terminal=True))
            if deliver:emit(NS(type='message_end',message=message or notification(ids)))
            emit(NS(type='agent_end',is_terminal=True))
        finally:recorder.close()
        self.events(events)
        return events

    def events(self, items):
        (self.run/'events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in items))

    def binding(self):return p.binding(self.run,async_required=True)

    def review(self):
        bound=self.binding()
        entries=[]
        for job in bound['async_execution']['jobs']:
            receipt={'version':1,'observer':'coordinator','run':str(self.run.resolve()),'execution':job,
                     'observed_at':'synthetic fixture, not real task evidence',
                     'children':{'status':'not_created','evidence':'Synthetic fixture has no subprocesses.'},
                     'scratch':{'status':'not_used','paths':[],'evidence':'Synthetic fixture has no scratch.'}}
            path=self.run/(job['job_id']+'.json');path.write_text(json.dumps(receipt))
            entries.append({'job_id':job['job_id'],'outcome':'failed','exit_code':130,
                            'reason':'Read the exact fixture notification; exit 130 is failure, no signal actor inferred.',
                            'lifecycle_receipt':{'path':path.name,'sha256':p.file_sha(path)}})
        return {'model':'gpt-5.6-sol','effort':'max','verdict':'pass','process_review':{
            'verdict':'pass','binding':bound,'reason':'Reviewed synthetic process evidence.', 'async_jobs':entries}}

    def validate(self, review):p.validate_review(self.run,{'process_evidence_policy':dict(p.POLICY_V2)},
        {'process_evidence_policy':dict(p.POLICY_V2)},review)

    def test_running_tool_end_is_not_terminal_evidence(self):
        self.evidence(deliver=False)
        with self.assertRaisesRegex(RuntimeError,'terminal delivery is unresolved'):self.binding()

    def test_failed_delivery_is_bound_without_inventing_success_or_cleanup(self):
        self.evidence(early_terminal=True)
        summary=self.binding()['async_execution'];job=summary['jobs'][0]
        self.assertEqual(job['state'],'running')
        self.assertEqual(job['delivery'],'delivered')
        self.assertNotIn('exit_code',job)
        review=self.review();self.validate(review)
        self.assertEqual(review['process_review']['async_jobs'][0]['exit_code'],130)

    def test_plain_assistant_text_and_missing_metadata_do_not_close_jobs(self):
        for message in (dict(notification(),role='assistant'),dict(notification(),details={}),
                        {'role':'custom','content':notification()['content']}):
            with self.subTest(message=message):
                self.evidence(message=message)
                with self.assertRaises(RuntimeError):self.binding()

    def test_wrong_duplicate_and_batched_job_ids(self):
        for ids,message in ((('bg_3',),notification(('bg_9',))),
                            (('bg_3','bg_3'),notification()),
                            (('bg_3',),notification(('bg_3','bg_3')))):
            with self.subTest(ids=ids,message=message):
                self.evidence(ids=ids,message=message)
                with self.assertRaises(RuntimeError):self.binding()
        self.evidence(ids=('bg_3','bg_4'))
        self.assertEqual(len(self.binding()['async_execution']['jobs']),2)
        self.validate(self.review())

    def test_late_result_requires_a_new_terminal_event(self):
        events=self.evidence(early_terminal=True)
        self.events(events[:-1])
        with self.assertRaisesRegex(RuntimeError,'terminal evidence'):self.binding()

    def test_changed_or_missing_private_notification_blocks_binding(self):
        self.evidence()
        path=self.run/'tool-evidence.jsonl';raw=path.read_bytes()
        path.write_bytes(raw.replace(b'code 130',b'code 0'))
        with self.assertRaisesRegex(RuntimeError,'notifications do not match'):self.binding()
        path.write_bytes(b'\n'.join(raw.splitlines()[:-1])+b'\n')
        with self.assertRaisesRegex(RuntimeError,'notifications do not match'):self.binding()

    def test_event_async_metadata_must_match_actual_tool_result(self):
        events=self.evidence();events[1]['asyncBash']=None;self.events(events)
        with self.assertRaisesRegex(RuntimeError,'Bash result binding'):self.binding()

    def test_v1_policy_and_receipt_are_not_retrofitted(self):
        self.evidence(deliver=False)
        task={'process_evidence_policy':dict(p.POLICY_V1)}
        self.assertFalse(p.async_enabled(task,task))
        review={'model':'gpt-5.6-sol','effort':'max','verdict':'pass','process_review':{
            'verdict':'pass','binding':p.binding(self.run),'reason':'Legacy fixture retains v1 schema.'}}
        p.validate_review(self.run,task,task,review)
        with self.assertRaises(ValueError):p.enabled(task,{'process_evidence_policy':dict(p.POLICY)})

    def test_cleanup_claim_without_current_coordinator_receipt_cannot_pass(self):
        self.evidence();review=self.review();entry=review['process_review']['async_jobs'][0]
        original=copy.deepcopy(entry)
        for changed in ({'outcome':'succeeded','exit_code':130},{'outcome':'unknown'},
                        {'lifecycle_receipt':None}, {'lifecycle_receipt':{'path':'../somewhere','sha256':'no'}}):
            entry.clear();entry.update(original);entry.update(changed)
            with self.subTest(changed=changed),self.assertRaises(RuntimeError):self.validate(review)
        entry.clear();entry.update(original)
        path=self.run/entry['lifecycle_receipt']['path'];path.write_text(path.read_text()+' ')
        with self.assertRaisesRegex(RuntimeError,'receipt changed'):self.validate(review)

    def test_unverified_children_or_uncaptured_scratch_path_cannot_pass(self):
        self.evidence();review=self.review();entry=review['process_review']['async_jobs'][0]
        path=self.run/entry['lifecycle_receipt']['path'];original=json.loads(path.read_text())
        for key,change in (('children',{'status':'unverified','evidence':'PID not checked'}),
                           ('scratch',{'status':'removed','paths':[],'evidence':'Assumed context unwound'}),
                           ('scratch',{'status':'unverified','paths':[],'evidence':'Path never captured'})):
            receipt=copy.deepcopy(original);receipt[key]=change;path.write_text(json.dumps(receipt))
            entry['lifecycle_receipt']['sha256']=p.file_sha(path)
            with self.subTest(key=key,change=change),self.assertRaises(RuntimeError):self.validate(review)

    def test_real_scoped_child_and_temp_observations_back_receipt(self):
        # Finite owned subprocess; no FIFO, API, business code or manual deletion.
        script='import tempfile\nwith tempfile.TemporaryDirectory() as p:\n print(p, flush=True)\n'
        child=subprocess.Popen([sys.executable,'-B','-c',script],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                               env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        out,err=child.communicate(timeout=5)
        self.assertEqual(child.returncode,0,err)
        path=Path(out.decode().strip());self.assertFalse(path.exists())
        self.evidence(message=notification(text='Fixture command completed normally; exit 0 measured independently.'))
        review=self.review();entry=review['process_review']['async_jobs'][0]
        receipt_path=self.run/entry['lifecycle_receipt']['path'];receipt=json.loads(receipt_path.read_text())
        receipt['children']={'status':'stopped','evidence':f'Owned PID {child.pid}; communicate reaped it with exit 0.'}
        receipt['scratch']={'status':'removed','paths':[str(path)],'evidence':'Captured printed path; exists() false after communicate.'}
        receipt_path.write_text(json.dumps(receipt));entry.update(outcome='succeeded',exit_code=0)
        entry['lifecycle_receipt']['sha256']=p.file_sha(receipt_path)
        self.validate(review)

    def test_sdk_preserves_custom_details_for_recording(self):
        from omp_rpc.protocol import parse_notification
        event=parse_notification({'type':'message_end','message':notification()})
        compact=compact_event(event)
        self.assertEqual(compact['asyncResult']['jobs'],[{'job_id':'bg_3','type':'bash'}])
        recorder=p.ToolEvidence(self.run,capture_async=True)
        try:recorder.observe(event)
        finally:recorder.close()
        self.assertEqual(json.loads((self.run/'tool-evidence.jsonl').read_text())['message'],notification())


if __name__=='__main__':unittest.main()
