"""Audit actual LLaMA-Factory cutoff effects without training or modifying data."""
import argparse
from collections import Counter
import json
from pathlib import Path

from .openhands_inventory import sha256_file
from .swe_llamafactory import factory_encoder, token_hash

REVISION = '2fc06364715b967f1860aea9cf38778875588b17'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared-dir', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--cutoff-len', type=int, required=True)
    parser.add_argument('--max-samples', type=int, default=32, help='maximum decisions per split')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.cutoff_len < 2 or args.max_samples < 1 or args.output_dir.exists():
        raise ValueError('invalid cutoff/sample count or existing output directory')
    report = json.loads((args.prepared_dir/'report.json').read_text(encoding='utf-8'))
    if report.get('model_revision') != REVISION or report.get('selected_source') != 'codeactinstruct':
        raise ValueError('unexpected source data or model revision')
    for name in ('train.jsonl', 'validation.jsonl', 'manifest.jsonl'):
        if sha256_file(args.prepared_dir/name) != report['output_sha256'][name]:
            raise ValueError(f'prepared file hash mismatch: {name}')
    encode = factory_encoder(args.model, REVISION, True)
    manifest = {'train': [], 'validation': []}
    with (args.prepared_dir/'manifest.jsonl').open(encoding='utf-8') as f:
        for line in f:
            m = json.loads(line)
            if m['split'] in manifest and len(manifest[m['split']]) < args.max_samples:
                manifest[m['split']].append(m)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    summary = {'cutoff_len': args.cutoff_len, 'max_samples_per_split': args.max_samples,
               'prepared_report_sha256': sha256_file(args.prepared_dir/'report.json'),
               'model': args.model, 'model_revision': REVISION,
               'note': 'Target tokens are compared exactly; context/system semantic preservation is not proved.', 'splits': {}}
    with (args.output_dir/'decisions.jsonl').open('x', encoding='utf-8') as out:
        for split in ('train', 'validation'):
            counts = Counter()
            with (args.prepared_dir/f'{split}.jsonl').open(encoding='utf-8') as source:
                for index, meta in enumerate(manifest[split]):
                    raw = source.readline()
                    if not raw or meta['sample_index'] != index:
                        raise ValueError(f'data/manifest mismatch: {split}/{index}')
                    row = json.loads(raw)
                    history = [{'role': 'user' if m['from'] == 'human' else 'assistant', 'content': m['value']} for m in row['conversations'][:-1]]
                    target = row['conversations'][-1]['value']
                    ids, labels = encode(history, row['system'], target, 2**31 - 1)
                    if token_hash(ids) != meta['input_ids_sha256'] or token_hash(labels) != meta['labels_sha256']:
                        raise ValueError(f'full token/label mismatch: {split}/{index}')
                    cut_ids, cut_labels = encode(history, row['system'], target, args.cutoff_len)
                    original_target = [x for x in labels if x != -100]
                    cut_target = [x for x in cut_labels if x != -100]
                    counts['checked'] += 1
                    counts['over_cutoff'] += len(ids) > args.cutoff_len
                    counts['target_changed'] += original_target != cut_target
                    counts['target_empty'] += not bool(cut_target)
                    out.write(json.dumps({'split': split, 'sample_index': index, 'trajectory_id': meta['trajectory_id'],
                        'original_tokens': len(ids), 'after_tokens': len(cut_ids),
                        'original_target_tokens': len(original_target), 'after_target_tokens': len(cut_target),
                        'target_changed': original_target != cut_target, 'target_empty': not bool(cut_target)}, ensure_ascii=False)+'\n')
            summary['splits'][split] = dict(counts)
    (args.output_dir/'report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
