"""Offline scripted provider fixture; no real provider or user credentials."""
import json, os, queue, shutil, subprocess, tempfile, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import bash_gate as gate_module
import worker_bash
import process_evidence

def run_fixture(scenario, check, *, mutate=None, before_prompt=None, prompt=True, parity=False, supervisor=False, gate_version=2):
    omp = shutil.which('omp')
    if omp is None:
        raise RuntimeError('OMP 18.2.8 must be installed and available on PATH for runtime fixtures')
    with tempfile.TemporaryDirectory(prefix='team-dev-gate-fixture-') as temporary:
        root = Path(temporary)
        agent = root/'agent'; agent.mkdir()
        work = root/'snapshot'; work.mkdir()
        (work/'context.txt').write_text('one\ntwo\n')
        received=[]
        provider_name, model_name = ('opencode-ds', 'deepseek-v4-flash') if supervisor else ('local-fixture','fixture')
        task, commands = scenario(root, work)
        task['process_evidence_policy'] = process_evidence.POLICY_GATE
        task['worker_bash_policy'] = worker_bash.resolve(task, root)
        gate_module.prepare(task, root)
        if gate_version == 1:
            # Explicit legacy-policy fixture; the extension retains v1 formatting.
            task[gate_module.KEY]['version'] = 1
            task[gate_module.KEY].pop('catalog_sha256')
        meta = json.loads(json.dumps(task))
        (root/'task.json').write_text(json.dumps(task))
        (root/'metadata.json').write_text(json.dumps(meta))
        (root/'worker-test-logs').mkdir()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(data)
                i=len(received)-1
                if i<len(commands):
                    batch = commands[i] if isinstance(commands[i], list) else [commands[i]]
                    calls=[]
                    for j,args in enumerate(batch):
                        args=dict(args);name=args.pop('_fixture_tool','bash')
                        calls.append({'index':j,'id':f'fixture_{i}_{j}','type':'function','function':{'name':name,'arguments':json.dumps(args)}})
                    delta={'role':'assistant','tool_calls':calls}
                    stop='tool_calls'
                else:
                    delta={'role':'assistant','content':'fixture finished'}; stop='stop'
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
                for chunk in ({'choices':[{'index':0,'delta':delta,'finish_reason':None}]},{'choices':[{'index':0,'delta':{},'finish_reason':stop}]}):
                    self.wfile.write(('data: '+json.dumps(dict(id='fixture',object='chat.completion.chunk',created=1,model=model_name,**chunk))+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        provider=root/'provider.ts'
        provider.write_text('export default function(pi) { pi.registerProvider('+json.dumps(provider_name)+', '+json.dumps({
            'baseUrl':f'http://127.0.0.1:{server.server_port}/v1','apiKey':'fixture-only','api':'openai-completions',
            'models':[{'id':model_name,'name':'fixture','reasoning':supervisor,'input':['text'],'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0},'contextWindow':32768,'maxTokens':1024}]})+'); }')
        env={k:v for k,v in os.environ.items() if k in ('PATH','USER','TMPDIR','LANG')}
        env.update(PI_CODING_AGENT_DIR=str(agent))
        argv=[omp,'--mode','rpc','--cwd',str(work),'--model',provider_name+'/'+model_name,'--thinking','low' if supervisor else 'off','--no-extensions','-e',str(provider),'--no-skills','--no-rules','--no-title','--no-prewalk','--no-lsp','--no-pty','--no-session','--tools','read,write,edit,grep,glob,bash','--system-prompt','Execute the fixture.']
        actual_version=subprocess.run([argv[0],'--version'],capture_output=True,text=True,check=True).stdout.strip()
        gate_module.launch(root, task, meta, env, argv, actual_version)
        if parity:
            (root/'vectors.json').write_text(json.dumps(commands,ensure_ascii=True))
            parity_source = root/'parity.ts'
            classifier=(root/gate_module.EXTENSION).read_text().split('export default function(pi: any)')[0]
            parity_source.write_text(classifier+'\nimport {writeFileSync} from "node:fs"; export default function(pi) { pi.on("session_start",()=>{const c=JSON.parse(readFileSync(process.env.TEAM_DEV_GATE_CONFIG,"utf8")); const a=JSON.parse(readFileSync('+json.dumps(str(root/'vectors.json'))+',"utf8")); writeFileSync('+json.dumps(str(root/'parity.json'))+',JSON.stringify(a.map(x=>classify(x,c.rules)))); writeFileSync('+json.dumps(str(root/'hint-parity.json'))+',JSON.stringify(a.map(x=>correctionHint(x,c.rules,c.test_catalog || [])))); }); }')
            argv.extend(['-e',str(parity_source)])
        if mutate: mutate(root, task, meta, env, argv)
        if supervisor:
            # Simulated serving-model labels ONLY, so production state validation
            # remains intact. The endpoint above is strictly loopback.
            import rpc_worker
            import team_dev
            task.update(worker_thinking='low', timeout_seconds=10,
                        progress={'warn_seconds':5,'stall_seconds':8,'first_write_seconds':9})
            (root/'task.json').write_text(json.dumps(task))
            (root/'prompt.md').write_text('Offline protocol fixture')
            try:
                outcome=rpc_worker.execute(root,meta,task,env,argv,team_dev.dump,
                                           lambda pid:'fixture-process',lambda:[])
                return check(root,task,meta,{'outcome':outcome,'requests':received})
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2)
        events=[]; inbox=queue.Queue()
        with (root/'stderr.log').open('w+') as err:
            proc=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,env=env,cwd=work)
            def listen():
                for line in proc.stdout:
                    try: inbox.put(json.loads(line))
                    except ValueError: inbox.put({'unparsed':line})
            reader=threading.Thread(target=listen,daemon=True); reader.start()
            try:
                deadline=time.monotonic()+40
                while time.monotonic()<deadline:
                    try: event=inbox.get(timeout=.25)
                    except queue.Empty:
                        if proc.poll() is not None: break
                        continue
                    events.append(event)
                    if event.get('type')=='ready':
                        proc.stdin.write(json.dumps({'id':'fixture-state','type':'get_state'})+'\n');proc.stdin.flush()
                    if event.get('type')=='response' and event.get('id')=='fixture-state':
                        if before_prompt: before_prompt(root,task,meta)
                        if not prompt: break
                        proc.stdin.write(json.dumps({'id':'fixture-prompt','type':'prompt','message':'Run fixture'})+'\n');proc.stdin.flush()
                    if event.get('type')=='agent_end': break
                return check(root,task,meta,{'events':events,'requests':received,'stderr':(root/'stderr.log').read_text()})
            finally:
                proc.stdin.close()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill();proc.wait()
                reader.join(timeout=2);proc.stdout.close()
                server.shutdown();server.server_close();thread.join(timeout=2)
