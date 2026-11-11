"""Offline loss evaluation on preserved CodeAct test decisions, without truncation."""
import argparse
import hashlib
import json
import math
from pathlib import Path

from .openhands_inventory import sha256_file
from .swe_llamafactory import factory_encoder, token_hash

REVISION = '2fc06364715b967f1860aea9cf38778875588b17'
TOKENIZER = {'tokenizer.json': '5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42',
             'tokenizer_config.json': '49e2b6e395f959f077f1e992b338919c0d4a9732fc6e613995e06557f843500c'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--prepared-dir', type=Path, required=True)
    ap.add_argument('--model', type=Path, required=True)
    ap.add_argument('--mode', choices=('base', 'adapter'), required=True)
    ap.add_argument('--adapter', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--cutoff-len', type=int, required=True)
    ap.add_argument('--max-samples', type=int, default=0, help='0 means all test decisions')
    args = ap.parse_args()
    if args.output.exists() or args.cutoff_len < 2 or args.max_samples < 0:
        raise ValueError('new output dir and valid cutoff / sample count required')
    if (args.mode == 'adapter') != bool(args.adapter):
        raise ValueError('pass --adapter only for adapter mode')
    report = json.loads((args.prepared_dir/'report.json').read_text(encoding='utf-8'))
    if report.get('model_revision') != REVISION or report.get('selected_source') != 'codeactinstruct':
        raise ValueError('prepared data model or source mismatch')
    for name in ('test.jsonl', 'manifest.jsonl'):
        if sha256_file(args.prepared_dir/name) != report['output_sha256'][name]:
            raise ValueError(f'prepared file hash mismatch: {name}')
    for name, expected in TOKENIZER.items():
        if sha256_file(args.model/name) != expected:
            raise ValueError(f'tokenizer hash mismatch: {name}')
    base_weight_files = sorted(args.model.glob('*.safetensors')) + sorted(args.model.glob('pytorch_model*.bin'))
    if not base_weight_files:
        raise ValueError('base model weight files not found')
    base_weight_hashes = {p.name: sha256_file(p) for p in base_weight_files}
    metadata = [json.loads(x) for x in (args.prepared_dir/'manifest.jsonl').read_text(encoding='utf-8').splitlines()]
    metadata = [x for x in metadata if x['split'] == 'test']
    with (args.prepared_dir/'test.jsonl').open(encoding='utf-8') as f:
        rows = [json.loads(x) for x in f]
    if len(rows) != len(metadata):
        raise ValueError('test sample and manifest counts differ')
    if args.max_samples:
        rows, metadata = rows[:args.max_samples], metadata[:args.max_samples]
    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, BitsAndBytesConfig
    encode = factory_encoder(str(args.model), REVISION, True)
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                               bnb_4bit_compute_dtype=torch.float16, bnb_4bit_use_double_quant=True)
    model = AutoModelForImageTextToText.from_pretrained(str(args.model), local_files_only=True,
        trust_remote_code=False, quantization_config=quant, device_map={'': 0}, dtype=torch.float16).eval()
    if args.adapter:
        model = PeftModel.from_pretrained(model, args.adapter, is_trainable=False).eval()
    weights = None
    if args.adapter:
        weights = next((x for x in (args.adapter/'adapter_model.safetensors', args.adapter/'adapter_model.bin') if x.is_file()), None)
        if not weights:
            raise ValueError('adapter weights not found')
    args.output.mkdir(parents=True, exist_ok=False)
    total_nll, total_tokens, evaluated, skipped = 0.0, 0, 0, 0
    with (args.output/'decisions.jsonl').open('x', encoding='utf-8') as out:
        for idx, (row, meta) in enumerate(zip(rows, metadata, strict=True)):
            if meta['sample_index'] != idx:
                raise ValueError('manifest ordering mismatch')
            record = {'sample_index': idx, 'trajectory_id': meta['trajectory_id'], 'tokens': meta['tokens']}
            if meta['tokens'] > args.cutoff_len:
                record['status'] = 'over_cutoff_not_truncated'
                skipped += 1
            else:
                messages = row['conversations']
                history = [{'role': 'user' if m['from'] == 'human' else 'assistant', 'content': m['value']} for m in messages[:-1]]
                ids, labels = encode(history, row['system'], messages[-1]['value'], 2**31-1)
                if token_hash(ids) != meta['input_ids_sha256'] or token_hash(labels) != meta['labels_sha256']:
                    raise ValueError(f'token/label hash mismatch at {idx}')
                n = sum(x != -100 for x in labels[1:])
                if not n:
                    raise ValueError(f'no supervised tokens: {idx}')
                with torch.inference_mode():
                    batch = torch.tensor([ids], dtype=torch.long, device='cuda')
                    targets = torch.tensor([labels], dtype=torch.long, device='cuda')
                    loss = float(model(input_ids=batch, labels=targets).loss.item())
                if not math.isfinite(loss):
                    raise ValueError(f'non-finite loss: {idx}')
                record.update(status='ok', mean_nll=loss, supervised_tokens=n)
                total_nll += loss*n; total_tokens += n; evaluated += 1
            out.write(json.dumps(record, ensure_ascii=False)+'\n')
            out.flush()
    result = {'mode': args.mode, 'model_revision': REVISION,
        'base_model_weight_sha256': base_weight_hashes,
        'adapter_weights_sha256': sha256_file(weights) if weights else None,
        'test_sha256': sha256_file(args.prepared_dir/'test.jsonl'),
        'manifest_sha256': sha256_file(args.prepared_dir/'manifest.jsonl'),
        'cutoff_len': args.cutoff_len, 'max_samples': args.max_samples, 'truncation': False,
        'requested': len(rows), 'evaluated': evaluated, 'over_cutoff_excluded': skipped,
        'supervised_tokens': total_tokens, 'token_weighted_nll': total_nll/total_tokens if total_tokens else None,
        'limitation': 'Offline next-output loss is not interactive task success; compare identical selected decisions only.'}
    (args.output/'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
