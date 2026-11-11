"""Trace fixed Mind2Web train and split SFT by website and identical task."""
import argparse
import collections
import hashlib
import json
import re
import unicodedata
from pathlib import Path
import ijson
from everos.agent_training.agentlab_restore_split import SFT_SHA, LEGACY_SHA, goal_matches, sha

RAW_SHA = {
 'train_0.json':'c8b622901057bca813a6d171733c41e4fc266c2902a23d63b9094c0add3f8f2c',
 'train_1.json':'41084922c50174ec032185707ae43bb04d3e8d725788b2e95936db629ba59100',
 'train_2.json':'3e8a77b835517a3b88d59d0afb3412448b3bbf7f6791db796459268f73106bdf',
 'train_3.json':'65077d1e9b89984e6fca2494c2a3137e4920a19161e5086b7aa88837b6188405',
 'train_4.json':'fcb8903310ffe43e3e1e9e50f8744b7a0ca0299e9772fef3fc124355e1eeeb0b',
 'train_5.json':'a6bf7490e3c8808363f829a6eb97a194578886ee7a73f3de65ec6d8c6cfca6dd',
 'train_6.json':'49b2764d8ce2d902448d024de0a7e3943169c3c4209d2fc6ddedcc353c4b3296',
 'train_7.json':'085e5bf60e0ba8a6bafb861bafc6a15660853f9408ca7f5817b11b6670bd9642',
 'train_8.json':'0cb825512cbc19a9ee0bd41b32f7b6cd2640ae0e91f3cb4263c70356600b6acf',
 'train_9.json':'07377a0c1a06c0aef22dd3c2ed400f393add51dc2cb727e859c0928acdaa5077',
 'train_10.json':'182542d7947b3fa9e90fc57a3d82d4d8f2997ca5a06664217720d7a78a956e33'}
SEED='mind2web-site-split-20261110-v1'


def normalize_task(task):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', task).casefold()).strip()


def assign_sites(tasks, reserved, seed):
    parent = {x['website']: x['website'] for x in tasks}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        a, b = find(a), find(b)
        if a != b: parent[max(a,b)] = min(a,b)
    by_task = {}
    for x in tasks:
        task = normalize_task(x['task'])
        if task in by_task: union(x['website'], by_task[task])
        else: by_task[task] = x['website']
    components = collections.defaultdict(list)
    for site in parent: components[find(site)].append(site)
    output = {}
    for sites in components.values():
        fixed = {reserved[site] for site in sites if site in reserved}
        if len(fixed) > 1: raise ValueError('reserved partition conflict for identical task or website')
        if fixed: partition = next(iter(fixed))
        else:
            key = seed + ':' + '\n'.join(sorted(sites))
            bucket = int(hashlib.sha256(key.encode()).hexdigest(),16) % 100
            partition = 'train' if bucket < 80 else 'validation' if bucket < 90 else 'test'
        for site in sites: output[site] = partition
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for a in ('input','raw-dir','legacy-manifest','audit','output-dir'): p.add_argument('--'+a,type=Path,required=True)
    args=p.parse_args()
    if sha(args.input)!=SFT_SHA or sha(args.legacy_manifest)!=LEGACY_SHA: raise ValueError('input hash mismatch')
    for name,h in RAW_SHA.items():
        if sha(args.raw_dir/name)!=h: raise ValueError('raw hash mismatch: '+name)
    legacy={}; reserved={}
    for line in args.legacy_manifest.open(encoding='utf-8'):
        row=json.loads(line); legacy[row['id']]=row
        old=reserved.setdefault(row['source_group'],row['partition'])
        if old != row['partition']: raise ValueError('legacy website conflict')
    raw={}
    for name,h in RAW_SHA.items():
        with (args.raw_dir/name).open('rb') as f:
            for i,x in enumerate(ijson.items(f,'item')):
                tid=x['annotation_id']; task=x['confirmed_task']; site=x['website']
                if tid in raw or not task or not site: raise ValueError('duplicate or incomplete raw task')
                raw[tid]={'trajectory_id':tid,'task':task,'website':site,'source_file':name,'source_file_sha256':h,'source_row_zero_based':i,'original_partition':'train','task_sha256':hashlib.sha256(normalize_task(task).encode()).hexdigest()}
    site_split=assign_sites(raw.values(),reserved,SEED)
    audits=[json.loads(l) for l in (args.audit/'records.jsonl').open(encoding='utf-8')]
    if json.loads((args.audit/'report.json').read_text())['source_sha256']!=SFT_SHA: raise ValueError('audit mismatch')
    decisions=[]; trajectories=collections.defaultdict(list); ids=set()
    with args.input.open('rb') as f:
        for n,line in enumerate(f,1):
            x=json.loads(line); a=audits[n-1]; tid=a['trajectory_id']; r=raw.get(tid)
            if a['source_line']!=n or a['id']!=x['id'] or x['id'] in ids: raise ValueError('SFT/audit mismatch')
            ids.add(x['id']); content=x['conversations'][0].get('content','')
            valid=bool(r and goal_matches(r['task'],content))
            item={'id':x['id'],'trajectory_id':tid,'source_line':n,'source_line_sha256':hashlib.sha256(line).hexdigest(),
                  'sft_sha256':SFT_SHA,'source_evidence':'raw_id_and_goal' if valid else 'unmatched_or_goal_mismatch',
                  'partition':site_split[r['website']] if valid else None, 'action':a.get('action'),
                  'original_loss':'unknown','training_ready':False,'audit_reasons':a['reasons']}
            if r: item.update({k:v for k,v in r.items() if k not in ('task','trajectory_id')})
            decisions.append(item); trajectories[tid].append(item)
    if len(decisions)!=len(audits): raise ValueError('audit count mismatch')
    for tid,items in trajectories.items():
        if len({(x['partition'],x['source_evidence']) for x in items})!=1:
            for x in items: x['partition']=None; x['source_evidence']='trajectory_inconsistent'
        if tid in legacy and items[0]['partition'] is not None:
            x=items[0]; old=legacy[tid]
            if (x['partition']!=old['partition'] or x['website']!=old['source_group'] or x['task_sha256']!=old['task_sha256'] or x['source_file_sha256']!=old['source_file_sha256'] or x['source_row_zero_based']!=old['source_row']):
                raise ValueError('legacy evidence mismatch: '+tid)
    args.output_dir.mkdir(parents=True,exist_ok=False)
    outs={k:(args.output_dir/(k+'.jsonl')).open('xb') for k in ('train','validation','test','quarantine')}
    try:
        with args.input.open('rb') as f:
            for item,line in zip(decisions,f): outs[item['partition'] or 'quarantine'].write(line)
    finally:
        for out in outs.values(): out.close()
    with (args.output_dir/'manifest.jsonl').open('x',encoding='utf-8') as f:
        for x in decisions: f.write(json.dumps(x,ensure_ascii=False)+'\n')
    with (args.output_dir/'raw-task-manifest.jsonl').open('x',encoding='utf-8') as f:
        for tid,r in raw.items():
            item={**r,'partition':site_split[r['website']],'in_sft':tid in trajectories}
            f.write(json.dumps(item,ensure_ascii=False)+'\n')
    summary={}
    for split in ('train','validation','test','quarantine'):
        items=[x for x in decisions if (x['partition'] or 'quarantine')==split]
        summary[split]={'decisions':len(items),'trajectories':len({x['trajectory_id'] for x in items}),'websites':len({x.get('website') for x in items if x.get('website')}),'actions':dict(collections.Counter(x['action'] or 'invalid' for x in items)),'sha256':sha(args.output_dir/(split+'.jsonl'))}
    report={'source_sha256':SFT_SHA,'legacy_sha256':LEGACY_SHA,'raw_sha256':RAW_SHA,'raw_tasks':len(raw),'sft_decisions':len(decisions),'sft_trajectories':len(trajectories),'raw_tasks_not_in_sft':len(set(raw)-set(trajectories)),'sft_trajectories_not_in_raw':len(set(trajectories)-set(raw)),'seed':SEED,'group_rule':'website and exact normalized task; preserve legacy site assignments; hash new components 80/10/10', 'stats':summary,'semantic_near_duplicate_review':'not performed','formal_experiment_ready':False,'training_ready':False,'manifest_sha256':sha(args.output_dir/'manifest.jsonl'),'raw_task_manifest_sha256':sha(args.output_dir/'raw-task-manifest.jsonl')}
    (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
