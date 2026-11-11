"""Draft untruncated OpenHands decision candidates; never starts training."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from .openhands_inventory import sha256_file
from .swe_llamafactory import factory_encoder, token_hash

REVISION = '2fc06364715b967f1860aea9cf38778875588b17'
PARTS = ('train', 'validation', 'test')
BUDGETS = (1024, 2048, 4096, 8192, 16384, 32768)


def decisions(item):
    system = item.get('system')
    if not isinstance(system, str) or not system.strip():
        raise ValueError('missing_system')
    history = []
    for index, msg in enumerate(item['conversations']):
        role = {'human': 'user', 'gpt': 'assistant'}.get(msg.get('from'))
        content = msg.get('value')
        if role is None or not isinstance(content, str):
            raise ValueError(f'invalid_message_at_{index}')
        expected = 'user' if len(history) % 2 == 0 else 'assistant'
        if role != expected:
            raise ValueError(f'non_alternating_role_at_{index}')
        if role == 'assistant':
            yield index, system, list(history), content
        history.append({'role': role, 'content': content})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input-dir', type=Path, required=True)
    ap.add_argument('--output-dir', type=Path, required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--local-only', action='store_true')
    ap.add_argument('--source', choices=('codeactinstruct',), default='codeactinstruct')
    args = ap.parse_args()
    if args.output_dir.exists():
        raise SystemExit(f'output directory already exists: {args.output_dir}')
    encode = factory_encoder(args.model, REVISION, args.local_only)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'model': args.model, 'model_revision': REVISION, 'template': 'qwen3_5_nothink',
              'llamafactory_version': '0.9.5', 'truncation': False,
              'target_policy': 'each assistant message is supervised as its own decision; source loss flags are not restored',
              'selected_source': args.source,
              'training_ready': False, 'counts': {}, 'input_sha256': {}, 'output_sha256': {}}
    registry = {}
    with (args.output_dir / 'manifest.jsonl').open('x', encoding='utf-8') as manifest, \
         (args.output_dir / 'excluded.jsonl').open('x', encoding='utf-8') as excluded:
        for part in PARTS:
            source = args.input_dir / f'{part}.jsonl'
            reference = args.input_dir / f'{part}.manifest.jsonl'
            report['input_sha256'][source.name] = sha256_file(source)
            report['input_sha256'][reference.name] = sha256_file(reference)
            counts = Counter()
            lengths = []
            if part != 'test':
                registry[f'openhands_{part}'] = {'file_name': f'{part}.jsonl', 'formatting': 'sharegpt',
                                                 'columns': {'messages': 'conversations', 'system': 'system'}}
            with source.open(encoding='utf-8') as inp, reference.open(encoding='utf-8') as refs, \
                 (args.output_dir / f'{part}.jsonl').open('x', encoding='utf-8') as out:
                for source_line, pair in enumerate(zip(inp, refs, strict=True), 1):
                    raw, ref = pair
                    row, meta = json.loads(raw), json.loads(ref)
                    if (row['id'] != meta['id'] or meta['assigned_partition'] != part or
                            hashlib.sha256(raw.encode()).hexdigest() != meta['raw_line_sha256']):
                        raise ValueError(f'input/manifest mismatch: {part}/{source_line}')
                    counts['trajectories'] += 1
                    if meta['source'] != args.source:
                        counts['excluded_source_trajectories'] += 1
                        excluded.write(json.dumps({'split': part, 'id': row.get('id'), 'source': meta['source'], 'source_line': source_line, 'reason': 'source_not_selected'}, ensure_ascii=False) + '\n')
                        continue
                    try:
                        candidates = list(decisions(row))
                    except (ValueError, KeyError, TypeError) as exc:
                        counts['excluded_trajectories'] += 1
                        excluded.write(json.dumps({'split': part, 'id': row.get('id'), 'source_line': source_line, 'reason': str(exc)}, ensure_ascii=False) + '\n')
                        continue
                    for event_index, system, history, target in candidates:
                        counts['decisions'] += 1
                        if not target.strip():
                            counts['excluded_decisions'] += 1
                            excluded.write(json.dumps({'split': part, 'id': row['id'], 'source_line': source_line, 'event_index': event_index, 'reason': 'empty_assistant_target'}, ensure_ascii=False) + '\n')
                            continue
                        try:
                            ids, labels = encode(history, system, target, 2**31 - 1)
                            if len(ids) != len(labels) or not any(x == -100 for x in labels) or not any(x != -100 for x in labels):
                                raise ValueError('invalid_context_or_target_labels')
                        except (ValueError, TypeError) as exc:
                            counts['excluded_decisions'] += 1
                            excluded.write(json.dumps({'split': part, 'id': row['id'], 'source_line': source_line, 'event_index': event_index, 'reason': str(exc)}, ensure_ascii=False) + '\n')
                            continue
                        conversations = [{'from': 'human' if m['role'] == 'user' else 'gpt', 'value': m['content']} for m in history]
                        conversations.append({'from': 'gpt', 'value': target})
                        out.write(json.dumps({'system': system, 'conversations': conversations}, ensure_ascii=False) + '\n')
                        counts['samples'] += 1
                        lengths.append(len(ids))
                        manifest.write(json.dumps({'split': part, 'sample_index': counts['samples'] - 1,
                            'source': meta['source'], 'trajectory_id': row['id'], 'source_line': source_line,
                            'event_index': event_index, 'task_group': meta['task_group'], 'tokens': len(ids),
                            'input_ids_sha256': token_hash(ids), 'labels_sha256': token_hash(labels),
                            'truncated': False, 'original_loss_flag': 'unknown', 'training_ready': False}, ensure_ascii=False) + '\n')
            report['counts'][part] = dict(counts)
            report['counts'][part]['max_tokens'] = max(lengths, default=0)
            report['counts'][part]['over_budget'] = {str(b): sum(n > b for n in lengths) for b in BUDGETS}
    (args.output_dir / 'dataset_info.json').write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name in ('train.jsonl', 'validation.jsonl', 'test.jsonl', 'manifest.jsonl', 'excluded.jsonl', 'dataset_info.json'):
        report['output_sha256'][name] = sha256_file(args.output_dir / name)
    (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
