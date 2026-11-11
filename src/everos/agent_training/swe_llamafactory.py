"""Export audited SWE decisions for LLaMA-Factory 0.9.5; never runs training."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from importlib.metadata import version
import json
from pathlib import Path

from .swe_lora_pipeline import build_decisions, read_converted, sha256


def token_hash(values):
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


def check_splits(train, validation):
    if {x['id'] for x in train} & {x['id'] for x in validation}:
        raise ValueError('train/validation trajectory overlap')
    for field in ('group', 'task_sha256'):
        def values(rows):
            return {x['source'][field] for x in rows
                    if isinstance(x.get('source'), dict) and x['source'].get(field)}
        if values(train) & values(validation):
            raise ValueError(f'train/validation source {field} overlap')


def fit_decision(decision, encode, cutoff, truncate):
    """Trim only with permission; never let the trainer truncate an accepted row."""
    history = [dict(x) for x in decision['messages']]
    if not history or history[0]['role'] != 'system':
        raise ValueError('missing system message')
    system = history.pop(0)['content']
    if not history or len(history) % 2 != 1 or any(
        m['role'] != ('user' if i % 2 == 0 else 'assistant') for i, m in enumerate(history)
    ):
        raise ValueError('context must alternate user and assistant and end in user')
    original_ids, original_labels = encode(history, system, decision['target'], 2**31 - 1)
    target = [x for x in original_labels if x != -100]
    if not target or not any(x == -100 for x in original_labels):
        raise ValueError('missing masked context or target')
    removed_turns = 0
    removed_user_chars = 0
    while True:
        full_ids, full_labels = encode(history, system, decision['target'], 2**31 - 1)
        if [x for x in full_labels if x != -100] != target:
            raise ValueError('target changed while truncating history')
        if len(full_ids) <= cutoff:
            actual_ids, actual_labels = encode(history, system, decision['target'], cutoff)
            if actual_ids != full_ids or actual_labels != full_labels:
                raise ValueError('LLaMA-Factory would truncate or change this sample')
            return {'system': system, 'messages': history, 'input_ids': full_ids,
                    'labels': full_labels, 'original_tokens': len(original_ids),
                    'removed_tokens': len(original_ids) - len(full_ids), 'removed_turns': removed_turns,
                    'removed_user_chars': removed_user_chars}
        if not truncate:
            return None
        if len(history) > 1:
            history = history[2:]
            removed_turns += 1
            continue
        content = history[-1]['content']
        low, high, best = 1, len(content) - 1, 0
        while low <= high:
            keep = (low + high) // 2
            candidate = [{'role': 'user', 'content': content[-keep:]}]
            ids, labels = encode(candidate, system, decision['target'], 2**31 - 1)
            if len(ids) <= cutoff and [x for x in labels if x != -100] == target:
                best, low = keep, keep + 1
            else:
                high = keep - 1
        if not best:
            return None
        removed_user_chars += len(content) - best
        history[-1]['content'] = content[-best:]


def factory_encoder(model, revision, local_only):
    if version('llamafactory') != '0.9.5':
        raise ValueError('This exporter requires LLaMA-Factory 0.9.5')
    from transformers import AutoProcessor, AutoTokenizer
    from llamafactory.data import get_template_and_fix_tokenizer
    from llamafactory.data.processor.supervised import SupervisedDatasetProcessor
    from llamafactory.hparams import DataArguments

    tokenizer = AutoTokenizer.from_pretrained(model, revision=revision, local_files_only=local_only,
                                               trust_remote_code=False)
    multimodal_processor = AutoProcessor.from_pretrained(model, revision=revision,
                                                         local_files_only=local_only, trust_remote_code=False)
    args = DataArguments(template='qwen3_5_nothink', mask_history=True, train_on_prompt=False)
    template = get_template_and_fix_tokenizer(tokenizer, args)
    processor = SupervisedDatasetProcessor(template=template, tokenizer=tokenizer,
                                           processor=multimodal_processor, data_args=args)

    def encode(messages, system, target, cutoff):
        args.cutoff_len = cutoff
        return processor._encode_data_example(messages, [{'role': 'assistant', 'content': target}],
                                              system, None, [], [], [])
    return encode


def export(args):
    if args.max_tokens < 2 or args.max_steps < 1:
        raise ValueError('max-tokens must be at least 2 and max-steps must be positive')
    prepared_path, training_path = args.output_dir.resolve(), args.train_output_dir.resolve()
    if args.train_output_dir.exists() or training_path == prepared_path or prepared_path in training_path.parents:
        raise ValueError('training output must be a new directory outside the prepared data directory')
    train = list(read_converted(args.train_input))
    validation = list(read_converted(args.validation_input)) if args.validation_input else []
    check_splits(train, validation)
    encode = factory_encoder(args.model, args.revision, args.local_only)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'llamafactory_version': '0.9.5', 'model': args.model, 'revision': args.revision,
              'max_tokens': args.max_tokens, 'truncate': args.truncate,
              'truncation_policy': 'remove oldest complete turns, then trim the last user message from the left; preserve system and target',
              'source_sha256': {str(p): sha256(p) for p in args.train_input + args.validation_input},
              'counts': {}}
    registry = {}
    with (args.output_dir / 'manifest.jsonl').open('w', encoding='utf-8') as manifest, \
         (args.output_dir / 'excluded.jsonl').open('w', encoding='utf-8') as excluded:
        for split, rows in [('train', train), ('validation', validation)]:
            counts = Counter(trajectories=len(rows))
            name = f'swe_{split}'
            registry[name] = {'file_name': f'{split}.jsonl', 'formatting': 'sharegpt',
                              'columns': {'messages': 'conversations', 'system': 'system'}}
            with (args.output_dir / f'{split}.jsonl').open('w', encoding='utf-8') as out:
                for row in rows:
                    for decision in build_decisions(row, split):
                        counts['decisions'] += 1
                        info = {k: decision[k] for k in ('trajectory_id', 'event_index', 'split', 'action', 'source')}
                        try:
                            fitted = fit_decision(decision, encode, args.max_tokens, args.truncate)
                        except ValueError as exc:
                            counts['excluded'] += 1
                            excluded.write(json.dumps({**info, 'reason': str(exc)}, ensure_ascii=False) + '\n')
                            continue
                        if fitted is None:
                            counts['excluded'] += 1
                            excluded.write(json.dumps({**info, 'reason': 'length_limit_or_no_complete_history_turn_to_remove'}, ensure_ascii=False) + '\n')
                            continue
                        counts['samples'] += 1
                        counts['truncated'] += bool(fitted['removed_turns'] or fitted['removed_user_chars'])
                        messages = fitted['messages'] + [{'role': 'assistant', 'content': decision['target']}]
                        conversations = [{'from': 'human' if m['role'] == 'user' else 'gpt', 'value': m['content']} for m in messages]
                        out.write(json.dumps({'system': fitted['system'], 'conversations': conversations}, ensure_ascii=False) + '\n')
                        manifest.write(json.dumps({**info, 'sample_index': counts['samples'] - 1,
                                                   'original_tokens': fitted['original_tokens'],
                                                   'final_tokens': len(fitted['input_ids']),
                                                   'removed_tokens': fitted['removed_tokens'],
                                                   'removed_turns': fitted['removed_turns'],
                                                   'removed_user_chars': fitted['removed_user_chars'],
                                                   'input_ids_sha256': token_hash(fitted['input_ids']),
                                                   'labels_sha256': token_hash(fitted['labels'])}, ensure_ascii=False) + '\n')
            report['counts'][split] = dict(counts)
    (args.output_dir / 'dataset_info.json').write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n')
    if not report['counts']['train'].get('samples'):
        (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        raise ValueError('no train samples survived; inspect report and exclusions')
    eval_steps = getattr(args, 'eval_steps', 0)
    save_steps = getattr(args, 'save_steps', 0)
    if eval_steps < 0 or save_steps < 0:
        raise ValueError('eval-steps and save-steps cannot be negative')
    if eval_steps and not report['counts']['validation'].get('samples'):
        raise ValueError('validation evaluation requested but no validation samples survived')
    config = {'model_name_or_path': args.model, 'model_revision': args.revision,
              'trust_remote_code': False, 'quantization_bit': 4, 'quantization_method': 'bnb',
              'quantization_type': 'nf4', 'double_quantization': True,
              'stage': 'sft', 'do_train': True, 'finetuning_type': 'lora',
              'lora_rank': 8, 'lora_alpha': 16, 'lora_target': 'q_proj,v_proj',
              'freeze_vision_tower': True, 'freeze_multi_modal_projector': True,
              'dataset_dir': str(args.output_dir.resolve()), 'dataset': 'swe_train',
              'template': 'qwen3_5_nothink', 'cutoff_len': args.max_tokens,
              'mask_history': True, 'train_on_prompt': False, 'packing': False,
              'preprocessing_num_workers': 1, 'output_dir': str(args.train_output_dir.resolve()),
              'overwrite_output_dir': False, 'logging_steps': 1, 'save_strategy': 'steps',
              'save_steps': save_steps or args.max_steps, 'report_to': 'none', 'per_device_train_batch_size': 1,
              'gradient_accumulation_steps': 1, 'learning_rate': 2e-4, 'max_steps': args.max_steps,
              'fp16': True, 'seed': 42}
    if eval_steps:
        config.update(eval_dataset='swe_validation', eval_strategy='steps',
                      eval_steps=eval_steps, per_device_eval_batch_size=1)
    # JSON is a YAML subset and is accepted by LLaMA-Factory's YAML parser.
    (args.output_dir / 'smoke.yaml').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    report['output_sha256'] = {name: sha256(args.output_dir / name) for name in
                               ('train.jsonl', 'validation.jsonl', 'dataset_info.json', 'smoke.yaml', 'manifest.jsonl', 'excluded.jsonl')}
    (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def verify(args):
    report = json.loads((args.prepared_dir / 'report.json').read_text())
    for name, expected in report['output_sha256'].items():
        if sha256(args.prepared_dir / name) != expected:
            raise ValueError(f'prepared file changed: {name}')
    config = json.loads((args.prepared_dir / 'smoke.yaml').read_text())
    if Path(config['dataset_dir']).resolve() != args.prepared_dir.resolve():
        raise ValueError('prepared directory moved; regenerate config and report')
    if Path(config['output_dir']).exists():
        raise ValueError('training output already exists; use a new output directory')
    print('Prepared data and config hashes verified; training not started.')


def check_adapter(args):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoTokenizer, BitsAndBytesConfig

    report = json.loads((args.prepared_dir / 'report.json').read_text())
    model_path, revision = report['model'], report['revision']
    tokenizer = AutoTokenizer.from_pretrained(model_path, revision=revision,
                                               local_files_only=args.local_only, trust_remote_code=False)
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                               bnb_4bit_compute_dtype=torch.float16, bnb_4bit_use_double_quant=True)
    base = AutoModelForImageTextToText.from_pretrained(model_path, revision=revision,
              local_files_only=args.local_only, trust_remote_code=False, quantization_config=quant,
              device_map={'': 0}, dtype=torch.float16)
    model = PeftModel.from_pretrained(base, args.adapter_dir, is_trainable=False).eval()
    tokens = tokenizer('Inspect the repository.', return_tensors='pt').to('cuda')
    with torch.inference_mode():
        logits = model(**tokens, use_cache=False).logits.float()
        with model.disable_adapter():
            baseline = model(**tokens, use_cache=False).logits.float()
    if not torch.isfinite(logits).all() or not torch.isfinite(baseline).all():
        raise ValueError('non-finite logits')
    result = {'model': model_path, 'revision': revision, 'adapter_dir': str(args.adapter_dir),
              'reload_ok': True, 'finite_logits': True,
              'max_abs_logit_difference_from_base': float((logits - baseline).abs().max().item()),
              'peak_allocated_mib': round(torch.cuda.max_memory_allocated() / 2**20, 2)}
    with args.report_path.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def audit(args):
    """Run the actual factory data loader and compare every token and label hash."""
    if version('llamafactory') != '0.9.5':
        raise ValueError('This audit requires LLaMA-Factory 0.9.5')
    verify(args)
    from llamafactory.data import get_dataset, get_template_and_fix_tokenizer
    from llamafactory.hparams import get_train_args
    from transformers import AutoProcessor, AutoTokenizer

    config = json.loads((args.prepared_dir / 'smoke.yaml').read_text())
    model_args, data_args, training_args, _, _ = get_train_args(config)
    tokenizer = AutoTokenizer.from_pretrained(model_args.model_name_or_path,
                 revision=model_args.model_revision, local_files_only=True, trust_remote_code=False)
    processor = AutoProcessor.from_pretrained(model_args.model_name_or_path,
                 revision=model_args.model_revision, local_files_only=True, trust_remote_code=False)
    template = get_template_and_fix_tokenizer(tokenizer, data_args)
    manifest = [json.loads(line) for line in (args.prepared_dir / 'manifest.jsonl').read_text().splitlines()]
    checked = {}
    for split in ('train', 'validation'):
        expected = [row for row in manifest if row['split'] == split]
        if not expected:
            checked[split] = 0
            continue
        data_args.dataset = [f'swe_{split}']
        data_args.eval_dataset = None
        dataset = get_dataset(template, model_args, data_args, training_args, 'sft', tokenizer, processor)['train_dataset']
        if len(dataset) != len(expected):
            raise ValueError(f'{split} loader count mismatch: {len(dataset)} != {len(expected)}')
        for i, (row, expected_row) in enumerate(zip(dataset, expected)):
            if (token_hash(row['input_ids']) != expected_row['input_ids_sha256'] or
                    token_hash(row['labels']) != expected_row['labels_sha256']):
                raise ValueError(f'{split} token/label mismatch at sample {i}')
        checked[split] = len(expected)
    print('LLaMA-Factory loader token and label hashes match:', json.dumps(checked))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('export')
    p.add_argument('--train-input', type=Path, nargs='+', required=True)
    p.add_argument('--validation-input', type=Path, nargs='*', default=[])
    p.add_argument('--model', required=True)
    p.add_argument('--revision', required=True)
    p.add_argument('--local-only', action='store_true')
    p.add_argument('--max-tokens', type=int, default=1024)
    p.add_argument('--truncate', action='store_true')
    p.add_argument('--max-steps', type=int, default=10)
    p.add_argument('--eval-steps', type=int, default=0, help='Evaluate validation every N steps; 0 disables')
    p.add_argument('--save-steps', type=int, default=0, help='Save every N steps; 0 saves at max-steps')
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--train-output-dir', type=Path, required=True)
    p.set_defaults(func=export)
    p = commands.add_parser('verify')
    p.add_argument('--prepared-dir', type=Path, required=True)
    p.set_defaults(func=verify)
    p = commands.add_parser('audit', help='Compare actual LLaMA-Factory loader tokens and labels')
    p.add_argument('--prepared-dir', type=Path, required=True)
    p.set_defaults(func=audit)
    p = commands.add_parser('check-adapter', help='Load a saved adapter on GPU and check finite logits')
    p.add_argument('--prepared-dir', type=Path, required=True)
    p.add_argument('--adapter-dir', type=Path, required=True)
    p.add_argument('--report-path', type=Path, required=True)
    p.add_argument('--local-only', action='store_true')
    p.set_defaults(func=check_adapter)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
