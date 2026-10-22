import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

import worker_bash as w
import process_evidence as evidence
import rpc_worker
import test_rpc_worker as rpc_fixtures
import test_team_dev as bridge_fixtures
m = bridge_fixtures.m


class ExactPolicyTests(unittest.TestCase):
    def setUp(self):
        self.run = Path('/tmp/exact policy fixture')
        self.task = {'worker_bash_mode': w.MODE,
                     'test_commands': [['/declared/python3', '-B', '-m', 'unittest', '-v', 'test_card']],
                     'process_evidence_policy': copy.deepcopy(evidence.POLICY)}
        self.policy = w.resolve(self.task, self.run)
        self.task[w.KEY] = self.policy
        self.meta = {w.KEY: copy.deepcopy(self.policy), 'process_evidence_policy': copy.deepcopy(evidence.POLICY)}

    def test_only_exact_prepared_forms_and_boundary_whitespace_are_allowed(self):
        _, direct, logged = w.commands(self.task, self.run)
        for value in direct + logged:
            for timeout in ({}, {'timeout': 20}, {'timeout': 0.5}):
                with self.subTest(value=value, timeout=timeout):
                    self.assertEqual(w.inspect(dict(command=' \n'+value+'\n ', **timeout), self.policy)['status'], 'allowed')

    def test_readonly_probes_and_modified_commands_are_outside_contract(self):
        _, direct, logged = w.commands(self.task, self.run)
        requests = [
            'python3 -c "import json; print(json.load(open(\'context/frame.json\')).keys())"',
            'python3 -B -c "import json; print(json.load(open(\'context/frame.json\')).keys())"',
            'wc -l context/CONTRACT.md context/observer.py',
            'python3 -c "print(\'x\')" 2>/dev/null; awk \'/"frames"/{print NR": "$0}\' context/frame.json | head',
            'PYTHONDONTWRITEBYTECODE=1 /declared/python3 -B -c "import ast; ast.parse(open(\'code.py\').read()); print(\'syntax ok\')"',
            '/declared/python3 -B -c "import PIL; print(PIL.__version__)"',
            'sha256sum context/annotation_manifest.json context/annotation_definitions.json',
            'cat saved.stderr | tail -1',
            'sd \'source_sha256="producer"\' \'source_sha256=producer_digest\' test_inputs.py',
            "sed -i '' 's/old/new/' code.py",
            "perl -pi -e 's/old/new/' code.py",
            'python3 -c "from pathlib import Path; Path(\'code.py\').write_text(\'new\')"',
            'printf new >code.py', 'patch code.py <repair.patch',
            direct[0] + '; echo done', 'cd /tmp && '+direct[0],
            direct[0].replace('/declared/python3','python3'),
            logged[0].replace('exit "$team_dev_test_exit"','printf "%s" "$team_dev_test_exit"'),
        ]
        for command in requests:
            with self.subTest(command=command):
                self.assertEqual(w.inspect({'command':command}, self.policy)['status'], 'undeclared_command')

    def test_extra_tool_controls_and_invalid_timeouts_reject_even_exact_command(self):
        command = w.commands(self.task,self.run)[1][0]
        for extra in ({'cwd':'/'},{'async':True},{'env':{}},{'timeout':None},{'timeout':True},
                      {'timeout':0},{'timeout':3601},{'timeout':float('nan')}, {'timeout':float('inf')}):
            self.assertEqual(w.inspect(dict(command=command,**extra),self.policy)['status'],'invalid_arguments')
        for args in (None, {}, {'command':''}, {'command':23}, {'command':'x'*(w.MAX_CHARS+1)}):
            self.assertEqual(w.inspect(args,self.policy)['status'],'unavailable')

    def test_literals_remain_opaque_and_compact_evidence_does_not_leak(self):
        self.task['test_commands'] = [['/python', '-c', 'print("PRIVATE; $(example) | head")']]
        policy = w.resolve(self.task,self.run)
        command = w.commands(self.task,self.run)[1][0]
        result = w.inspect({'command':command},policy)
        self.assertEqual(result['status'],'allowed')
        self.assertNotIn('PRIVATE',json.dumps([result,policy]))
        self.assertNotEqual(result['command_sha256'],w.inspect({'command':command+'; true'},policy)['command_sha256'])

    def test_frozen_copies_path_and_argv_changes_fail_closed(self):
        self.assertEqual(w.validate(self.task,self.meta,self.run),self.policy)
        for owner in ('task','meta'):
            for value in ('missing',None,{},dict(self.policy,version=True)):
                task,meta=copy.deepcopy(self.task),copy.deepcopy(self.meta)
                target=task if owner=='task' else meta
                if value=='missing': target.pop(w.KEY)
                else: target[w.KEY]=value
                with self.subTest(owner=owner,value=value),self.assertRaises(ValueError):
                    w.validate(task,meta,self.run)
        task=copy.deepcopy(self.task); task['test_commands'][0][0]='/alternate/python'
        with self.assertRaises(ValueError): w.validate(task,self.meta,self.run)
        with self.assertRaises(ValueError): w.validate(self.task,self.meta,Path('/changed/root'))
        with self.assertRaises(ValueError): w.validate(self.task,dict(self.meta,process_evidence_policy=None),self.run)

    def test_legacy_standard_invalid_mode_and_oversize(self):
        self.assertIsNone(w.validate({}, {}, self.run))
        with self.assertRaises(ValueError): w.validate({'worker_bash_mode':w.MODE},{},self.run)
        task={'worker_bash_mode':'standard'}; task[w.KEY]=w.resolve(task,self.run)
        self.assertIsNone(w.validate(task,{w.KEY:task[w.KEY]},self.run))
        for value in (None,'tests_only',[],False):
            with self.subTest(value=value),self.assertRaises(ValueError): w.resolve({'worker_bash_mode':value},self.run)
        self.task['test_commands']=[['/python','-c','x'*w.MAX_CHARS]]
        with self.assertRaises(ValueError): w.resolve(self.task,self.run)


class ExactRpcTests(unittest.TestCase):
    setUp = rpc_fixtures.RpcTests.setUp
    execute = rpc_fixtures.RpcTests.execute

    def strict(self):
        self.task.update(worker_bash_mode=w.MODE,test_commands=[['/declared/python3','-B','-m','unittest']],
                         process_evidence_policy=copy.deepcopy(evidence.POLICY))
        self.task[w.KEY]=w.resolve(self.task,self.run)
        return {w.KEY:copy.deepcopy(self.task[w.KEY]),'process_evidence_policy':copy.deepcopy(evidence.POLICY)}

    def events(self,command):
        return [NS(type='tool_execution_start',tool_name='bash',tool_call_id='a',args={'command':command}),
                NS(type='tool_execution_end',tool_name='bash',tool_call_id='a',is_error=False,result={'content':[]}),
                NS(type='agent_end',is_terminal=True)]

    def test_exact_success_binds_complete_summary_and_detects_tampering(self):
        meta=self.strict()
        self.client.initial=self.events(w.commands(self.task,self.run)[1][0])
        self.assertEqual(self.execute(meta),('exited',0))
        actual=evidence.binding(self.run,async_required=True,live_output_required=True,exact_bash_policy=self.task[w.KEY])
        self.assertEqual(actual['exact_bash']['status_counts']['allowed'],1)
        self.assertFalse(actual['blocked_requests'])
        review={'model':'gpt-5.6-sol','effort':'max','process_review':{'verdict':'pass','binding':actual,'reason':'Inspected','async_jobs':[]}}
        evidence.validate_review(self.run,self.task,meta,review)
        review['process_review']['binding']=dict(actual); review['process_review']['binding'].pop('exact_bash')
        with self.assertRaisesRegex(RuntimeError,'exact_bash'): evidence.validate_review(self.run,self.task,meta,review)
        path=self.run/'events.jsonl'; events=[json.loads(s) for s in path.read_text().splitlines()]
        events[0]['exactBashAudit']['status']='undeclared_command'
        path.write_text(''.join(json.dumps(s)+'\n' for s in events))
        with self.assertRaisesRegex(RuntimeError,'Exact Bash parameters'):
            evidence.binding(self.run,async_required=True,live_output_required=True,exact_bash_policy=self.task[w.KEY])

    def test_successful_outer_tool_still_stops_and_cannot_pass_review(self):
        meta=self.strict(); self.client.initial=self.events('python3 -c "print(1)"')
        self.assertEqual(self.execute(meta),('failed',1))
        self.assertTrue(self.client.aborted)
        failure=json.loads((self.run/'failure.json').read_text())
        self.assertEqual(failure['reason'],w.OBSERVATION)
        self.assertEqual(json.loads((self.run/'progress.json').read_text())['exact_bash']['rejected_requests'],1)
        with self.assertRaisesRegex(RuntimeError,'Forbidden request'):
            evidence.validate_review(self.run,self.task,meta,{})
        from run_diagnostics import diagnose_run
        result=diagnose_run(self.run)
        self.assertEqual(result['assessment'],'observed_request_outside_declared_tests')
        self.assertEqual(result['rpc_client_fault_count'],0)

    def test_cancel_keeps_priority_and_private_evidence_still_rejects(self):
        meta=self.strict(); self.client.initial=self.events('wc -l context.json')
        (self.run/'cancel-requested').write_text('cancel')
        self.assertEqual(self.execute(meta),('cancelled',1))
        self.assertEqual(json.loads((self.run/'failure.json').read_text())['reason'],'cancel_requested')
        # Callback journal may reach disk before any compact event is drained.
        (self.run/'events.jsonl').write_text('')
        blocked=evidence.observed_blocked_requests(self.run,exact_bash_policy=self.task[w.KEY])
        self.assertIn(w.OBSERVATION,blocked)

    def test_legacy_and_standard_do_not_gain_strict_rejection(self):
        self.client.initial=self.events('wc -l context.json')
        self.assertEqual(self.execute(),('exited',0))
        self.assertNotIn('exact_bash',json.loads((self.run/'progress.json').read_text()))

    def test_invalid_policy_rejected_before_client_construction(self):
        meta=self.strict(); meta[w.KEY]=None
        with mock.patch.object(rpc_worker,'RpcClient') as client:
            with self.assertRaises(ValueError):
                rpc_worker.execute(self.run,meta,self.task,{},[],m.dump,lambda pid:'stamp',lambda:[],client_factory=client)
            client.assert_not_called()


class ExactBridgeTests(unittest.TestCase):
    setUp = bridge_fixtures.BridgeTests.setUp
    prepare = bridge_fixtures.BridgeTests.prepare

    def test_preparation_freezes_exact_commands_and_prompt(self):
        self.task['worker_bash_mode']=w.MODE
        self.prepare(worker_evidence=False)
        task=m.load(self.run/'task.json'); meta=m.load(self.run/'metadata.json')
        self.assertEqual(w.validate(task,meta,self.run),task[w.KEY])
        prompt=(self.run/'prompt.md').read_text()
        payload=json.loads(prompt.split('\n\n',1)[1])
        self.assertEqual(payload['worker_bash_mode'],w.MODE)
        self.assertIn('SOURCE EDIT ROUTING:',prompt)
        self.assertIn('only through the native edit or write tools within allowed_paths',prompt)
        self.assertIn('A named keyword and a positional string literal are different edit targets',prompt)
        self.assertIn('Do not embed a repair in a test command',prompt)
        self.assertIn('If edit/write fails or is unavailable, report the blocker',prompt)
        self.assertIn('STRICT CHECK ROUTING:',prompt)
        self.assertTrue(prompt.startswith('STRICT CHECK ROUTING:'))
        self.assertEqual(prompt.count('STRICT CHECK ROUTING:'),1)
        entry=prompt.split('STRICT BASH MODE:',1)[0]
        self.assertIn('TASK CONSTRAINTS: '+json.dumps(task.get('constraints', []),ensure_ascii=False),entry)
        self.assertIn('Keep missing facts unknown',entry)
        self.assertIn('not fresh verification of file bytes',entry)
        self.assertEqual(w.inspect({'command':'/declared/python3 -B -c "import PIL; print(PIL.__version__)"',
                                    'timeout':30},task[w.KEY])['status'],'undeclared_command')
        for recipe in ('Run short probes directly', 'For an authorized exploratory Python check',
                       'For Python syntax-only checks, use', 'Use inline tests or declared test files'):
            self.assertNotIn(recipe,prompt)
        for command in payload['worker_direct_test_commands']+payload['worker_logged_test_commands']:
            self.assertEqual(w.inspect({'command':command},task[w.KEY])['status'],'allowed')
        self.task['constraints']=['Use supplied dependency facts; no standalone version checks.']
        self.prepare(worker_evidence=False)
        constrained_prompt=(self.run/'prompt.md').read_text()
        constrained_entry=constrained_prompt.split('STRICT BASH MODE:',1)[0]
        self.assertIn('TASK CONSTRAINTS: '+json.dumps(self.task['constraints'],ensure_ascii=False),constrained_entry)
        constrained_payload=json.loads(constrained_prompt.split('\n\n',1)[1])
        self.assertEqual(constrained_payload['constraints'],self.task['constraints'])
        self.task['worker_bash_mode']='standard'
        self.prepare(worker_evidence=False)
        standard_prompt=(self.run/'prompt.md').read_text()
        self.assertNotIn('SOURCE EDIT ROUTING:',standard_prompt)
        self.assertNotIn('STRICT CHECK ROUTING:',standard_prompt)
        self.assertNotIn('TASK CONSTRAINTS:',standard_prompt.split('\n\n',1)[0])
        for recipe in ('Run short probes directly', 'For an authorized exploratory Python check',
                       'For Python syntax-only checks, use', 'Use inline tests or declared test files'):
            self.assertIn(recipe,standard_prompt)

    def test_start_and_worker_fail_before_credentials_on_policy_corruption(self):
        self.task['worker_bash_mode']=w.MODE; self.prepare(worker_evidence=False)
        meta=m.load(self.run/'metadata.json'); meta[w.KEY]=None; m.dump(self.run/'metadata.json',meta)
        with mock.patch.object(m,'worker_env') as credentials, mock.patch.object(m.subprocess,'Popen') as launch:
            with self.assertRaises(ValueError): m.start(self.run)
            with self.assertRaises(ValueError): m.worker(self.run)
            credentials.assert_not_called(); launch.assert_not_called()

    def test_cancelled_forbidden_request_cannot_resume_or_archive(self):
        self.task['worker_bash_mode']=w.MODE; self.prepare(worker_evidence=False)
        meta=m.load(self.run/'metadata.json'); meta['process']='cancelled'; m.dump(self.run/'metadata.json',meta)
        m.dump(self.run/'failure.json',{'reason':'cancel_requested'})
        (self.run/'tool-evidence.jsonl').write_text(json.dumps({'version':1,'type':'tool_execution_start',
            'id':'a','args':{'command':'wc -l context.json'}})+'\n')
        with self.assertRaisesRegex(RuntimeError,'prohibited-request'): m.resume(self.run,self.root/'missing-message')
        self.assertFalse((self.run/'attempt-1').exists())
        self.assertEqual(m.load(self.run/'metadata.json'),meta)

    def test_propose_rejects_corrupt_policy_even_with_synthetic_worker_success(self):
        self.task['worker_bash_mode']=w.MODE; self.prepare()
        task=m.load(self.run/'task.json'); task[w.KEY]=None; m.dump(self.run/'task.json',task)
        with self.assertRaises(ValueError): m.propose(self.run)

    def test_summary_and_cli_include_exact_evidence(self):
        self.task['worker_bash_mode']=w.MODE; self.prepare()
        summary=m.summarize_events(self.run)
        self.assertEqual(summary['exact_bash']['bash_calls'],0)
        import contextlib,io,sys
        output=io.StringIO()
        with mock.patch.object(sys,'argv',['team_dev.py','process-evidence','--run',str(self.run)]),contextlib.redirect_stdout(output):
            m.main()
        actual=json.loads(output.getvalue())
        self.assertEqual(actual['exact_bash']['rejected_requests'],0)
        self.assertIn('policy_sha256',actual['exact_bash'])


if __name__=='__main__':
    unittest.main()
