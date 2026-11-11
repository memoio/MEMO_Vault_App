import json
import pytest
from everos.agent_training.swe_train_run import make_config, check_readiness
from everos.agent_training.swe_eval_run import success

def test_training_candidate_only_explicit_smoke():
    with pytest.raises(ValueError): check_readiness({},'smoke',False)
    assert check_readiness({},'smoke',True) == 'technical_probe'
    with pytest.raises(ValueError): check_readiness({},'scale',True)
    assert check_readiness({'training_ready':True},'scale',False)=='reviewed'

def test_train_config_uses_dataset_and_distinct_output():
    c=make_config({'cutoff_len':4096},'/model','/data','/run/adapter','scale',75,4)
    assert c['max_steps']==75 and c['gradient_accumulation_steps']==4
    assert c['dataset']=='swe_train' and c['output_dir']=='/run/adapter'
    assert c['template']=='qwen3_5_nothink' and c['quantization_bit']==4

def test_eval_requires_failed_baseline_patch_and_passed_tests():
    assert success(1,0,True,0)
    assert not success(0,0,True,0)
    assert not success(1,0,False,0)
    assert not success(1,1,True,0)
    assert not success(1,0,True,1)

def test_train_prepare_produces_new_config_and_does_not_modify_data(tmp_path):
    import hashlib, subprocess, sys, os
    data=tmp_path/'data'; model=tmp_path/'model'; data.mkdir(); model.mkdir()
    (model/'config.json').write_text('{}'); (model/'model.safetensors').write_bytes(b'fixture')
    config={'template':'qwen3_5_nothink','cutoff_len':4096,'dataset':'swe_train'}
    (data/'smoke.yaml').write_text(json.dumps(config))
    for name in ('train.jsonl','validation.jsonl','dataset_info.json','manifest.jsonl','excluded.jsonl'): (data/name).write_text('{}\n')
    names=('smoke.yaml','train.jsonl','validation.jsonl','dataset_info.json','manifest.jsonl','excluded.jsonl')
    report={'revision':'2fc06364715b967f1860aea9cf38778875588b17','max_tokens':4096,'counts':{'train':{'samples':1},'validation':{'samples':1}},'output_sha256':{n:hashlib.sha256((data/n).read_bytes()).hexdigest() for n in names}}
    (data/'report.json').write_text(json.dumps(report)); target=tmp_path/'run'
    result=subprocess.run([sys.executable,'-m','everos.agent_training.swe_train_run','--mode','smoke','--data',str(data),'--model',str(model),'--run',str(target),'--technical-probe','--prepare-only'],capture_output=True,text=True,env=os.environ.copy())
    assert result.returncode==0, result.stderr
    out=json.loads((target/'training.yaml').read_text()); assert out['max_steps']==10 and out['dataset_dir']==str(data)
    assert json.loads((target/'run-report.json').read_text())['data_status']=='technical_probe'
    assert json.loads((data/'smoke.yaml').read_text())==config
