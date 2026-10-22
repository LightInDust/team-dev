"""Actual local OMP loop + supervisor/evidence regressions. No paid provider."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

import bash_gate as g
import worker_bash as w
from worker_test_catalog import catalog, correction_hint, validate_catalog
import process_evidence as e
import rpc_worker as r
from test_gate_runtime_fixture import run_fixture
import test_rpc_worker as rpc_fixtures
from test_team_dev import m


def scenario(root, work):
    (work/'fixture_test.py').write_text('from pathlib import Path\nimport sys\nPath("allowed.txt").write_text("executed")\nprint("full stdout")\nprint("full stderr",file=sys.stderr)\nraise SystemExit(7)\n')
    task = {'worker_bash_mode':w.MODE, 'test_commands':[[sys.executable,'-B','fixture_test.py']]}
    return task, []


def journals(root, task, result):
    gate=g.for_run(root); gate.refresh(complete=True)
    compact, raw=[],[]
    for event in result['events']:
        kind=event.get('type')
        if kind in ('agent_start','turn_start','agent_end'):
            compact.append({k:event[k] for k in ('type','isTerminal') if k in event})
        elif kind in ('tool_execution_start','tool_execution_end'):
            item=dict(event)
            if kind=='tool_execution_start':
                item['commandAudit']=e.inspect_bash(event['args'])
                item['exactBashAudit']=w.inspect(event['args'],task[w.KEY])
                from live_output import inspect
                item['liveOutputAudit']=inspect(event['args'])
                item['preexecGate']=gate.annotation(event['toolCallId'],event['args'])
                raw.append(dict(version=1,type=kind,id=event['toolCallId'],args=event['args']))
            else:
                from async_evidence import bash_async
                item['asyncBash']=bash_async(event['result'])
                raw.append(dict(version=1,type=kind,id=event['toolCallId'],is_error=event['isError'],result=event['result']))
            compact.append(item)
    (root/'events.jsonl').write_text(''.join(json.dumps(i)+'\n' for i in compact))
    (root/'tool-evidence.jsonl').write_text(''.join(json.dumps(i)+'\n' for i in raw))
    return e.binding(root,async_required=True,live_output_required=True,exact_bash_policy=task[w.KEY])


class RuntimeTests(unittest.TestCase):
    def test_actual_typo_hint_then_exact_catalog_arguments_execute_with_original_status(self):
        def setup(root, work):
            task, _ = scenario(root, work)
            entries = catalog(task, root)
            wrong = entries[0]['arguments']['command'].replace('PYTHONDONTWRITEBYTECODE', 'PYONDONTWRITEBYTECODE')
            return task, [{'command': wrong}, entries[0]['arguments'], entries[1]['arguments']]
        def check(root, task, meta, result):
            bound = journals(root, task, result)
            self.assertEqual(bound['preexec_gate']['denied_requests'], 1)
            gate = g.for_run(root); gate.refresh(complete=True)
            denial = next(row for row in gate.entries.values() if row['decision'] == 'block')
            self.assertIn('reference=T1.direct', denial['reason'])
            self.assertIn('first_difference_codepoint=6', denial['reason'])
            self.assertEqual(denial['reason'].count('Hint is not permission.'), 1)
            self.assertTrue(any('reference=T1.direct' in str(x) for x in result['requests'][1]['messages']))
            logs = list((root/'worker-test-logs').glob('*/exit-code'))
            self.assertEqual(len(logs), 1); self.assertEqual(logs[0].read_text().strip(), '7')
        run_fixture(setup, check)

    def test_actual_legacy_gate_keeps_original_denial_format(self):
        def setup(root, work):
            task, _ = scenario(root, work)
            return task, [{'command': 'T1.direct'}, {'command': w.commands(task, root)[1][0]}]
        def check(root, task, meta, result):
            self.assertEqual(task[g.KEY]['version'], 1)
            gate = g.for_run(root); gate.refresh(complete=True)
            denial = next(row for row in gate.entries.values() if row['decision'] == 'block')
            self.assertNotIn('Exact-test hint:', denial['reason'])
            self.assertEqual(journals(root, task, result)['preexec_gate']['denied_requests'], 1)
        run_fixture(setup, check, gate_version=1)

    def test_actual_same_batch_non_bash_lockouts_have_raw_and_correlated_failure_evidence(self):
        def setup(root,work):
            task,_=scenario(root,work)
            batch=[{'command':'wc -l context.txt'} for _ in range(3)]
            batch += [{'_fixture_tool':'write','path':'blocked.txt','content':'must not be written'},
                      {'_fixture_tool':'read','path':'context.txt'}]
            return task,[batch]
        def check(root,task,meta,result):
            self.assertEqual(result['outcome'],('failed',1))
            self.assertEqual(m.load(root/'failure.json')['reason'],'preexec_bash_denial_limit')
            self.assertFalse((root/'snapshot/blocked.txt').exists())
            gate=g.for_run(root);gate.refresh(complete=True)
            self.assertEqual({row['tool_name'] for row in gate.lockouts.values()},{'read','write'})
            rows=[json.loads(x) for x in (root/'tool-evidence.jsonl').read_text().splitlines()]
            raw=[row for row in rows if row['type']=='gate_lockout_start']
            self.assertEqual(len(raw),2)
            self.assertEqual(next(row for row in raw if row['tool_name']=='write')['args']['content'],'must not be written')
            receipt=m.load(root/'gate-lockout-evidence.json')
            self.assertEqual(receipt['acceptance'],'failed_attempt_not_eligible')
            self.assertEqual(len(receipt['lockouts']),2)
            self.assertTrue(all(x['rpc_correlation']=='matched' for x in receipt['lockouts']))
            self.assertEqual(m.load(root/'progress.json')['tool_counts'],{})
            path=root/'tool-evidence.jsonl'
            for row in rows:
                if row['type']=='gate_lockout_end':row['is_error']=False
            path.write_text(''.join(json.dumps(x)+'\n' for x in rows))
            self.assertTrue(all(x['rpc_correlation']=='mismatch' for x in gate.failure_evidence()['lockouts']))
            rows=[x for x in rows if x['type']!='gate_lockout_end']
            path.write_text(''.join(json.dumps(x)+'\n' for x in rows))
            self.assertTrue(all(x['rpc_correlation']=='incomplete' for x in gate.failure_evidence()['lockouts']))
        run_fixture(setup,check,supervisor=True)

    def test_actual_supervisor_continues_after_denial_and_binds_success(self):
        def setup(root,work):
            task,_=scenario(root,work)
            return task,[{'command':'wc -l context.txt; printf BAD >denied.txt'}, {'command':w.commands(task,root)[1][0]}]
        def check(root,task,meta,result):
            self.assertEqual(result['outcome'],('exited',0))
            self.assertFalse((root/'snapshot/denied.txt').exists())
            self.assertEqual((root/'snapshot/allowed.txt').read_text(),'executed')
            bound=e.binding(root,async_required=True,live_output_required=True,exact_bash_policy=task[w.KEY])
            self.assertEqual(bound['preexec_gate']['denied_requests'],1)
            summary=m.summarize_events(root)
            self.assertEqual(summary['preexec_gate']['denied_requests'],1)
            self.assertEqual(summary['exact_bash']['rejected_requests'],0)
            self.assertEqual(m.load(root/'progress.json')['tool_counts']['bash'],1)
        run_fixture(setup,check,supervisor=True)

    def test_actual_supervisor_missing_gate_has_zero_provider_requests(self):
        def mutate(root,task,meta,env,argv):
            index=argv.index('--extension');del argv[index:index+2]
        def check(root,task,meta,result):
            self.assertEqual(result['outcome'],('failed',1))
            self.assertEqual(result['requests'],[])
            self.assertEqual(m.load(root/'failure.json')['reason'],'preexec_gate_invalid')
        run_fixture(scenario,check,mutate=mutate,supervisor=True)

    def test_actual_block_then_direct_and_logged_exit_status_and_evidence(self):
        def setup(root,work):
            task,_=scenario(root,work)
            direct=w.commands(task,root)[1][0]; logged=w.commands(task,root)[2][0]
            return task,[{'command':'wc -l context.txt; printf BAD >denied.txt'},
                         {'command':direct,'cwd':str(work),'timeout':20}, {'command':logged}]
        def check(root,task,meta,result):
            self.assertFalse((root/'snapshot/denied.txt').exists())
            self.assertEqual((root/'snapshot/allowed.txt').read_text(),'executed')
            logs=list((root/'worker-test-logs').glob('*/exit-code'))
            self.assertEqual(len(logs),1); self.assertEqual(logs[0].read_text().strip(),'7')
            self.assertIn('full stdout',(logs[0].parent/'stdout.log').read_text())
            self.assertIn('full stderr',(logs[0].parent/'stderr.log').read_text())
            bound=journals(root,task,result)
            self.assertEqual(bound['preexec_gate']['denied_requests'],1)
            self.assertEqual(bound['exact_bash']['bash_calls'],2)
            self.assertFalse(bound['blocked_requests'])
            messages=result['requests'][1]['messages']
            self.assertTrue(any('Command was NOT executed' in str(i) for i in messages))
            self.assertTrue(any('code 7' in str(i) for i in result['events']))
            review={'model':'gpt-5.6-sol','effort':'max','process_review':{'verdict':'pass','binding':bound,'reason':'fixture','async_jobs':[],
                    'coverage_resolution':'Inspected real logged test envelope and exit 7.', 'temporary_path_resolution':'Fixture owns root and all consumers finish before context cleanup.'}}
            with self.assertRaisesRegex(RuntimeError,'explicit Sol disposition'):
                e.validate_review(root,task,meta,review)
            review['process_review']['preexec_denial_resolution']='Inspected pre-execution denial and subsequent exact tests.'
            e.validate_review(root,task,meta,review)
            # Matching command alone is insufficient: raw denial result must match.
            path=root/'tool-evidence.jsonl'; original=path.read_bytes()
            rows=[json.loads(s) for s in original.splitlines()]
            rows[1]['result']={'content':[{'type':'text','text':'forged result'}]}
            path.write_text(''.join(json.dumps(v)+'\n' for v in rows))
            with self.assertRaises(g.GateError):
                e.binding(root,async_required=True,live_output_required=True,exact_bash_policy=task[w.KEY])
            path.write_bytes(original)
            gate_path=root/g.JOURNAL; original=gate_path.read_bytes()
            for invalid in (original[:-1],original+original.splitlines()[1]+b'\n',original.splitlines()[0]+b'\n'):
                gate_path.write_bytes(invalid)
                with self.assertRaises((g.GateError,RuntimeError)):
                    e.binding(root,async_required=True,live_output_required=True,exact_bash_policy=task[w.KEY])
            gate_path.write_bytes(original)
        run_fixture(setup,check)

    def test_actual_parallel_third_denial_latches_and_prevents_later_allowed_command(self):
        def setup(root,work):
            task,_=scenario(root,work)
            commands=[{'command':x+'; printf BAD >denied.txt'} for x in ('cat context.txt','sd old new context.txt','sha256sum context.txt')]
            return task,[commands,{'command':w.commands(task,root)[1][0]}]
        def check(root,task,meta,result):
            gate=g.for_run(root); summary=gate.refresh(complete=True)
            self.assertEqual(summary['denied_requests'],3);self.assertTrue(summary['exhausted'])
            self.assertEqual(len(gate.entries),4)
            self.assertTrue(all(row['decision']=='block' for row in gate.entries.values()))
            self.assertFalse((root/'snapshot/denied.txt').exists())
            self.assertFalse((root/'snapshot/allowed.txt').exists())
            with self.assertRaisesRegex(g.GateError,'budget exhausted'):journals(root,task,result)
        run_fixture(setup,check)

    def test_actual_gate_and_python_agree_on_argument_and_unicode_boundaries(self):
        def setup(root,work):
            task,_=scenario(root,work); command=w.commands(task,root)[1][0]
            vectors=[None,{}, {'command':''}, {'command':False},{'command':'x'*32769}, {'command':'😀'*32769}]
            for trim in ('',' ','\x1c','\x85','\u2000','\ufeff'):
                vectors.append({'command':trim+command+trim})
            for extra in ({'cwd':str(work)},{'cwd':'.'},{'cwd':None},{'cwd':str(work)+'/.'},{'cwd':str(work)+'\x00'},
                          {'timeout':True},{'timeout':None},{'timeout':0},{'timeout':3601},{'timeout':.01},{'timeout':3600},{'env':{}},{'async':True}):
                vectors.append(dict(command=command,**extra))
            vectors += [{'command':x} for x in ('wc -l context.txt','python3 -B -c "import PIL"','sha256sum context.txt')]
            vectors += [{'command': command.replace('PYTHONDONTWRITEBYTECODE', 'PYONDONTWRITEBYTECODE')},
                        {'command': 'T1.direct'}, {'command': command + ' PRIVATE'},
                        {'command': w.commands(task, root)[2][0].replace('exit ', 'printf ')}]
            return task,vectors
        def check(root,task,meta,result):
            _,vectors=setup(root,root/'snapshot')
            expected=[w.inspect(v,task[w.KEY])['status'] for v in vectors]
            self.assertTrue((root/'parity.json').exists(),str(result))
            self.assertEqual(json.loads((root/'parity.json').read_text()),expected)
            self.assertEqual(json.loads((root/'hint-parity.json').read_text()),
                             [correction_hint(v, task[w.KEY], catalog(task, root)) for v in vectors])
            self.assertEqual(result['requests'],[])
        run_fixture(setup,check,prompt=False,parity=True)

    def test_actual_extension_registration_missing_never_prompts(self):
        def mutate(root,task,meta,env,argv):
            index=argv.index('--extension');del argv[index:index+2]
        def check(root,task,meta,result):
            self.assertEqual(result['requests'],[])
            with self.assertRaises(g.GateError): g.for_run(root).refresh()
        run_fixture(scenario,check,mutate=mutate,prompt=False)

    def test_actual_matching_command_with_wrong_cwd_is_not_executed(self):
        def setup(root,work):
            task,_=scenario(root,work)
            return task,[{'command':w.commands(task,root)[1][0],'cwd':'.'}]
        def check(root,task,meta,result):
            self.assertFalse((root/'snapshot/allowed.txt').exists())
            gate=g.for_run(root);gate.refresh(complete=True)
            self.assertEqual(next(iter(gate.entries.values()))['status'],'invalid_arguments')
            bound=journals(root,task,result)
            self.assertEqual(bound['preexec_gate']['denied_requests'],1)
        run_fixture(setup,check)


class PolicyTests(unittest.TestCase):
    def test_catalog_changes_fail_closed_and_hashes_remain_exact(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d); task, _ = scenario(run, run)
            task['process_evidence_policy'] = e.POLICY_GATE; task[w.KEY] = w.resolve(task, run)
            entries = catalog(task, run)
            self.assertEqual(validate_catalog(entries, task[w.KEY]), entries)
            self.assertEqual(w.inspect({'command': 'T1.direct'}, task[w.KEY])['status'], 'undeclared_command')
            for mutate in (lambda x: x[0]['arguments'].update(command='python -c "import os"'),
                           lambda x: x[0].update(id='T2.direct'), lambda x: x.pop()):
                bad = copy.deepcopy(entries); mutate(bad)
                with self.assertRaises(ValueError): validate_catalog(bad, task[w.KEY])
            g.prepare(task, run); meta = copy.deepcopy(task)
            g.launch(run, task, meta, {}, [], '18.2.8')
            path = run/g.CONFIG; original = path.read_bytes()
            config = json.loads(original); config['test_catalog'][0]['arguments']['command'] += ' '
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(g.GateError, 'catalog changed'):
                g.Gate(run, task[g.KEY], task[w.KEY])
            path.write_bytes(original)
            task['test_commands'][0].append('--different')
            with self.assertRaises(g.GateError): g.validate(task, meta, run)

    def test_frozen_policy_extension_version_and_config_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); task,_=scenario(run,run)
            task['process_evidence_policy']=e.POLICY_GATE;task[w.KEY]=w.resolve(task,run)
            g.prepare(task,run);meta=copy.deepcopy(task)
            g.validate(task,meta,run)
            with self.assertRaises(g.GateError):g.launch(run,task,meta,{},[],'18.2.9')
            data=(run/g.EXTENSION).read_bytes(); (run/g.EXTENSION).write_bytes(data+b'\n')
            with self.assertRaises(g.GateError):g.validate(task,meta,run)
            (run/g.EXTENSION).write_bytes(data)
            bad=copy.deepcopy(meta);bad[g.KEY]['max_denials']=30
            with self.assertRaises(g.GateError):g.validate(task,bad,run)
            missing=copy.deepcopy(task);missing.pop(g.KEY)
            with self.assertRaises(g.GateError):g.validate(missing,missing,run)
            env={};argv=['omp','--no-extensions'];g.launch(run,task,meta,env,argv,'18.2.8')
            self.assertIn('--no-extensions',argv);self.assertEqual(argv[-2:],['--extension',str(run/g.EXTENSION)])
            config=json.loads((run/g.CONFIG).read_text());config['rules']['allowed_command_sha256']=[]
            (run/g.CONFIG).write_text(json.dumps(config))
            with self.assertRaises(g.GateError):g.Gate(run,task[g.KEY],task[w.KEY])


class SupervisorTests(unittest.TestCase):
    setUp=rpc_fixtures.RpcTests.setUp
    execute=rpc_fixtures.RpcTests.execute

    def setup_gate(self):
        self.run = self.run.resolve()
        self.task.update(worker_bash_mode=w.MODE,test_commands=[['/usr/bin/true']],process_evidence_policy=e.POLICY_GATE)
        self.task[w.KEY]=w.resolve(self.task,self.run);g.prepare(self.task,self.run)
        self.meta=copy.deepcopy(self.task);g.launch(self.run,self.task,self.meta,{},[],'18.2.8')
        (self.run/'task.json').write_text(json.dumps(self.task));(self.run/'metadata.json').write_text(json.dumps(self.meta))

    def test_registration_failure_stops_before_prompt(self):
        self.setup_gate()
        with mock.patch.object(self.client,'prompt') as prompt:
            self.assertEqual(self.execute(self.meta),('failed',1));prompt.assert_not_called()
        self.assertEqual(m.load(self.run/'failure.json')['reason'],'preexec_gate_invalid')

    def test_third_denial_stops_even_with_terminal_event_and_never_resumes(self):
        self.setup_gate();config=m.load(self.run/g.CONFIG)
        rows=[dict(type='ready',version=1,nonce=config['nonce'],policy_sha256=config['policy_sha256'])]
        (self.run/g.JOURNAL).write_text(json.dumps(rows[0])+'\n')
        def prompt(message):
            for n in range(1,4):
                ident=str(n)
                reason=(f"TEAM_DEV_GATE_BLOCK:{config['nonce']}:{ident}:{n}: Command was NOT executed. "
                        'Use read/grep/glob for inspection, edit/write for source, or copy an exact prepared test command and its permitted arguments. '+
                        ('Correction budget exhausted; stop.' if n==3 else 'Correct the tool choice; do not repeat this request.'))
                args={'command':'wc -l context.txt'}
                reason += ' ' + correction_hint(args, self.task[w.KEY], config['test_catalog'])
                row=dict(type='decision',id=ident,args=args,status='undeclared_command',decision='block',denials=n,reason=reason)
                with (self.run/g.JOURNAL).open('a') as out:out.write(json.dumps(row)+'\n')
                self.client.emit(NS(type='tool_execution_start',tool_name='bash',tool_call_id=ident,args=args))
                self.client.emit(NS(type='tool_execution_end',tool_name='bash',tool_call_id=ident,is_error=True,result={'content':[{'type':'text','text':reason}],'details':{}}))
            self.client.emit(NS(type='agent_end',is_terminal=True))
        self.client.prompt=prompt
        self.assertEqual(self.execute(self.meta),('failed',1))
        self.assertEqual(m.load(self.run/'failure.json')['reason'],'preexec_bash_denial_limit')
        self.assertEqual(m.load(self.run/'progress.json')['tool_counts'],{})
        meta=m.load(self.run/'metadata.json');meta['process']='failed';m.dump(self.run/'metadata.json',meta)
        with self.assertRaisesRegex(RuntimeError,'fresh task'):m.resume(self.run,self.run/'absent')


if __name__=='__main__':unittest.main()
