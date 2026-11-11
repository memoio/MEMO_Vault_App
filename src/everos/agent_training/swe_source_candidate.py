"""Create explicitly provisional SWE-Agent XML candidates from smoke parquet.

Unknown original loss masks and unsupported formats are not certified for training.
"""
import argparse
import collections
import json
from pathlib import Path
import pyarrow.parquet as pq
from .swe_lora_pipeline import ACTION


def convert(row, group):
    msgs=row['messages']
    if isinstance(msgs,str): msgs=json.loads(msgs)
    if not isinstance(msgs,list) or len(msgs)<3 or msgs[0].get('role')!='system' or not isinstance(msgs[0].get('content'),str):
        raise ValueError('missing system or messages')
    conversations=[]
    for i,msg in enumerate(msgs[1:]):
        role=msg.get('role'); value=msg.get('content')
        if role != ('user' if i%2==0 else 'assistant') or not isinstance(value,str):
            raise ValueError('nonalternating or non-text messages')
        conversations.append({'from':'human' if role=='user' else 'gpt','value':value})
    if not any(ACTION.search(x['value']) for x in conversations if x['from']=='gpt'):
        raise ValueError('no supported XML action')
    return {'id':row['traj_id'],'system':msgs[0]['content'],'conversations':conversations,
            'source':{'group':group,'instance_id':row['instance_id'],'loss_provenance':'unknown'}}


def run(base, output, scope="smoke"):
    if output.exists(): raise FileExistsError(output)
    selected=collections.defaultdict(list)
    for line in (base/'splits-v1'/('smoke_manifest.jsonl' if scope=='smoke' else 'full_manifest.jsonl')).open():
        r=json.loads(line)
        if r['split'] in ('train','validation'): selected[(r['split'],r['traj_id'])].append(r)
    chosen={}
    for key,rows in selected.items():
        for fmt in ('xml','train','tool'):
            matches=[r for r in rows if r['format']==fmt]
            if len(matches)==1:
                chosen[key]=matches[0]; break
    output.mkdir(parents=True)
    counts=collections.Counter(); seen=set()
    with (output/'train.jsonl').open('w') as train,(output/'validation.jsonl').open('w') as val,(output/'excluded.jsonl').open('w') as excluded:
        outs={'train':train,'validation':val}
        for split in ('train','validation'):
            for p in sorted((base/'datasets-v1'/scope/split/'data').glob('*.parquet')):
                fmt=p.name.split('-')[0]
                if fmt not in ('xml','train','tool'): continue
                for batch in pq.ParquetFile(p).iter_batches(batch_size=4, columns=['traj_id','instance_id','messages']):
                    for row in batch.to_pylist():
                        key=(split,row['traj_id']); meta=chosen.get(key)
                        if not meta or meta['format']!=fmt or key in seen: continue
                        seen.add(key)
                        try: result=convert(row,meta['group'])
                        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as err:
                            counts[split+'_invalid']+=1; excluded.write(json.dumps({'id':row['traj_id'],'reason':str(err)})+'\n'); continue
                        result['source'].update(format=fmt, raw_path=meta['path'], raw_row=meta['row'])
                        outs[split].write(json.dumps(result,ensure_ascii=False)+'\n'); counts[split+'_candidate']+=1
        for key in sorted(set(selected)-seen):
            counts[key[0]+'_unsupported_or_duplicate']+=1
            excluded.write(json.dumps({'split':key[0], 'id':key[1], 'reason':'no unique supported format'})+'\n')
    report={'status':'provisional_not_approved_for_training','reason':'source original loss mask unknown; tool semantics not verified', 'scope':scope, 'counts':dict(counts)}
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps(report,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--base',type=Path,required=True); p.add_argument('--output',type=Path,required=True); p.add_argument('--scope',choices=['smoke','full'],default='smoke'); a=p.parse_args(); run(a.base,a.output,a.scope)
