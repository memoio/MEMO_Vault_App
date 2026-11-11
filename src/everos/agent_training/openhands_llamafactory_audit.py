"""Compare actual LLaMA-Factory loader output with prepared decision hashes."""
import argparse
import json
from pathlib import Path

from .swe_llamafactory import token_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared-dir', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--max-samples', type=int, default=32)
    parser.add_argument('--cutoff-len', type=int, required=True)
    args = parser.parse_args()
    if args.max_samples < 1 or args.cutoff_len < 2:
        raise ValueError('positive max-samples and cutoff-len >= 2 required')
    from llamafactory.data import get_dataset, get_template_and_fix_tokenizer
    from llamafactory.hparams import get_train_args
    from transformers import AutoProcessor, AutoTokenizer

    path = args.prepared_dir.resolve()
    report = json.loads((path / 'report.json').read_text(encoding='utf-8'))
    config = {'model_name_or_path': args.model, 'model_revision': report['model_revision'],
              'trust_remote_code': False, 'stage': 'sft', 'do_train': True,
              'finetuning_type': 'lora', 'dataset_dir': str(path), 'dataset': 'openhands_train',
              'template': 'qwen3_5_nothink', 'cutoff_len': args.cutoff_len,
              'mask_history': True, 'train_on_prompt': False, 'packing': False,
              'max_samples': args.max_samples, 'preprocessing_num_workers': 1,
              'output_dir': '/tmp/vault-openhands-lf-loader-audit', 'report_to': 'none'}
    model_args, data_args, training_args, _, _ = get_train_args(config)
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True, trust_remote_code=False)
    processor = AutoProcessor.from_pretrained(args.model, local_files_only=True, trust_remote_code=False)
    template = get_template_and_fix_tokenizer(tokenizer, data_args)
    with (path / 'manifest.jsonl').open(encoding='utf-8') as stream:
        manifest = [json.loads(line) for line in stream]
    checked = {}
    for part in ('train', 'validation'):
        expected = [r for r in manifest if r['split'] == part][:args.max_samples]
        if not expected:
            raise ValueError(f'no samples to audit: {part}')
        if any(r['tokens'] > args.cutoff_len for r in expected):
            raise ValueError(f'{part} sample exceeds audit cutoff; raise cutoff')
        data_args.dataset = [f'openhands_{part}']
        data_args.eval_dataset = None
        dataset = get_dataset(template, model_args, data_args, training_args, 'sft', tokenizer, processor)['train_dataset']
        if len(dataset) != len(expected):
            raise ValueError(f'{part} count mismatch: {len(dataset)} != {len(expected)}')
        for index, (actual, exp) in enumerate(zip(dataset, expected, strict=True)):
            if token_hash(actual['input_ids']) != exp['input_ids_sha256'] or token_hash(actual['labels']) != exp['labels_sha256']:
                raise ValueError(f'{part} token/label mismatch at {index}')
        checked[part] = len(expected)
    print('LLaMA-Factory loader verified:', json.dumps(checked))


if __name__ == '__main__':
    main()
