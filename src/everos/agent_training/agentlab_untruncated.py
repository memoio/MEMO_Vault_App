"""Draft untruncated AgentLab decisions for LLaMA-Factory, without training."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from .swe_llamafactory import factory_encoder, token_hash

REVISION = '2fc06364715b967f1860aea9cf38778875588b17'
PARTS = ('train', 'validation', 'test')
BUDGETS = (1024, 2048, 4096, 8192, 16384, 32768)


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def check_metadata(ident, split, line_hash, meta):
    if meta['id'] != ident or meta['partition'] != split or meta['source_line_sha256'] != line_hash:
        raise ValueError('decision ID, split or raw line hash mismatch')


def registry_for():
    return {f'agentlab_{part}': {'file_name': f'{part}.jsonl', 'formatting': 'sharegpt',
                                 'columns': {'messages': 'conversations', 'system': 'system'}}
            for part in ('train', 'validation')}


def adapt_decision(row, encode):
    system = row.get('system')
    messages = row.get('conversations')
    if not isinstance(system, str) or not system.strip() or not isinstance(messages, list) or len(messages) != 2:
        raise ValueError('invalid system or message count')
    if any(not isinstance(x, dict) for x in messages) or [m.get('role') for m in messages] != ['user', 'assistant']:
        raise ValueError('invalid message roles')
    user, target = (m.get('content') for m in messages)
    if not isinstance(user, str) or not isinstance(target, str) or not user.strip() or not target.strip():
        raise ValueError('empty or invalid observation/target')
    ids, labels = encode([{'role': 'user', 'content': user}], system, target, 2**31-1)
    if len(ids) != len(labels) or not any(x == -100 for x in labels) or not any(x != -100 for x in labels):
        raise ValueError('invalid masked context or target labels')
    sample = {'system': system, 'conversations': [{'from':'human', 'value':user}, {'from':'gpt', 'value':target}]}
    info = {'tokens':len(ids), 'input_ids_sha256':token_hash(ids), 'labels_sha256':token_hash(labels),
            'truncated':False, 'original_loss_flag':'unknown', 'training_ready':False}
    return sample, info


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input-dir', type=Path, required=True)
    ap.add_argument('--split-dir', type=Path, required=True)
    ap.add_argument('--output-dir', type=Path, required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--local-only', action='store_true')
    args = ap.parse_args()
    if args.output_dir.exists(): raise ValueError('output directory already exists')
    split_report = json.loads((args.split_dir/'report.json').read_text(encoding='utf-8'))
    source_manifest = args.split_dir/'manifest.jsonl'
    if sha256(source_manifest) != split_report['manifest_sha256']: raise ValueError('split manifest hash mismatch')
    reference = {}
    with source_manifest.open(encoding='utf-8') as f:
        for line in f:
            meta = json.loads(line)
            if meta['id'] in reference: raise ValueError('duplicate split manifest ID')
            reference[meta['id']] = meta
    encode = factory_encoder(args.model, REVISION, args.local_only)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'model':args.model, 'model_revision':REVISION, 'template':'qwen3_5_nothink',
              'llamafactory_version':'0.9.5', 'truncation':False,
              'target_policy':'preserve each published assistant target; original loss flag unknown',
              'training_ready':False, 'source_manifest_sha256':sha256(source_manifest),
              'input_sha256':{}, 'output_sha256':{}, 'counts':{}}
    seen = set()
    with (args.output_dir/'manifest.jsonl').open('x',encoding='utf-8') as manifest, \
         (args.output_dir/'excluded.jsonl').open('x',encoding='utf-8') as excluded:
        for part in PARTS:
            source = args.input_dir/f'{part}.jsonl'
            report['input_sha256'][source.name] = sha256(source)
            counts, lengths, warnings = Counter(), [], Counter()
            with source.open('rb') as inp, (args.output_dir/f'{part}.jsonl').open('x',encoding='utf-8') as out:
                for source_line, raw in enumerate(inp, 1):
                    row = json.loads(raw); ident = row['id']
                    if ident in seen or ident not in reference: raise ValueError(f'duplicate or unknown ID: {ident}')
                    seen.add(ident); meta = reference[ident]
                    check_metadata(ident, part, hashlib.sha256(raw).hexdigest(), meta)
                    counts['source_decisions'] += 1
                    try:
                        sample, info = adapt_decision(row, encode)
                    except (ValueError, TypeError, KeyError) as exc:
                        counts['excluded'] += 1
                        excluded.write(json.dumps({'id':ident,'split':part,'source_line':source_line,'reason':str(exc)},ensure_ascii=False)+'\n')
                        continue
                    counts['samples'] += 1; lengths.append(info['tokens'])
                    for reason in meta.get('audit_reasons', []): warnings[reason] += 1
                    out.write(json.dumps(sample, ensure_ascii=False)+'\n')
                    manifest.write(json.dumps({'id':ident,'split':part,'sample_index':counts['samples']-1,
                        'source_line':meta['source_line'], 'split_source_line':source_line,
                        'source_line_sha256':meta['source_line_sha256'], 'trajectory_id':meta['trajectory_id'],
                        'website':meta.get('website'), 'task_sha256':meta.get('task_sha256'),
                        'audit_reasons':meta.get('audit_reasons', []), **info},ensure_ascii=False)+'\n')
            counts['max_tokens'] = max(lengths, default=0)
            counts['min_tokens'] = min(lengths, default=0)
            counts['over_budget'] = {str(n):sum(x>n for x in lengths) for n in BUDGETS}
            counts['audit_reasons'] = dict(warnings)
            report['counts'][part] = dict(counts)
    (args.output_dir/'dataset_info.json').write_text(json.dumps(registry_for(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for name in ('train.jsonl','validation.jsonl','test.jsonl','manifest.jsonl','excluded.jsonl','dataset_info.json'):
        report['output_sha256'][name] = sha256(args.output_dir/name)
    (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__ == '__main__': main()
