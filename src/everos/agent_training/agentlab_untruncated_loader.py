"""Audit actual LLaMA-Factory loader against exported tokens and labels."""
import argparse
import json
from pathlib import Path
from .swe_llamafactory import token_hash


def compare_encoded(actual, expected, index):
    if token_hash(actual['input_ids']) != expected['input_ids_sha256'] or token_hash(actual['labels']) != expected['labels_sha256']:
        raise ValueError(f'token/label mismatch at {index}')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared-dir',type=Path,required=True)
    p.add_argument('--model',required=True)
    p.add_argument('--max-samples',type=int,default=32)
    p.add_argument('--cutoff-len',type=int,required=True)
    args=p.parse_args()
    if args.max_samples < 1 or args.cutoff_len < 2: raise ValueError('invalid limit')
    from llamafactory.data import get_dataset, get_template_and_fix_tokenizer
    from llamafactory.hparams import get_train_args
    from transformers import AutoProcessor, AutoTokenizer
    path=args.prepared_dir.resolve(); report=json.loads((path/'report.json').read_text())
    config={'model_name_or_path':args.model,'model_revision':report['model_revision'],'trust_remote_code':False,
            'stage':'sft','do_train':True,'finetuning_type':'lora','dataset_dir':str(path),
            'dataset':'agentlab_train','template':'qwen3_5_nothink','cutoff_len':args.cutoff_len,
            'mask_history':True,'train_on_prompt':False,'packing':False,'max_samples':args.max_samples,
            'preprocessing_num_workers':1,'output_dir':'/tmp/vault-agentlab-lf-loader-audit','report_to':'none'}
    model_args,data_args,training_args,_,_=get_train_args(config)
    tokenizer=AutoTokenizer.from_pretrained(args.model,local_files_only=True,trust_remote_code=False)
    processor=AutoProcessor.from_pretrained(args.model,local_files_only=True,trust_remote_code=False)
    template=get_template_and_fix_tokenizer(tokenizer,data_args)
    manifest=[json.loads(l) for l in (path/'manifest.jsonl').open()]
    checked={}
    for part in ('train','validation'):
        expected=[x for x in manifest if x['split']==part][:args.max_samples]
        if not expected: raise ValueError(f'no samples to audit: {part}')
        if any(r['tokens']>args.cutoff_len for r in expected): raise ValueError(f'{part} exceeds audit cutoff')
        data_args.dataset=[f'agentlab_{part}']; data_args.eval_dataset=None
        dataset=get_dataset(template,model_args,data_args,training_args,'sft',tokenizer,processor)['train_dataset']
        if len(dataset)!=len(expected): raise ValueError(f'{part} count mismatch')
        for i,(actual,exp) in enumerate(zip(dataset,expected,strict=True)): compare_encoded(actual,exp,i)
        checked[part]=len(expected)
    print('LLaMA-Factory loader verified:',json.dumps(checked))

if __name__=='__main__': main()
