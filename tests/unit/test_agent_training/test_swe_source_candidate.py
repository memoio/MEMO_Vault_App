from everos.agent_training.swe_source_candidate import convert

def test_convert_marks_unknown_loss_provenance():
    row={'traj_id':'t','instance_id':'i','messages':[{'role':'system','content':'s'},{'role':'user','content':'u'},{'role':'assistant','content':'<function=submit>\n</function>'}]}
    x=convert(row,'g')
    assert x['id']=='t' and x['source']['group']=='g'
    assert x['source']['loss_provenance']=='unknown'
    assert x['conversations'][1]['from']=='gpt'

def test_convert_rejects_non_alternating_and_bad_system():
    import pytest
    with pytest.raises(ValueError):
        convert({'traj_id':'t','instance_id':'i','messages':[{'role':'user','content':'u'}]},'g')

def test_smoke_tracks_unsupported_and_test_exclusion(tmp_path):
    import json
    import pyarrow as pa
    import pyarrow.parquet as pq
    from everos.agent_training.swe_source_candidate import run
    base=tmp_path/'input'; (base/'splits-v1').mkdir(parents=True)
    rows=[{'split':'train','traj_id':'a','format':'train','group':'g','path':'data/train-x.parquet','row':0},
          {'split':'train','traj_id':'b','format':'ticks','group':'g','path':'data/ticks-x.parquet','row':0},
          {'split':'test','traj_id':'c','format':'train','group':'h','path':'data/train-x.parquet','row':1}]
    (base/'splits-v1'/'smoke_manifest.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    dest=base/'datasets-v1/smoke/train/data'; dest.mkdir(parents=True)
    msgs=[{'role':'system','content':'s'},{'role':'user','content':'u'},{'role':'assistant','content':'<function=submit>\n</function>'}]
    pq.write_table(pa.Table.from_pylist([{'traj_id':'a','instance_id':'i','messages':msgs}]),dest/'train-00000.parquet')
    out=tmp_path/'out'; run(base,out)
    report=json.loads((out/'report.json').read_text())
    assert report['counts']=={'train_candidate':1,'train_unsupported_or_duplicate':1}
    assert '"id": "c"' not in (out/'train.jsonl').read_text()
    assert '"id": "b"' in (out/'excluded.jsonl').read_text()
