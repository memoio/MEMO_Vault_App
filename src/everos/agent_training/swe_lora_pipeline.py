"""Prepare SWE-Agent XML decisions and train a Qwen3.5 QLoRA adapter.

Offline, single-GPU research pipeline; no container or tools are executed.
Inputs for prepare are ADP upstream SWE-Agent converter JSONL files, one record
per trajectory with id, system, and conversations (human/gpt).
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time

ACTION = re.compile(r'<function=(bash|str_replace_editor|submit)>\s*\n')
ADP_COMMIT = 'ab1180306a3d4d27e9195448c90f030607eba981'


def convert_record(row, converter):
    if 'available_apis' in row.get('details', {}):
        raise ValueError('source-provided available_apis is not allowed')
    converted = converter(json.dumps(row, ensure_ascii=False), '', {})
    if converted is None or converted.get('id') != row.get('id'):
        raise ValueError('upstream conversion failed or changed id')
    source = row.get('details', {}).get('source_json')
    if source:
        converted['source'] = json.loads(source)
    return converted


def convert(args):
    commit = subprocess.check_output(['git', '-C', str(args.upstream_dir), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != ADP_COMMIT:
        raise ValueError(f'expected ADP commit {ADP_COMMIT}, got {commit}')
    # This repository contains executable code; only use the known fixed commit.
    os.environ['MY_DATASET'] = 'swe-smith'
    sys.path.insert(0, str(args.upstream_dir.resolve()))
    from agents.sweagent.std_to_sft import process_row
    partitions = ('train/general', 'train/domain', 'validation/validation', 'test/test')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'upstream_commit': commit, 'partitions': {}, 'source_sha256': {}}
    with (args.output_dir / 'excluded.jsonl').open('w', encoding='utf-8') as excluded:
        for partition in partitions:
            path = args.adp_dir / partition / 'swe_smith.jsonl'
            report['source_sha256'][partition] = sha256(path)
            counts = Counter()
            with path.open(encoding='utf-8') as src, (args.output_dir / (partition.replace('/', '_') + '.jsonl')).open('w', encoding='utf-8') as out:
                for line in src:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    counts['input'] += 1
                    try:
                        result = convert_record(row, process_row)
                    except (ValueError, TypeError, KeyError) as exc:
                        counts['excluded'] += 1
                        excluded.write(json.dumps({'partition': partition, 'id': row.get('id'), 'reason': str(exc)}, ensure_ascii=False) + '\n')
                        continue
                    out.write(json.dumps(result, ensure_ascii=False) + '\n')
                    counts['converted'] += 1
            report['partitions'][partition] = dict(counts)
    (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def read_converted(paths):
    seen = set()
    for path in paths:
        with path.open(encoding='utf-8') as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                ident = row.get('id')
                if not isinstance(ident, str) or not ident:
                    raise ValueError(f'{path}:{line_no}: missing trajectory id')
                if ident in seen:
                    raise ValueError(f'duplicate trajectory id: {ident}')
                seen.add(ident)
                yield row


def build_decisions(row, split):
    if not isinstance(row.get('system'), str) or not isinstance(row.get('conversations'), list):
        raise ValueError(f'invalid converted trajectory: {row.get("id")}')
    history = [{'role': 'system', 'content': row['system']}]
    for i, item in enumerate(row['conversations']):
        role = {'human': 'user', 'gpt': 'assistant'}.get(item.get('from'))
        value = item.get('value')
        if role is None or not isinstance(value, str):
            raise ValueError(f'invalid conversation item {row["id"]}:{i}')
        if role == 'assistant' and ACTION.search(value):
            if not any(m['role'] == 'user' for m in history):
                raise ValueError(f'missing user context {row["id"]}:{i}')
            yield {'trajectory_id': row['id'], 'event_index': i, 'split': split,
                   'action': ACTION.search(value).group(1),
                   'source': row.get('source'), 'messages': [dict(m) for m in history], 'target': value}
        history.append({'role': role, 'content': value})


def encode_decision(tokenizer, decision, max_tokens, truncate):
    messages, target = decision['messages'], decision['target']
    options = {'tokenize': True, 'enable_thinking': False}
    prefix = tokenizer.apply_chat_template(messages, add_generation_prompt=True, **options)
    full = tokenizer.apply_chat_template(messages + [{'role': 'assistant', 'content': target}],
                                         add_generation_prompt=False, **options)
    if hasattr(prefix, 'keys'):
        prefix, full = prefix['input_ids'], full['input_ids']
    if full[:len(prefix)] != prefix or len(full) <= len(prefix):
        raise ValueError('chat_template_prefix_mismatch')
    removed = max(0, len(full) - max_tokens)
    if removed and (not truncate or removed >= len(prefix)):
        return None
    labels = [-100] * len(prefix) + full[len(prefix):]
    return {'input_ids': full[removed:], 'attention_mask': [1] * (len(full) - removed),
            'labels': labels[removed:], 'original_tokens': len(full),
            'truncated_tokens': removed, 'prefix_tokens': len(prefix) - removed}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(args):
    from transformers import AutoTokenizer
    if args.max_tokens < 2:
        raise ValueError('max-tokens must be at least 2')
    tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision,
                                               local_files_only=args.local_only, trust_remote_code=False)
    # Fail before creating output if a source trajectory appears in both splits.
    train = list(read_converted(args.train_input))
    validation = list(read_converted(args.validation_input)) if args.validation_input else []
    overlap = {x['id'] for x in train} & {x['id'] for x in validation}
    if overlap:
        raise ValueError(f'train/validation trajectory overlap: {len(overlap)}')
    for field in ('group', 'task_sha256'):
        train_values = {x['source'].get(field) for x in train if isinstance(x.get('source'), dict) and x['source'].get(field)}
        val_values = {x['source'].get(field) for x in validation if isinstance(x.get('source'), dict) and x['source'].get(field)}
        if train_values & val_values:
            raise ValueError(f'train/validation source {field} overlap')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'model': args.model, 'revision': args.revision, 'max_tokens': args.max_tokens,
              'truncate': args.truncate, 'thinking': False, 'counts': {},
              'source_sha256': {str(p): sha256(p) for p in args.train_input + args.validation_input},
              'note': 'XML source decisions; validate tool semantics and task isolation before experiments'}
    with (args.output_dir / 'excluded.jsonl').open('w', encoding='utf-8') as excluded:
        for split, rows in [('train', train), ('validation', validation)]:
            counts = Counter(trajectories=len(rows))
            with (args.output_dir / f'{split}.jsonl').open('w', encoding='utf-8') as out:
                for row in rows:
                    for decision in build_decisions(row, split):
                        counts['decisions'] += 1
                        info = {k: decision[k] for k in ('trajectory_id', 'event_index', 'split', 'action', 'source')}
                        try:
                            encoded = encode_decision(tokenizer, decision, args.max_tokens, args.truncate)
                        except ValueError as exc:
                            counts['excluded_template'] += 1
                            excluded.write(json.dumps({**info, 'reason': str(exc)}, ensure_ascii=False) + '\n')
                            continue
                        if encoded is None:
                            counts['excluded_length'] += 1
                            excluded.write(json.dumps({**info, 'reason': 'length_limit_or_target_too_long'}, ensure_ascii=False) + '\n')
                            continue
                        counts['samples'] += 1
                        counts['truncated'] += bool(encoded['truncated_tokens'])
                        out.write(json.dumps({**info, **encoded}, ensure_ascii=False) + '\n')
            report['counts'][split] = dict(counts)
    (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def load_samples(path, expected_split):
    rows = []
    with path.open(encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            ids, labels, mask = row['input_ids'], row['labels'], row['attention_mask']
            if row.get('split') != expected_split or not ids or len(ids) != len(labels) or len(ids) != len(mask):
                raise ValueError(f'invalid {expected_split} sample')
            if not any(x != -100 for x in labels) or not any(x == -100 for x in labels):
                raise ValueError('sample needs masked context and an assistant target')
            rows.append(row)
    return rows


def train(args):
    import torch
    import transformers
    import peft
    import bitsandbytes
    import accelerate
    from transformers import AutoTokenizer, AutoModelForImageTextToText, BitsAndBytesConfig
    from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
    if args.epochs < 1 or args.gradient_accumulation < 1 or args.lr <= 0 or args.max_steps < 0 or args.lora_r < 1 or args.lora_alpha < 1:
        raise ValueError('invalid training parameters')
    rows = load_samples(args.train_data, 'train')
    if not rows:
        raise ValueError('empty training dataset')
    val = load_samples(args.validation_data, 'validation') if args.validation_data else []
    if {r['trajectory_id'] for r in rows} & {r['trajectory_id'] for r in val}:
        raise ValueError('train/validation trajectory overlap')
    data_report_path = args.train_data.parent / 'report.json'
    if not data_report_path.exists():
        raise ValueError('prepared data report.json is required')
    data_report = json.loads(data_report_path.read_text())
    if data_report.get('model') != args.model or data_report.get('revision') != args.revision:
        raise ValueError('model/revision must match prepared data report')
    if args.validation_data and args.validation_data.parent != args.train_data.parent:
        raise ValueError('validation data must come from the same prepared dataset')
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA GPU required')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    torch.cuda.reset_peak_memory_stats()
    free, total = torch.cuda.mem_get_info()
    result = {'success': False, 'model': args.model, 'revision': args.revision, 'seed': args.seed,
              'train_sha256': sha256(args.train_data), 'validation_sha256': sha256(args.validation_data) if args.validation_data else None,
              'dataset_report_sha256': sha256(data_report_path), 'train_samples': len(rows), 'validation_samples': len(val),
              'epochs': args.epochs, 'gradient_accumulation': args.gradient_accumulation, 'learning_rate': args.lr,
              'max_steps': args.max_steps, 'base_setup': args.base_setup, 'lora_r': args.lora_r,
              'lora_alpha': args.lora_alpha, 'torch': torch.__version__, 'cuda': torch.version.cuda,
              'transformers': transformers.__version__, 'peft': peft.__version__, 'bitsandbytes': bitsandbytes.__version__,
              'accelerate': accelerate.__version__, 'gpu': torch.cuda.get_device_name(0),
              'gpu_free_before_mib': round(free / 2**20), 'gpu_total_mib': round(total / 2**20)}
    report_path = args.output_dir / 'run.json'
    def save_result():
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    save_result()
    start = time.monotonic()
    try:
        tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision, local_files_only=args.local_only, trust_remote_code=False)
        quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4', bnb_4bit_compute_dtype=torch.float16, bnb_4bit_use_double_quant=True)
        def load_base():
            return AutoModelForImageTextToText.from_pretrained(args.model, revision=args.revision, local_files_only=args.local_only,
                trust_remote_code=False, dtype=torch.float16, quantization_config=quant, device_map={'': 0}, attn_implementation='eager')
        model = load_base()
        model.config.use_cache = False
        if args.base_setup == 'manual-fp16':
            for param in model.parameters():
                param.requires_grad_(False)
            model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
        else:
            model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True, gradient_checkpointing_kwargs={'use_reentrant': False})
        config = LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=0.0, bias='none', task_type='CAUSAL_LM',
                            target_modules=r'.*language_model\.layers\.\d+\.self_attn\.(q_proj|v_proj)')
        model = get_peft_model(model, config)
        params = [(n, p) for n, p in model.named_parameters() if p.requires_grad]
        if not params or any('lora_' not in n for n, _ in params):
            raise RuntimeError('unexpected trainable parameters')
        result['trainable_parameters'] = sum(p.numel() for _, p in params)
        result['trainable_parameter_names'] = [n for n, _ in params]
        initial = {n: p.detach().cpu().clone() for n, p in params}
        optimizer = torch.optim.AdamW((p for _, p in params), lr=args.lr)
        def tensors(row):
            return {k: torch.tensor([row[k]], dtype=torch.long, device='cuda') for k in ('input_ids', 'attention_mask', 'labels')}
        model.train()
        optimizer_steps = 0
        micro_steps = 0
        losses = []
        train_start = time.monotonic()
        for epoch in range(args.epochs):
            order = list(range(len(rows)))
            rng.shuffle(order)
            for offset in range(0, len(order), args.gradient_accumulation):
                batch = order[offset:offset + args.gradient_accumulation]
                optimizer.zero_grad(set_to_none=True)
                for index in batch:
                    output = model(**tensors(rows[index]), use_cache=False)
                    loss = output.loss
                    if not torch.isfinite(loss):
                        raise RuntimeError('non-finite loss')
                    (loss / len(batch)).backward()
                    losses.append(float(loss.detach().item()))
                    micro_steps += 1
                grads = [p.grad for _, p in params if p.grad is not None]
                if not grads or not all(torch.isfinite(g).all() for g in grads):
                    raise RuntimeError('missing or non-finite gradients')
                if not any(torch.count_nonzero(g).item() for g in grads):
                    raise RuntimeError('all gradients are zero')
                optimizer.step()
                optimizer_steps += 1
                if args.max_steps and optimizer_steps >= args.max_steps:
                    break
            if args.max_steps and optimizer_steps >= args.max_steps:
                break
        torch.cuda.synchronize()
        result.update(train_seconds=round(time.monotonic() - train_start, 3), micro_steps=micro_steps,
                      optimizer_steps=optimizer_steps, loss_first=losses[0], loss_last=losses[-1],
                      changed_lora_tensors=sum(not torch.equal(initial[n], p.detach().cpu()) for n, p in params),
                      peak_allocated_mib=round(torch.cuda.max_memory_allocated()/2**20, 2),
                      peak_reserved_mib=round(torch.cuda.max_memory_reserved()/2**20, 2))
        if not result['changed_lora_tensors']:
            raise RuntimeError('no adapter weights changed')
        model.eval()
        if val:
            vals = []
            with torch.inference_mode():
                for row in val:
                    loss = model(**tensors(row), use_cache=False).loss
                    if not torch.isfinite(loss):
                        raise RuntimeError('non-finite validation loss')
                    vals.append(float(loss.item()))
            result['validation_mean_loss'] = sum(vals) / len(vals)
        inputs = {k: v for k, v in tensors(rows[0]).items() if k != 'labels'}
        with torch.inference_mode():
            before = model(**inputs, use_cache=False, logits_to_keep=1).logits.detach().float().cpu()
        adapter = args.output_dir / 'adapter'
        model.save_pretrained(adapter, safe_serialization=True)
        tokenizer.save_pretrained(args.output_dir / 'tokenizer')
        del model, optimizer, params, initial, output, loss
        import gc
        gc.collect()
        torch.cuda.empty_cache()
        reloaded = PeftModel.from_pretrained(load_base(), adapter, is_trainable=False).eval()
        with torch.inference_mode():
            after = reloaded(**inputs, use_cache=False, logits_to_keep=1).logits.detach().float().cpu()
        result['reload_max_abs_logit_diff'] = float((before - after).abs().max().item())
        if not torch.allclose(before, after, atol=1e-3, rtol=1e-3):
            raise RuntimeError('adapter reload logits mismatch')
        result['success'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - start, 3)
        save_result()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    conversion = commands.add_parser('convert', help='Convert fixed ADP SWE-smith trajectories using the pinned upstream converter')
    conversion.add_argument('--adp-dir', type=Path, required=True)
    conversion.add_argument('--upstream-dir', type=Path, required=True)
    conversion.add_argument('--output-dir', type=Path, required=True)
    conversion.set_defaults(func=convert)
    prep = commands.add_parser('prepare', help='Convert XML conversations into decision-level token samples')
    prep.add_argument('--train-input', type=Path, nargs='+', required=True)
    prep.add_argument('--validation-input', type=Path, nargs='*', default=[])
    prep.add_argument('--output-dir', type=Path, required=True)
    prep.add_argument('--model', required=True)
    prep.add_argument('--revision', required=True)
    prep.add_argument('--max-tokens', type=int, default=2048)
    prep.add_argument('--truncate', action='store_true', help='Opt in to left-context truncation')
    prep.add_argument('--local-only', action='store_true')
    prep.set_defaults(func=prepare)
    fit = commands.add_parser('train', help='Train and reload a 4-bit QLoRA adapter')
    fit.add_argument('--train-data', type=Path, required=True)
    fit.add_argument('--validation-data', type=Path)
    fit.add_argument('--output-dir', type=Path, required=True)
    fit.add_argument('--model', required=True)
    fit.add_argument('--revision', required=True)
    fit.add_argument('--epochs', type=int, default=1)
    fit.add_argument('--gradient-accumulation', type=int, default=1)
    fit.add_argument('--lr', type=float, default=2e-4)
    fit.add_argument('--max-steps', type=int, default=0, help='Maximum optimizer steps; 0 means all epochs')
    fit.add_argument('--seed', type=int, default=42)
    fit.add_argument('--lora-r', type=int, default=8)
    fit.add_argument('--lora-alpha', type=int, default=16)
    fit.add_argument('--base-setup', choices=('peft', 'manual-fp16'), default='peft',
                     help='manual-fp16 is experimental and was used for the 4 GiB smoke test')
    fit.add_argument('--local-only', action='store_true')
    fit.set_defaults(func=train)
    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
