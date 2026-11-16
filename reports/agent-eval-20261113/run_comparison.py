import json,os,subprocess,time,pathlib
root=pathlib.Path('/root/EverOS');out=root/'reports/agent-eval-20261113/results.jsonl';tasks=[json.loads(x) for x in (out.parent/'tasks.jsonl').read_text().splitlines()]
env=os.environ.copy();env['MEMO_AGENT_MAX_NEW_TOKENS']='96';env['MEMO_AGENT_CPU_THREADS']='4'
worker=root/'src/everos/entrypoints/memo_vault/agent_inference_worker.py';python=root/'.memo-vault/agent-training/envs/qwen35-training/bin/python'
with (out.parent/'worker.log').open('w') as err, out.open('x') as result:
 p=subprocess.Popen([str(python),'-u',str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,env=env,bufsize=1)
 try:
  for task in tasks:
   for agent in ('default','openhands','swe','agentlab'):
    start=time.monotonic();request={'agent':agent,'messages':[{'role':'user','content':task['prompt']}]};p.stdin.write(json.dumps(request,ensure_ascii=False)+'\n');p.stdin.flush();line=p.stdout.readline()
    if not line:raise RuntimeError('worker exited')
    response=json.loads(line);row={'task_id':task['id'],'domain':task['domain'],'agent':agent,'elapsed_seconds':round(time.monotonic()-start,2),'max_new_tokens':96,'do_sample':False,**response};result.write(json.dumps(row,ensure_ascii=False)+'\n');result.flush();print(task['id'],agent,'ERROR' if 'error' in row else 'OK',flush=True)
 finally:
  p.stdin.close();p.terminate();p.wait(timeout=15)
