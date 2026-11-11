from everos.agent_training.swe_full_split import repo_group, split_for, choose_smoke

def test_repo_group_preserves_dots_and_rejects_ambiguous():
    assert repo_group('pdfminer__pdfminer.six.1a8bd2f7.lm_rewrite__abc') == 'pdfminer__pdfminer.six'
    assert repo_group('bad-id') is None
    assert repo_group('foo__bar.12345678.x.abcdef12.y') is None

def test_split_is_stable_and_grouped():
    assert split_for('owner__repo') == split_for('owner__repo')
    assert split_for('owner__repo') in {'train', 'validation', 'test'}

def test_smoke_limits_and_determinism():
    a=[{'traj_id':str(i),'group':'g'+str(i%5)} for i in range(100)]
    selected=choose_smoke(a, 20)
    assert len(selected)==20
    assert selected==choose_smoke(list(reversed(a)),20)
    assert len({x['traj_id'] for x in selected})==20
    assert len({x['group'] for x in selected})==5

def test_materialize_preserves_rows_and_smoke(tmp_path):
    import json
    import pyarrow as pa
    import pyarrow.parquet as pq
    from everos.agent_training.swe_materialize_split import run
    base=tmp_path/'base'; (base/'raw'/'data').mkdir(parents=True)
    idx=tmp_path/'index'; idx.mkdir()
    table=pa.table({'traj_id':['a','b','c'], 'messages':['one','two','three']})
    pq.write_table(table,base/'raw'/'data'/'sample.parquet')
    rows=[{'path':'data/sample.parquet','row':i,'split':s,'smoke':sm} for i,(s,sm) in enumerate([('train',True),('test',False),('train',False)])]
    (idx/'full_manifest.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    out=tmp_path/'out'; run(base,idx,out)
    assert pq.read_table(out/'full/train/data/sample.parquet').to_pylist()==[{'traj_id':'a','messages':'one'},{'traj_id':'c','messages':'three'}]
    assert pq.read_table(out/'smoke/train/data/sample.parquet').to_pylist()==[{'traj_id':'a','messages':'one'}]
    assert pq.read_table(out/'full/test/data/sample.parquet').to_pylist()==[{'traj_id':'b','messages':'two'}]
