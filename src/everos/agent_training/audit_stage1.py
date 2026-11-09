"""Reproducible, read-only audit of the selected paper ADP v2 trajectories.

This reports evidence and risks, not training approval or executable tools.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

DATASETS = ('agentinstruct', 'codeactinstruct', 'swe_smith', 'mind2web')
PARTITIONS = ('train/general', 'train/domain', 'validation/validation', 'test/test')
ACTIONS = {'api_action', 'code_action'}
OBSERVATIONS = {'text_observation', 'web_observation'}
TOOL_EVIDENCE = {
    'agentinstruct': 'source_prompt_bash_finish_answer',
    'codeactinstruct': 'source_system_python_execute',
    'swe_smith': 'upstream_declaration_version_requires_verification',
    'mind2web': 'upstream_declaration_requires_parameter_mapping',
}


def audit_record(record):
    details = record.get('details', {})
    source = json.loads(details.get('source_json', '{}'))
    dataset = source.get('dataset', '')
    content = record.get('content', [])
    system = json.loads(details.get('system_messages_json', '[]'))
    system_text = '\n'.join(system)
    flags = json.loads(details.get('source_loss_flags_json', '[]'))
    actions = [(i, c) for i, c in enumerate(content) if c.get('class_') in ACTIONS]
    unobserved = sum(1 for i, c in actions if i + 1 >= len(content) or content[i + 1].get('class_') not in OBSERVATIONS)
    counts = Counter(c.get('class_', 'missing') for c in content)
    names = Counter((c.get('function', '?') if c.get('class_') == 'api_action' else 'code:' + str(c.get('language', '?'))) for _, c in actions)
    text_parts = [system_text]
    for c in content:
        text_parts.append(json.dumps(c, ensure_ascii=False, sort_keys=True))
    return {
        'id': record.get('id'), 'dataset': dataset, 'group': source.get('group'),
        'task_sha256': source.get('task_sha256'), 'source_file_sha256': details.get('source_file_sha256'),
        'source_file': source.get('relative_file'), 'source_row': source.get('row_index'),
        'classes': dict(counts), 'actions': len(actions), 'unobserved_actions': unobserved,
        'action_names': dict(names), 'has_user': any(c.get('class_') == 'text_observation' and c.get('source') == 'user' and bool(c.get('content')) for c in content),
        'has_system': bool(system_text), 'source_loss_false_count': flags.count(False),
        'codeact_explicit_tool_marker': 'Tool function available' in system_text,
        'codeact_wikipedia_marker': 'wikipedia_search' in system_text,
        'codeact_household_marker': any(x in system_text for x in ('take_from(', 'open_receptacle(', 'toggle(')),
        'tool_evidence': TOOL_EVIDENCE.get(dataset, 'unknown'),
        'chars_approx': sum(len(x) for x in text_parts),
        '_text_for_token_count': '\n'.join(text_parts),
    }


def cross_split_conflicts(records, field):
    groups = defaultdict(set)
    for row in records:
        if row.get(field) is not None:
            groups[(row['dataset'], row[field])].add(row['partition'])
    return [{'dataset': d, 'value': v, 'partitions': sorted(parts)} for (d, v), parts in sorted(groups.items()) if len(parts) > 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('.memo-vault/agent-training/datasets/paper_adp_v2'))
    parser.add_argument('--output-dir', type=Path, default=Path('.memo-vault/agent-training/audits/stage1_v1'))
    parser.add_argument('--tokenizer', default=None, help='Local Hugging Face model/cache path; offline only')
    args = parser.parse_args()
    tokenizer = None
    if args.tokenizer:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True, trust_remote_code=False)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows, file_hashes = [], {}
    for partition in PARTITIONS:
        for dataset in DATASETS:
            path = args.data_dir / partition / f'{dataset}.jsonl'
            digest = hashlib.sha256()
            with path.open('rb') as f:
                for line_number, line in enumerate(f, 1):
                    digest.update(line)
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    row = audit_record(record)
                    if row['dataset'] != dataset:
                        raise ValueError(f'{path}:{line_number}: source dataset mismatch')
                    row['partition'] = partition
                    text = row.pop('_text_for_token_count')
                    if tokenizer:
                        row['tokens_approx'] = len(tokenizer.encode(text, add_special_tokens=False, verbose=False))
                    rows.append(row)
            file_hashes[str(path.relative_to(args.data_dir))] = digest.hexdigest()
    summaries = {}
    for partition in PARTITIONS:
        summaries[partition] = {}
        for dataset in DATASETS:
            group = [r for r in rows if r['partition'] == partition and r['dataset'] == dataset]
            sizes = sorted(r['tokens_approx'] for r in group) if tokenizer else []
            summaries[partition][dataset] = {
                'trajectories': len(group), 'missing_user': sum(not r['has_user'] for r in group),
                'actions': sum(r['actions'] for r in group), 'actions_without_immediate_observation': sum(r['unobserved_actions'] for r in group),
                'trajectories_with_unobserved_actions': sum(r['unobserved_actions'] > 0 for r in group),
                'action_names': dict(sum((Counter(r['action_names']) for r in group), Counter())),
                'system_messages': sum(r['has_system'] for r in group),
                'source_loss_false_items': sum(r['source_loss_false_count'] for r in group),
                'codeact_explicit_tool_marker': sum(r['codeact_explicit_tool_marker'] for r in group),
                'codeact_wikipedia_marker': sum(r['codeact_wikipedia_marker'] for r in group),
                'codeact_household_marker': sum(r['codeact_household_marker'] for r in group),
                'tokens_approx': ({'min': sizes[0], 'median': sizes[len(sizes)//2], 'p90': sizes[min(len(sizes)-1, int(len(sizes)*0.9))], 'max': sizes[-1], 'over_512': sum(x > 512 for x in sizes), 'over_2048': sum(x > 2048 for x in sizes)} if sizes else None),
            }
    report = {'version': 'stage1-audit-v1', 'scope': 'All four partitions; training candidates only train/general',
              'token_method': 'Qwen tokenizer on JSON serialization of ADP items plus retained system text; not chat template or final training length',
              'tokenizer': args.tokenizer, 'input_sha256': file_hashes, 'summary': summaries,
              'group_conflicts': cross_split_conflicts(rows, 'group'), 'task_hash_conflicts': cross_split_conflicts(rows, 'task_sha256'),
              'caveats': ['Tool evidence is source-level, not proof that each action is executable or schema-compatible.', 'Immediate next observation is a heuristic; terminal submit and final webpage action may legitimately lack one.', 'Exact hashes and source groups do not detect semantic duplicates, shared demonstrations, or cross-source leakage.', 'No labels, suitability approval, executable tool implementation, or training is produced.']}
    (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    with (args.output_dir / 'records.jsonl').open('w') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    print(json.dumps({'records': len(rows), 'output_dir': str(args.output_dir), 'group_conflicts': len(report['group_conflicts']), 'task_hash_conflicts': len(report['task_hash_conflicts'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
