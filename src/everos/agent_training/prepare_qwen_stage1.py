"""Conservative Qwen stage-1 research candidates from pinned source messages.

Only general training trajectories are read. Not a finished training dataset:
SWE and web are held back until their original tool interfaces are verified.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path


def build_candidates(dataset, conversations):
    history, candidates, reasons = [], [], Counter()
    for index, item in enumerate(conversations):
        role = item.get('from') if dataset == 'agentinstruct' else item.get('role')
        text = item.get('value') if dataset == 'agentinstruct' else item.get('content')
        if not isinstance(text, str) or not text.strip():
            reasons['invalid_message'] += 1
            return [], reasons
        role = {'human':'user', 'gpt':'assistant'}.get(role, role)
        if role not in ('system', 'user', 'assistant'):
            reasons['unknown_role'] += 1
            return [], reasons
        if role == 'assistant':
            loss = item.get('loss') if dataset == 'agentinstruct' else True
            if loss is True:
                if not any(m['role'] == 'user' for m in history):
                    reasons['missing_user_context'] += 1
                else:
                    candidates.append({'messages': [dict(m) for m in history], 'target': text, 'source_message_index': index})
            elif loss is not False:
                reasons['unknown_source_loss'] += 1
        history.append({'role': role, 'content': text})
    return candidates, reasons


def labels_for_decision(tokenizer, messages, target, max_tokens, truncate=False):
    prefix = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    full = tokenizer.apply_chat_template(messages + [{'role':'assistant','content':target}], tokenize=True, add_generation_prompt=False)
    if hasattr(prefix, 'keys'):
        prefix = prefix['input_ids']
        full = full['input_ids']
    if full[:len(prefix)] != prefix or len(full) <= len(prefix):
        raise ValueError('qwen_chat_template_prefix_mismatch')
    original_tokens = len(full)
    removed = max(0, len(full) - max_tokens)
    if removed and (not truncate or removed >= len(prefix)):
        return None
    labels = [-100] * len(prefix) + full[len(prefix):]
    full, labels = full[removed:], labels[removed:]
    return {'input_ids': full, 'attention_mask': [1] * len(full), 'labels': labels, 'prefix_tokens': len(prefix) - removed, 'tokens': len(full), 'original_tokens': original_tokens, 'truncated_tokens': removed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('.memo-vault/agent-training/datasets/paper_adp_v2'))
    parser.add_argument('--raw-dir', type=Path, default=Path('.memo-vault/agent-training/datasets/raw'))
    parser.add_argument('--output-dir', type=Path, default=Path('.memo-vault/agent-training/datasets/qwen_stage1_candidate_v1'))
    parser.add_argument('--tokenizer', default='Qwen/Qwen2.5-0.5B-Instruct')
    parser.add_argument('--revision', default='7ae557604adf67be50417f59c2c2f167def9a775')
    parser.add_argument('--max-tokens', type=int, default=2048)
    parser.add_argument('--truncate', action='store_true', help='Left-truncate context while retaining the complete target; default rejects overlong samples')
    args = parser.parse_args()
    if args.max_tokens <= 0:
        parser.error('--max-tokens must be positive')
    if args.output_dir.exists():
        raise FileExistsError(f'output exists: {args.output_dir}; select another --output-dir')
    import pyarrow.parquet as pq
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, revision=args.revision, local_files_only=True, trust_remote_code=False)
    args.output_dir.mkdir(parents=True)
    report = {'version':'qwen-stage1-candidate-v1', 'max_tokens':args.max_tokens, 'model':args.tokenizer, 'revision':args.revision,
              'scope':'train/general only', 'status':'research candidates; manual review and interface compatibility pending',
              'truncate':args.truncate, 'policy':'source conversation; only final assistant target gets loss; SWE and Mind2Web held back',
              'datasets':{}, 'source_files':{}}
    with (args.output_dir / 'samples.jsonl').open('w') as out, (args.output_dir / 'decisions.jsonl').open('w') as decisions:
        for dataset in ('agentinstruct', 'codeactinstruct', 'swe_smith', 'mind2web'):
            path = args.data_dir / 'train/general' / f'{dataset}.jsonl'
            digest = hashlib.sha256()
            summary = Counter()
            cached, verified = {}, {}
            with path.open('rb') as f:
                for line in f:
                    digest.update(line)
                    record = json.loads(line)
                    source = json.loads(record['details']['source_json'])
                    summary['trajectories'] += 1
                    common = {'dataset':dataset, 'trajectory_id':record['id'], 'source':source}
                    if dataset in ('swe_smith', 'mind2web'):
                        reason = 'tool_schema_unverified' if dataset == 'swe_smith' else 'web_interface_and_context_unresolved'
                        summary['trajectories_held_back'] += 1
                        decisions.write(json.dumps({**common, 'status':'held_back', 'reason':reason}, ensure_ascii=False) + '\n')
                        continue
                    relative = source['relative_file']
                    raw_path = args.raw_dir / relative
                    if relative not in verified:
                        h = hashlib.sha256()
                        with raw_path.open('rb') as raw:
                            for block in iter(lambda:raw.read(1024 * 1024), b''):
                                h.update(block)
                        verified[relative] = h.hexdigest()
                        cached[relative] = pq.read_table(raw_path).to_pylist()
                    if verified[relative] != record['details']['source_file_sha256']:
                        raise ValueError(f'raw sha256 mismatch: {relative}')
                    raw_record = cached[relative][source['row_index']]
                    if str(raw_record['id']) != record['id']:
                        raise ValueError(f'raw id mismatch: {record["id"]}')
                    candidates, reasons = build_candidates(dataset, raw_record['conversations'])
                    summary.update(reasons)
                    summary['candidate_decisions'] += len(candidates)
                    accepted = 0
                    for candidate in candidates:
                        info = {**common, 'source_message_index':candidate['source_message_index']}
                        try:
                            tokenized = labels_for_decision(tokenizer, candidate['messages'], candidate['target'], args.max_tokens, truncate=args.truncate)
                        except ValueError as exc:
                            if str(exc) != 'qwen_chat_template_prefix_mismatch':
                                raise
                            summary['decisions_template_boundary_mismatch'] += 1
                            decisions.write(json.dumps({**info, 'status':'held_back', 'reason':'qwen_chat_template_prefix_mismatch'}, ensure_ascii=False) + '\n')
                            continue
                        if tokenized is None:
                            summary['decisions_over_limit'] += 1
                            reason = 'target_exceeds_limit_or_no_context' if args.truncate else 'over_token_limit_no_truncation'
                            decisions.write(json.dumps({**info, 'status':'held_back', 'reason':reason}, ensure_ascii=False) + '\n')
                            continue
                        accepted += 1
                        summary['samples'] += 1
                        summary['truncated_samples'] += bool(tokenized['truncated_tokens'])
                        payload = {**info, 'messages':candidate['messages'], 'target':candidate['target'], **tokenized,
                                   'review_status':'unreviewed', 'tool_protocol':'source_native'}
                        out.write(json.dumps(payload, ensure_ascii=False) + '\n')
                    summary['trajectories_with_samples'] += bool(accepted)
                    if not candidates:
                        decisions.write(json.dumps({**common, 'status':'held_back', 'reason':'no_eligible_assistant_targets'}, ensure_ascii=False) + '\n')
                    elif not accepted:
                        summary['trajectories_without_fitting_samples'] += 1
            report['source_files'][dataset] = {'adp_sha256':digest.hexdigest(), 'raw_sha256':verified}
            report['datasets'][dataset] = dict(summary)
    (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['datasets'], ensure_ascii=False))


if __name__ == '__main__':
    main()
