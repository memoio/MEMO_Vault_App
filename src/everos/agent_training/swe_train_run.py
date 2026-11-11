"""Run a recorded SWE LLaMA-Factory QLoRA smoke or scale experiment."""
import argparse, hashlib, json, math, subprocess, sys, time
from pathlib import Path

REVISION='2fc06364715b967f1860aea9cf38778875588b17'
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''): h.update(block)
    return h.hexdigest()

def check_readiness(report,mode,probe):
    if probe and mode!='smoke': raise ValueError('technical probe is allowed only for smoke')
    if report.get('training_ready') is True: return 'reviewed'
    if mode=='smoke' and probe: return 'technical_probe'
    raise ValueError('data is not marked training_ready; only explicit smoke probe is allowed')

def make_config(original,model,data,output,mode,steps,accum):
    config=dict(original)
    config.update(model_name_or_path=model, model_revision=REVISION, dataset_dir=data,
        dataset='swe_train', template='qwen3_5_nothink', output_dir=output,
        max_steps=steps, gradient_accumulation_steps=accum, gradient_checkpointing=True,
        quantization_bit=4, quantization_method='bnb', quantization_type='nf4',
        double_quantization=True, mask_history=True, train_on_prompt=False,
        logging_nan_inf_filter=False, save_steps=steps, overwrite_output_dir=False,
        eval_dataset='swe_validation' if mode=='scale' else None,
        eval_strategy='steps' if mode=='scale' else 'no', eval_steps=25 if mode=='scale' else None,
        per_device_eval_batch_size=1)
    return config

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['smoke','scale','full'],required=True); p.add_argument('--data',type=Path,required=True)
    p.add_argument('--model',type=Path,required=True); p.add_argument('--run',type=Path,required=True)
    p.add_argument('--steps',type=int); p.add_argument('--epochs',type=float,default=1.0); p.add_argument('--gradient-accumulation',type=int,default=4)
    p.add_argument('--technical-probe',action='store_true'); p.add_argument('--prepare-only',action='store_true')
    a=p.parse_args(); steps=a.steps or (10 if a.mode=='smoke' else 75)
    if (a.mode=='smoke' and steps!=10) or (a.mode=='scale' and not 50<=steps<=100) or a.gradient_accumulation<1 or a.epochs<=0 or (a.mode=='full' and a.steps is not None): p.error('smoke requires 10 steps; scale requires 50–100; full uses positive epochs; accumulation must be positive')
    data=a.data.resolve(); model=a.model.resolve(); run=a.run.resolve()
    if run.exists() or data in run.parents or run in data.parents: raise ValueError('run must be a new directory outside data')
    if not (model/'config.json').is_file() or not list(model.glob('*.safetensors')): raise ValueError('full local model weights required')
    report=json.loads((data/'report.json').read_text()); readiness=check_readiness(report,a.mode,a.technical_probe)
    if report.get('revision')!=REVISION: raise ValueError('unexpected model revision')
    for name,expected in report['output_sha256'].items():
        if sha(data/name)!=expected: raise ValueError('prepared file changed: '+name)
    old=json.loads((data/'smoke.yaml').read_text())
    if old.get('template')!='qwen3_5_nothink' or old.get('cutoff_len')!=report['max_tokens']: raise ValueError('template or cutoff mismatch')
    if not report['counts']['train'].get('samples') or not report['counts']['validation'].get('samples'): raise ValueError('empty data split')
    config=make_config(old,str(model),str(data),str(run/'adapter'),a.mode,steps,a.gradient_accumulation)
    if a.mode=='full':
        config.pop('max_steps',None); config['num_train_epochs']=a.epochs; config['save_steps']=100
        config['eval_dataset']='swe_validation'; config['eval_strategy']='steps'; config['eval_steps']=100
    run.mkdir(parents=True); config_path=run/'training.yaml'; config_path.write_text(json.dumps(config,indent=2)+'\n')
    result={'mode':a.mode,'data_status':readiness,'data_report_sha256':sha(data/'report.json'), 'config_sha256':sha(config_path),'model':str(model),'revision':REVISION,'status':'prepared'}
    (run/'run-report.json').write_text(json.dumps(result,indent=2)+'\n')
    if a.prepare_only: print(json.dumps(result,indent=2)); return
    start=time.monotonic()
    with (run/'training.log').open('w') as log:
        process=subprocess.run([sys.executable,'-m','llamafactory.cli','train',str(config_path)],stdout=log,stderr=subprocess.STDOUT)
    state_path=run/'adapter/trainer_state.json'
    if not state_path.exists():
        checkpoints=list((run/'adapter').glob('checkpoint-*/trainer_state.json'))
        if checkpoints: state_path=max(checkpoints,key=lambda p:int(p.parent.name.split('-')[-1]))
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    losses=[x['loss'] for x in state.get('log_history',[]) if 'loss' in x]
    weights=next((x for x in (run/'adapter/adapter_model.safetensors',run/'adapter/adapter_model.bin') if x.is_file()),None)
    ok=process.returncode==0 and (state.get('global_step')==steps if a.mode!='full' else state.get('global_step',0)>0) and bool(losses) and all(isinstance(x,(float,int)) and math.isfinite(x) for x in losses) and weights is not None and (run/'adapter/adapter_config.json').is_file()
    result.update(status='complete' if ok else 'failed',exit_code=process.returncode,seconds=round(time.monotonic()-start,2),global_step=state.get('global_step'),finite_losses=bool(losses) and all(isinstance(x,(int,float)) and math.isfinite(x) for x in losses),adapter_sha256=sha(weights) if weights else None)
    (run/'run-report.json').write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps(result,indent=2))
    if not ok: raise SystemExit('training incomplete: inspect training.log and trainer_state.json')

if __name__=='__main__': main()
