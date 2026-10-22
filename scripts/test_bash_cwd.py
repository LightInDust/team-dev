import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import worker_bash as w
import process_evidence as evidence
import test_worker_bash as fixtures
from run_diagnostics import diagnose_run, bash_rejection_assessment

m = fixtures.m


class CwdPolicyTests(unittest.TestCase):
    setUp = fixtures.ExactPolicyTests.setUp

    def test_v2_freezes_cwd_and_allows_only_omission_or_exact_snapshot(self):
        self.assertEqual(self.policy['version'],2)
        snapshot=str(self.run/'snapshot')
        self.assertEqual(self.policy['snapshot_cwd'],snapshot)
        command=w.commands(self.task,self.run)[1][0]
        for extra,status in (({},'omitted'),({'cwd':snapshot},'snapshot')):
            result=w.inspect(dict(command=command,timeout=180,**extra),self.policy)
            self.assertEqual(result['status'],'allowed')
            self.assertIs(result['command_match'],True)
            self.assertEqual(result['cwd_status'],status)
            self.assertEqual(result['argument_issues'],[])

    def test_relative_alias_other_run_and_malformed_cwd_are_rejected(self):
        command=w.commands(self.task,self.run)[1][0]
        for cwd in ('.','./snapshot','../snapshot',str(self.run/'snapshot')+'/',str(self.run/'snapshot')+'/.',
                    str(self.run/'snapshot')+'/..','/different/run/snapshot','$SNAPSHOT','~/snapshot',
                    'file://'+str(self.run/'snapshot'),'/symlink-alias',None,False,[],{},'', 'bad\x00path'):
            with self.subTest(cwd=cwd):
                result=w.inspect({'command':command,'cwd':cwd},self.policy)
                self.assertEqual(result['status'],'invalid_arguments')
                self.assertIs(result['command_match'],True)
                self.assertIn(result['cwd_status'],('different','invalid'))
                self.assertTrue(result['argument_issues'])

    def test_argument_and_command_observations_remain_separate_and_payload_free(self):
        result=w.inspect({'command':'PRIVATE_BAD_COMMAND','cwd':'/PRIVATE_CWD','pty':False,'timeout':0},self.policy)
        self.assertIs(result['command_match'],False)
        self.assertEqual(result['status'],'invalid_arguments')
        self.assertEqual(result['argument_issues'],['unsupported_arguments','invalid_timeout','cwd_not_snapshot'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        command=w.commands(self.task,self.run)[1][0]
        for key in ('async','pty','env','timeout_ms'):
            result=w.inspect({'command':command,'cwd':self.policy['snapshot_cwd'],key:False},self.policy)
            self.assertEqual(result['argument_issues'],['unsupported_arguments'])
        result=w.inspect({'command':'undeclared','cwd':self.policy['snapshot_cwd']},self.policy)
        self.assertEqual(result['status'],'undeclared_command')

    def test_frozen_v1_schema_and_cwd_rejection_are_unchanged(self):
        task=copy.deepcopy(self.task); meta=copy.deepcopy(self.meta)
        task[w.KEY]=w.resolve(task,self.run,version=1); meta[w.KEY]=copy.deepcopy(task[w.KEY])
        self.assertNotIn('snapshot_cwd',task[w.KEY])
        self.assertEqual(w.validate(task,meta,self.run),task[w.KEY])
        command=w.commands(task,self.run)[1][0]
        expected={'version':1,'status':'allowed','command_sha256':w.command_sha(command)}
        self.assertEqual(w.inspect({'command':command},task[w.KEY]),expected)
        expected['status']='invalid_arguments'
        self.assertEqual(w.inspect({'command':command,'cwd':str(self.run/'snapshot')},task[w.KEY]),expected)

    def test_cwd_policy_cannot_be_changed_or_partially_removed(self):
        for value in (None,'/different/snapshot','missing'):
            for owner in ('task','meta','both'):
                task,meta=copy.deepcopy(self.task),copy.deepcopy(self.meta)
                for obj in ([task,meta] if owner=='both' else [task] if owner=='task' else [meta]):
                    if value=='missing':obj[w.KEY].pop('snapshot_cwd')
                    else:obj[w.KEY]['snapshot_cwd']=value
                with self.subTest(value=value,owner=owner),self.assertRaises(ValueError):
                    w.validate(task,meta,self.run)
        for version in (0,3,None,True,'2'):
            task,meta=copy.deepcopy(self.task),copy.deepcopy(self.meta)
            task[w.KEY]['version']=meta[w.KEY]['version']=version
            with self.assertRaises(ValueError):w.validate(task,meta,self.run)

    def test_missing_or_incomplete_match_counts_do_not_claim_argument_only(self):
        command=w.commands(self.task,self.run)[1][0]
        audit=w.ExactBashAudit()
        for policy in (w.resolve(self.task,self.run,version=1),self.policy):
            audit.event({'type':'tool_execution_start','toolName':'bash',
                         'exactBashAudit':w.inspect({'command':command,'cwd':'.'},policy)})
        summary=audit.summary()
        self.assertEqual(summary['command_match_counts'],{'matched':1,'not_matched':0,'unavailable':1})
        expected='bash_tool_arguments_rejected_command_match_unverified'
        self.assertEqual(bash_rejection_assessment(summary),expected)
        for counts in (None,{}, {'matched':True,'not_matched':0,'unavailable':0},
                       {'matched':1,'not_matched':0,'unavailable':0}):
            self.assertEqual(bash_rejection_assessment(dict(summary,command_match_counts=counts)),expected)


class CwdRpcTests(unittest.TestCase):
    setUp = fixtures.ExactRpcTests.setUp
    execute = fixtures.ExactRpcTests.execute
    strict = fixtures.ExactRpcTests.strict
    events = fixtures.ExactRpcTests.events

    def test_exact_logged_command_with_snapshot_cwd_completes_and_binds(self):
        meta=self.strict()
        self.client.initial=self.events(w.commands(self.task,self.run)[2][0])
        self.client.initial[0].args.update(cwd=self.task[w.KEY]['snapshot_cwd'],timeout=180)
        self.assertEqual(self.execute(meta),('exited',0))
        binding=evidence.binding(self.run,async_required=True,live_output_required=True,exact_bash_policy=self.task[w.KEY])
        self.assertFalse(binding['blocked_requests'])
        self.assertEqual(binding['exact_bash']['status_counts']['allowed'],1)
        records=[json.loads(s) for s in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertEqual(records[0]['exactBashAudit']['cwd_status'],'snapshot')
        self.assertIs(records[0]['exactBashAudit']['command_match'],True)
        # An intact command hash cannot cover a changed private cwd.
        path=self.run/'tool-evidence.jsonl'; raw=[json.loads(s) for s in path.read_text().splitlines()]
        raw[0]['args']['cwd']='/different/snapshot'
        path.write_text(''.join(json.dumps(s)+'\n' for s in raw))
        with self.assertRaisesRegex(RuntimeError,'Exact Bash parameters'):
            evidence.binding(self.run,async_required=True,live_output_required=True,exact_bash_policy=self.task[w.KEY])

    def test_wrong_cwd_stops_even_with_outer_success_and_diagnoses_arguments(self):
        meta=self.strict();self.client.initial=self.events(w.commands(self.task,self.run)[1][0])
        self.client.initial[0].args['cwd']='.'
        self.assertEqual(self.execute(meta),('failed',1))
        self.assertEqual(diagnose_run(self.run)['assessment'],'bash_tool_arguments_rejected')
        with self.assertRaisesRegex(RuntimeError,'Forbidden request'):
            evidence.validate_review(self.run,self.task,meta,{})

    def test_frozen_v1_rpc_and_process_binding_keep_original_schema(self):
        meta=self.strict();self.task[w.KEY]=w.resolve(self.task,self.run,version=1);meta[w.KEY]=copy.deepcopy(self.task[w.KEY])
        self.client.initial=self.events(w.commands(self.task,self.run)[1][0])
        self.assertEqual(self.execute(meta),('exited',0))
        events=[json.loads(s) for s in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertEqual(set(events[0]['exactBashAudit']),{'version','status','command_sha256'})
        self.assertEqual(events[0]['exactBashAudit']['version'],1)
        self.assertFalse(evidence.binding(self.run,async_required=True,live_output_required=True,
            exact_bash_policy=self.task[w.KEY])['blocked_requests'])
        self.assertNotIn('command_match_counts',json.loads((self.run/'progress.json').read_text())['exact_bash'])

    def test_mismatched_command_and_bad_cwd_preserve_both_causes(self):
        meta=self.strict();self.client.initial=self.events('echo undeclared')
        self.client.initial[0].args.update(cwd='/different/snapshot',timeout=0)
        self.assertEqual(self.execute(meta),('failed',1))
        progress=json.loads((self.run/'progress.json').read_text())
        self.assertEqual(progress['exact_bash']['status_counts']['invalid_arguments'],1)
        self.assertEqual(progress['exact_bash']['command_match_counts'],{'matched':0,'not_matched':1,'unavailable':0})
        self.assertEqual(diagnose_run(self.run)['assessment'],'bash_command_and_arguments_rejected')

    def test_v1_argument_rejection_leaves_command_match_unknown_in_diagnosis(self):
        meta=self.strict();self.task[w.KEY]=w.resolve(self.task,self.run,version=1);meta[w.KEY]=copy.deepcopy(self.task[w.KEY])
        self.client.initial=self.events(w.commands(self.task,self.run)[1][0])
        self.client.initial[0].args['cwd']=str(self.run/'snapshot')
        self.assertEqual(self.execute(meta),('failed',1))
        self.assertEqual(diagnose_run(self.run)['assessment'],'bash_tool_arguments_rejected_command_match_unverified')
        self.assertNotIn('command_match_counts',json.loads((self.run/'progress.json').read_text())['exact_bash'])

    def test_private_cwd_rejection_survives_cancel_before_compact_log(self):
        meta=self.strict();self.client.initial=self.events(w.commands(self.task,self.run)[1][0])
        self.client.initial[0].args['cwd']='/other/snapshot'
        (self.run/'cancel-requested').write_text('cancel')
        self.assertEqual(self.execute(meta),('cancelled',1))
        (self.run/'events.jsonl').write_text('')
        self.assertIn(w.OBSERVATION,evidence.observed_blocked_requests(self.run,exact_bash_policy=self.task[w.KEY]))


class CwdBridgeTests(unittest.TestCase):
    setUp = fixtures.ExactBridgeTests.setUp
    prepare = fixtures.ExactBridgeTests.prepare

    def test_prepare_freezes_and_supplies_exact_cwd_then_preserves_real_test_status(self):
        self.task.update(worker_bash_mode=w.MODE,
            test_commands=[[sys.executable,'-B','-c','import os,sys; print(os.getcwd()); sys.exit(7)']])
        snapshot=self.prepare(worker_evidence=False)
        prompt=(self.run/'prompt.md').read_text()
        payload=json.loads(prompt.split('\n\n',1)[1])
        task=m.load(self.run/'task.json');meta=m.load(self.run/'metadata.json')
        self.assertEqual(payload['worker_bash_snapshot_cwd'],str(snapshot))
        self.assertEqual(task[w.KEY]['version'],2)
        args={'command':payload['worker_logged_test_commands'][0],'cwd':payload['worker_bash_snapshot_cwd'],'timeout':180}
        self.assertEqual(w.inspect(args,w.validate(task,meta,self.run))['status'],'allowed')
        result=subprocess.run(['/bin/sh','-c',args['command']],cwd=args['cwd'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,7)
        log=Path(result.stdout.strip().split('=',1)[1])
        self.assertEqual((log/'exit-code').read_text(),'7\n')
        self.assertEqual((log/'stdout.log').read_text().strip(),str(snapshot))
        self.assertNotIn('cat saved.log | tail -20', prompt)
        self.assertIn('Do not invoke Bash cat, head, tail, grep', prompt)
        self.assertIn('Do not poll a running command', prompt)
        templates=payload['worker_test_log_read_templates']
        self.assertEqual([t['tool'] for t in templates], ['read']*3)
        resolved=[t['arguments']['path'].replace('<WORKER_TEST_LOG_DIR>',str(log)) for t in templates]
        self.assertEqual(resolved,[str(log/'exit-code'),str(log/'stdout.log')+':1-200',str(log/'stderr.log')+':1-200'])
        self.assertEqual(Path(resolved[0]).read_text(),'7\n')
        self.assertEqual(Path(resolved[1].removesuffix(':1-200')).read_text().strip(),str(snapshot))
        self.assertEqual(Path(resolved[2].removesuffix(':1-200')).read_text(),'')
        # Reading completed evidence through Bash remains a rejected request.
        self.assertEqual(w.inspect({'command':'cat '+resolved[0]},task[w.KEY])['status'],'undeclared_command')

    def test_tampered_cwd_policy_stops_start_before_credentials(self):
        self.task['worker_bash_mode']=w.MODE;self.prepare(worker_evidence=False)
        task=m.load(self.run/'task.json');meta=m.load(self.run/'metadata.json')
        task[w.KEY]['snapshot_cwd']=meta[w.KEY]['snapshot_cwd']='/other/snapshot'
        m.dump(self.run/'task.json',task);m.dump(self.run/'metadata.json',meta)
        with mock.patch.object(m,'worker_env') as credentials:
            with self.assertRaises(ValueError):m.start(self.run)
            credentials.assert_not_called()
        self.assertEqual(m.load(self.run/'metadata.json')['process'],'not_started')


if __name__=='__main__':
    unittest.main()
