"""Reload a Qwen3.5 base and QLoRA adapter and compare finite logits."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--adapter', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise ValueError('report already exists')
    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoTokenizer, BitsAndBytesConfig

    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True, trust_remote_code=False)
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                               bnb_4bit_compute_dtype=torch.float16, bnb_4bit_use_double_quant=True)
    base = AutoModelForImageTextToText.from_pretrained(args.model, local_files_only=True,
        trust_remote_code=False, quantization_config=quant, device_map={'': 0}, dtype=torch.float16)
    model = PeftModel.from_pretrained(base, args.adapter, is_trainable=False).eval()
    inputs = tokenizer('Use Python to compute 1 + 1.', return_tensors='pt').to('cuda')
    with torch.inference_mode():
        adapted = model(**inputs).logits.float()
        with model.disable_adapter():
            baseline = model(**inputs).logits.float()
    if not torch.isfinite(adapted).all() or not torch.isfinite(baseline).all():
        raise ValueError('non-finite logits')
    result = {'adapter': str(args.adapter), 'finite_logits': True,
              'max_abs_logit_difference': float((adapted - baseline).abs().max().item()),
              'peak_allocated_mib': round(torch.cuda.max_memory_allocated() / 2**20, 2)}
    with args.report.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write('\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
