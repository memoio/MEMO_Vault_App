"""Run one SWE-Agent comparison arm on an isolated task; compare separate runs."""
import argparse, hashlib, json, subprocess, sys, threading, time
from pathlib import Path

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def success(baseline, agent, changed, after): return baseline==1 and agent==0 and changed and after==0

def command(argv,cwd,log,timeout):
    names = {'baseline-test.log': '检查修复前的测试',
             'sweagent.log': '运行 SWE-agent', 'post-test.log': '检查修复后的测试'}
    stage = names.get(log.name, log.name)
    started = time.monotonic()
    done = threading.Event()
    print(f'\n[评测] {stage}，超时上限 {timeout} 秒，日志: {log}', flush=True)

    def display_log():
        next_update = started + 30
        with log.open('r', encoding='utf-8', errors='replace') as reader:
            while True:
                chunk = reader.read(8192)
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    continue
                if done.is_set():
                    break
                now = time.monotonic()
                if now >= next_update:
                    elapsed = int(now - started)
                    remaining = max(0, timeout - elapsed)
                    print(f'\n[评测] {stage}已用 {elapsed} 秒；距超时约 {remaining} 秒（不是完成时间预测）', flush=True)
                    next_update = now + 30
                done.wait(0.5)

    with log.open('w', encoding='utf-8') as f:
        monitor = threading.Thread(target=display_log, daemon=True)
        monitor.start()
        try:
            try:
                result = subprocess.run(argv,cwd=cwd,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
                code = result.returncode
            except subprocess.TimeoutExpired:
                f.write('\nTIMEOUT\n')
                f.flush()
                code = 124
        finally:
            f.flush()
            done.set()
            monitor.join()
    print(f'\n[评测] {stage}结束，退出码 {code}，耗时 {time.monotonic()-started:.1f} 秒', flush=True)
    return code

def run(a):
    repo=a.repo.resolve(); config=a.config.resolve(); problem=a.problem.resolve(); out=a.output.resolve()
    print(f'[评测] 开始 {a.label}；结果目录: {out}', flush=True)
    if out.exists(): raise ValueError('output directory already exists')
    if '@sha256:' not in a.image: raise ValueError('use immutable docker image digest')
    commit=subprocess.check_output(['git','-C',str(repo),'rev-parse',a.commit+'^{commit}'],text=True).strip()
    if commit!=a.commit or len(commit)!=40: raise ValueError('commit must be full immutable SHA')
    out.mkdir(parents=True)
    for dest in ('baseline-repo','agent-repo'):
        subprocess.run(['git','clone','--quiet','--no-hardlinks',str(repo),str(out/dest)],check=True)
        subprocess.run(['git','-C',str(out/dest),'checkout','--quiet','--detach',commit],check=True)
    def test(dir,log):
        return command(['docker','run','--rm','--network','none','--memory',a.memory,'--cpus',a.cpus,'--mount',f'type=bind,src={dir},dst=/workspace','-w','/workspace','--entrypoint','/bin/sh',a.image,'-lc',a.test_command],None,log,a.timeout)
    before=test(out/'baseline-repo',out/'baseline-test.log')
    if before == 1:
        print('[评测] 原始测试按预期失败，开始运行 agent。', flush=True)
    meta={'label':a.label,'model_identity':a.model_identity,'source_commit':commit,'problem_sha256':sha(problem),'config_sha256':sha(config),'image':a.image,'test_command':a.test_command,'baseline_exit':before,'memory':a.memory,'cpus':a.cpus,'timeout':a.timeout,'protected_paths':a.protected_path}
    if before!=1:
        meta.update(status='invalid_baseline',reason='expected test command exit 1; check baseline-test.log')
        (out/'result.json').write_text(json.dumps(meta,indent=2)+'\n'); raise SystemExit('baseline test did not fail as expected')
    start=time.monotonic(); agent=command([a.sweagent,'run','--config',str(config),f'--env.repo.path={out / "agent-repo"}',f'--env.deployment.image={a.image}',f'--problem_statement.path={problem}',f'--output_dir={out / "trajectory"}','--actions.apply_patch_locally=True'],Path(a.sweagent_cwd).resolve(),out/'sweagent.log',a.timeout)
    status=subprocess.check_output(['git','-C',str(out/'agent-repo'),'status','--porcelain'],text=True)
    diff=subprocess.check_output(['git','-C',str(out/'agent-repo'),'diff','--binary'])
    (out/'applied.patch').write_bytes(diff)
    # A tracked applied patch is required. Untracked-only output is not a submitted patch.
    changed=bool(diff)
    changed_paths=set(subprocess.check_output(['git','-C',str(out/'agent-repo'),'diff','--name-only'],text=True).splitlines())
    protected_changed=bool(changed_paths.intersection(a.protected_path))
    after=test(out/'agent-repo',out/'post-test.log') if agent==0 and changed else None
    meta.update(status='finished',agent_exit=agent,patch_present=changed,untracked_or_modified=status,post_test_exit=after,protected_changed=protected_changed,resolved=success(before,agent,changed,after) and not protected_changed,elapsed_seconds=round(time.monotonic()-start,2))
    (out/'result.json').write_text(json.dumps(meta,indent=2)+'\n'); print(json.dumps(meta,indent=2))

def compare(a):
    base=json.loads((a.base/'result.json').read_text()); adapter=json.loads((a.adapter/'result.json').read_text())
    keys=('source_commit','problem_sha256','config_sha256','image','test_command','memory','cpus','timeout','protected_paths')
    if base.get('label')!='base' or adapter.get('label')!='adapter' or any(base.get(k)!=adapter.get(k) for k in keys): raise ValueError('comparison conditions differ')
    if base.get('status')!='finished' or adapter.get('status')!='finished': raise ValueError('both runs must be finished')
    print(json.dumps({'base_resolved':base['resolved'],'adapter_resolved':adapter['resolved'],'base_model':base['model_identity'],'adapter_model':adapter['model_identity'],'task_commit':base['source_commit']},indent=2))

def main():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='op',required=True)
    r=sub.add_parser('run'); r.add_argument('--label',choices=['base','adapter'],required=True); r.add_argument('--model-identity',required=True)
    for name in ('repo','config','problem','output'): r.add_argument('--'+name,type=Path,required=True)
    r.add_argument('--commit',required=True); r.add_argument('--image',required=True); r.add_argument('--test-command',required=True)
    r.add_argument('--protected-path',action='append',default=[]); r.add_argument('--sweagent',default='sweagent'); r.add_argument('--sweagent-cwd',required=True); r.add_argument('--memory',default='4g'); r.add_argument('--cpus',default='2'); r.add_argument('--timeout',type=int,default=900)
    c=sub.add_parser('compare'); c.add_argument('--base',type=Path,required=True); c.add_argument('--adapter',type=Path,required=True)
    a=p.parse_args(); run(a) if a.op=='run' else compare(a)
if __name__=='__main__': main()
