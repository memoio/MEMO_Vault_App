"""Export explicitly reviewed AgentLab decisions to LLaMA-Factory; no training."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from .agentlab_data_audit import classify_record, load_partitions
from .swe_llamafactory import factory_encoder, fit_decision, token_hash

MODEL_REVISION = '2fc06364715b967f1860aea9cf38778875588b17'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def select_reviewed_decision(row, partitions, review, source_sha256):
    info = classify_record(row, partitions)
    if info['partition'] == 'test':
        raise ValueError('test samples are reserved')
    if info['partition'] not in ('train', 'validation'):
        raise ValueError('source_id_unmatched or invalid partition')
    if review.get('id') != row['id'] or review.get('approved') is not True:
        raise ValueError('missing explicit sample approval')
    if review.get('source_sha256') != source_sha256:
        raise ValueError('review source sha256 mismatch')
    if not isinstance(review.get('evidence'), str) or not review['evidence'].strip():
        raise ValueError('review evidence required')
    hard = [x for x in info['reasons'] if x not in
            ('source_revision_and_supervision_unverified', 'browsergym_live_id_mapping_unverified')]
    if hard:
        raise ValueError(','.join(hard))
    if not isinstance(row.get('system'), str) or not row['system'].strip():
        raise ValueError('empty system prompt')
    return {'id': row['id'], 'trajectory_id': info['trajectory_id'], 'split': info['partition'],
            'messages': [{'role': 'system', 'content': row['system']}, row['conversations'][0]],
            'target': row['conversations'][1]['content'], 'evidence': review['evidence']}


def export(args):
    if args.max_tokens < 2 or args.max_steps < 1:
        raise ValueError('max-tokens >= 2 and max-steps >= 1 required')
    prepared = args.output_dir.resolve()
    train_out = args.train_output_dir.resolve()
    if prepared.exists() or train_out.exists() or prepared == train_out or prepared in train_out.parents:
        raise ValueError('data and training outputs must be new, separate directories')
    partitions = load_partitions(args.manifest)
    digest = sha256(args.input)
    reviews = {}
    for line in args.review.read_text(encoding='utf-8').splitlines():
        r = json.loads(line)
        if r['id'] in reviews:
            raise ValueError('duplicate review id')
        reviews[r['id']] = r
    if not reviews:
        raise ValueError('no reviewed records')
    encode = factory_encoder(args.model, MODEL_REVISION, args.local_only)
    prepared.mkdir(parents=True, exist_ok=False)
    counts, seen = Counter(), set()
    outputs = {s: (prepared / f'{s}.jsonl').open('x', encoding='utf-8') for s in ('train', 'validation')}
    try:
        with args.input.open(encoding='utf-8') as source, (prepared/'manifest.jsonl').open('x', encoding='utf-8') as manifest, (prepared/'excluded.jsonl').open('x', encoding='utf-8') as excluded:
            for line_num, line in enumerate(source, 1):
                row = json.loads(line)
                ident = row.get('id')
                if ident not in reviews:
                    continue
                if ident in seen:
                    raise ValueError(f'duplicate source id: {ident}')
                seen.add(ident)
                try:
                    decision = select_reviewed_decision(row, partitions, reviews[ident], digest)
                    fitted = fit_decision(decision, encode, args.max_tokens, False)
                    if fitted is None:
                        raise ValueError('length_limit_no_truncation')
                except ValueError as exc:
                    counts['excluded'] += 1
                    excluded.write(json.dumps({'id':ident,'source_line':line_num,'reason':str(exc)},ensure_ascii=False)+'\n')
                    continue
                split = decision['split']
                counts[split] += 1
                messages = [{'from':'human','value':fitted['messages'][0]['content']},
                            {'from':'gpt','value':decision['target']}]
                outputs[split].write(json.dumps({'system':fitted['system'],'conversations':messages},ensure_ascii=False)+'\n')
                manifest.write(json.dumps({'id':ident,'source_line':line_num,'split':split,
                    'trajectory_id':decision['trajectory_id'],'review_evidence':decision['evidence'],
                    'original_tokens':fitted['original_tokens'],'final_tokens':len(fitted['input_ids']),
                    'truncated':False,'input_ids_sha256':token_hash(fitted['input_ids']),
                    'labels_sha256':token_hash(fitted['labels'])},ensure_ascii=False)+'\n')
    finally:
        for handle in outputs.values():
            handle.close()
    missing = sorted(set(reviews) - seen)
    report = {'source_sha256':digest,'source_manifest_sha256':sha256(args.manifest),
              'review_sha256':sha256(args.review),'model':args.model,'model_revision':MODEL_REVISION,'revision':MODEL_REVISION,
              'max_tokens':args.max_tokens,'truncate':False,'counts':dict(counts),'missing_review_ids':missing}
    if missing or not counts['train']:
        (prepared/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        raise ValueError('missing review IDs or no reviewed training samples; see report')
    registry = {f'agentlab_{s}':{'file_name':f'{s}.jsonl','formatting':'sharegpt',
                'columns':{'messages':'conversations','system':'system'}} for s in ('train','validation')}
    (prepared/'dataset_info.json').write_text(json.dumps(registry,indent=2)+'\n')
    config = {'model_name_or_path':args.model,'model_revision':MODEL_REVISION,'trust_remote_code':False,
        'quantization_bit':4,'quantization_method':'bnb','quantization_type':'nf4','double_quantization':True,
        'stage':'sft','do_train':True,'finetuning_type':'lora','lora_rank':8,'lora_alpha':16,
        'lora_target':'q_proj,v_proj','freeze_vision_tower':True,'freeze_multi_modal_projector':True,
        'dataset_dir':str(prepared),'dataset':'agentlab_train','template':'qwen3_5_nothink',
        'cutoff_len':args.max_tokens,'mask_history':True,'train_on_prompt':False,'packing':False,
        'preprocessing_num_workers':1,'output_dir':str(train_out),'overwrite_output_dir':False,
        'logging_steps':1,'save_strategy':'steps','save_steps':args.max_steps,'report_to':'none',
        'per_device_train_batch_size':1,'gradient_accumulation_steps':1,'learning_rate':2e-4,
        'max_steps':args.max_steps,'fp16':True,'seed':42}
    (prepared/'smoke.yaml').write_text(json.dumps(config,indent=2)+'\n')
    report['output_sha256'] = {n:sha256(prepared/n) for n in ('train.jsonl','validation.jsonl','dataset_info.json','smoke.yaml','manifest.jsonl','excluded.jsonl')}
    (prepared/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(report,ensure_ascii=False))


def verify(args):
    report = json.loads((args.prepared_dir/'report.json').read_text())
    for name, expected in report['output_sha256'].items():
        if sha256(args.prepared_dir/name) != expected:
            raise ValueError('prepared file changed: '+name)
    config = json.loads((args.prepared_dir/'smoke.yaml').read_text())
    if Path(config['dataset_dir']).resolve() != args.prepared_dir.resolve():
        raise ValueError('prepared directory moved; re-export')
    if Path(config['output_dir']).exists():
        raise ValueError('training output already exists')
    print('Prepared data and configuration hashes verified; training not started.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('export')
    for name in ('input','manifest','review','output-dir','train-output-dir'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--model',required=True)
    p.add_argument('--local-only',action='store_true')
    p.add_argument('--max-tokens',type=int,default=2048)
    p.add_argument('--max-steps',type=int,default=10)
    p.set_defaults(func=export)
    p=sub.add_parser('verify')
    p.add_argument('--prepared-dir',type=Path,required=True)
    p.set_defaults(func=verify)
    args=parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
