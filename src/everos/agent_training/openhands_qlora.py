"""Prepare or run a recorded Qwen3.5 OpenHands LLaMA-Factory QLoRA job."""
import argparse
from collections import Counter
from importlib.metadata import version
import json
import math
from pathlib import Path
import subprocess
import sys
import time

from .openhands_inventory import sha256_file

REVISION = '2fc06364715b967f1860aea9cf38778875588b17'
TOKENIZER_HASHES = {
    'tokenizer.json': '5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42',
    'tokenizer_config.json': '49e2b6e395f959f077f1e992b338919c0d4a9732fc6e613995e06557f843500c',
}
MODEL_HASHES = {
    'model.safetensors-00001-of-00001.safetensors': '04b1c301231dd422b8860db31311ab2721511346a32cb1e079c4c4e5f1fe4696',
    'config.json': 'b90b86f35c8e6925ef74ee04d0e758f0a845c83a42089ad82bbaa948de9b4204',
    'chat_template.jinja': '273d8e0e683b885071fb17e08d71e5f2a5ddfb5309756181681de4f5a1822d80',
}


def assess_training_state(state, mode, max_steps):
    losses = [entry['loss'] for entry in state.get('log_history', []) if 'loss' in entry]
    finite_losses = bool(losses) and all(isinstance(x, (int, float)) and math.isfinite(x) for x in losses)
    steps = state.get('global_step', 0)
    expected_steps = steps == max_steps if mode in ('smoke', 'scale') else steps > 0
    return steps, finite_losses, expected_steps


def assess_data_readiness(report, mode, probe_unverified_data=False):
    ready = report.get('training_ready') is True
    if ready:
        return {'training_ready': True, 'probe_only': False}
    if mode == 'smoke' and probe_unverified_data:
        return {'training_ready': False, 'probe_only': True}
    if mode == 'full':
        raise ValueError('full training requires training_ready=true in the prepared data report')
    raise ValueError('data is not marked training_ready=true; use --probe-unverified-data for a technical smoke only')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('smoke', 'scale', 'full'), required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--cutoff-len', type=int, required=True)
    parser.add_argument('--allow-truncation', action='store_true')
    parser.add_argument('--max-steps', type=int, default=10)
    parser.add_argument('--epochs', type=float, default=1.0)
    parser.add_argument('--learning-rate', type=float, default=2e-4)
    parser.add_argument('--gradient-accumulation', type=int, default=1)
    parser.add_argument('--eval-steps', type=int, default=0)
    parser.add_argument('--probe-unverified-data', action='store_true',
                        help='allow unreviewed data for a technical smoke only; never enables scale/full training')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.cutoff_len < 2 or args.max_steps < 1 or args.epochs <= 0 or args.gradient_accumulation < 1 or args.eval_steps < 0:
        raise ValueError('cutoff, steps, epochs or gradient accumulation invalid')
    if args.mode == 'scale' and not 50 <= args.max_steps <= 100:
        raise ValueError('scale mode requires 50 to 100 optimizer steps')
    if args.mode != 'smoke' and args.probe_unverified_data:
        raise ValueError('--probe-unverified-data is only valid for smoke mode')
    data = args.data_dir.resolve()
    model = args.model.resolve()
    run = args.run_dir.resolve()
    if run.exists() or run == data or data in run.parents or run in data.parents:
        raise ValueError('run-dir must be a new directory separate from the prepared data')
    report = json.loads((data / 'report.json').read_text(encoding='utf-8'))
    if report.get('model_revision') != REVISION or report.get('template') != 'qwen3_5_nothink' or report.get('selected_source') != 'codeactinstruct':
        raise ValueError('unexpected data model/template/source')
    if report.get('truncation') is not False:
        raise ValueError('input must be the untruncated prepared data')
    data_readiness = assess_data_readiness(report, args.mode, args.probe_unverified_data)
    for name in ('train.jsonl', 'validation.jsonl', 'dataset_info.json', 'manifest.jsonl'):
        if sha256_file(data / name) != report['output_sha256'][name]:
            raise ValueError(f'data hash mismatch: {name}')
    for name, expected in {**TOKENIZER_HASHES, **MODEL_HASHES}.items():
        if sha256_file(model / name) != expected:
            raise ValueError(f'Qwen fixed model file mismatch: {name}')
    counts = Counter()
    expected_index = Counter()
    with (data / 'manifest.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            part = row['split']
            if part not in ('train', 'validation'):
                continue
            if row['sample_index'] != expected_index[part] or row.get('truncated') is not False:
                raise ValueError(f'invalid manifest ordering or truncation: {part}')
            expected_index[part] += 1
            counts[part] += 1
            if row['tokens'] > args.cutoff_len:
                counts[part + '_over_cutoff'] += 1
    for part in ('train', 'validation'):
        if counts[part] != report['counts'][part]['samples'] or not counts[part]:
            raise ValueError(f'manifest count mismatch or empty split: {part}')
    affected = counts['train_over_cutoff'] + counts['validation_over_cutoff']
    if affected and not args.allow_truncation:
        raise ValueError(f'{affected} train/validation samples exceed cutoff {args.cutoff_len}; choose a larger cutoff or explicitly pass --allow-truncation')
    if not args.prepare_only and version('llamafactory') != '0.9.5':
        raise ValueError('LLaMA-Factory 0.9.5 required')
    config = {'model_name_or_path': str(model), 'model_revision': REVISION,
              'trust_remote_code': False, 'quantization_bit': 4, 'quantization_method': 'bnb',
              'quantization_type': 'nf4', 'double_quantization': True,
              'stage': 'sft', 'do_train': True, 'finetuning_type': 'lora',
              'lora_rank': 8, 'lora_alpha': 16, 'lora_target': 'q_proj,v_proj',
              'freeze_vision_tower': True, 'freeze_multi_modal_projector': True,
              'dataset_dir': str(data), 'dataset': 'openhands_train', 'template': 'qwen3_5_nothink',
              'cutoff_len': args.cutoff_len, 'mask_history': True, 'train_on_prompt': False,
              'packing': False, 'preprocessing_num_workers': 1, 'gradient_checkpointing': True,
              'do_eval': args.eval_steps > 0,
              'eval_dataset': 'openhands_validation' if args.eval_steps > 0 else None,
              'eval_strategy': 'steps' if args.eval_steps > 0 else 'no',
              'eval_steps': args.eval_steps if args.eval_steps > 0 else None,
              'output_dir': str(run / 'adapter'), 'overwrite_output_dir': False,
              'per_device_train_batch_size': 1, 'gradient_accumulation_steps': args.gradient_accumulation,
              'learning_rate': args.learning_rate, 'logging_steps': 1, 'logging_nan_inf_filter': False, 'save_strategy': 'steps',
              'save_steps': args.max_steps if args.mode in ('smoke', 'scale') else 100,
              'report_to': 'none', 'disable_tqdm': False, 'fp16': True, 'seed': 42}
    if args.mode in ('smoke', 'scale'):
        config['max_steps'] = args.max_steps
    else:
        config['num_train_epochs'] = args.epochs
    run.mkdir(parents=True, exist_ok=False)
    config_path = run / 'training.yaml'  # JSON is valid YAML.
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    metadata = {'mode': args.mode, 'status': 'prepared', 'allow_truncation': args.allow_truncation,
                **data_readiness, 'eval_steps': args.eval_steps,
                'cutoff_len': args.cutoff_len, 'sample_counts': dict(counts),
                'data_report_sha256': sha256_file(data / 'report.json'),
                'model_file_sha256': {**TOKENIZER_HASHES, **MODEL_HASHES},
                'training_config_sha256': sha256_file(config_path),
                'caveats': ['Truncation may remove target tokens or important context.',
                            'No held-out test data is used in training.',
                            'Token length audit is based on the recorded fixed tokenizer and template.']}
    (run / 'run-report.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    if args.prepare_only:
        return
    start = time.monotonic()
    metadata['status'] = 'running'
    (run / 'run-report.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Training output and progress: {run / "training.log"}', flush=True)
    with (run / 'training.log').open('wb') as log:
        with subprocess.Popen(
            [sys.executable, '-u', '-m', 'llamafactory.cli', 'train', str(config_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ) as process:
            try:
                while chunk := process.stdout.read1(65536):
                    log.write(chunk)
                    log.flush()
                    try:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    except BrokenPipeError:
                        # Keep the training log even if the output consumer exits.
                        sys.stdout = open('/dev/null', 'w', encoding='utf-8')
            except KeyboardInterrupt:
                process.terminate()
                process.wait()
                raise
            returncode = process.wait()
    adapter = run / 'adapter'
    weights = next((p for p in (adapter / 'adapter_model.safetensors', adapter / 'adapter_model.bin') if p.is_file()), None)
    state_path = adapter / 'trainer_state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
    steps, finite_losses, expected_steps = assess_training_state(state, args.mode, args.max_steps)
    complete = bool(returncode == 0 and weights and (adapter / 'adapter_config.json').is_file()
                    and finite_losses and expected_steps)
    metadata.update(status='complete' if complete else 'failed',
                    elapsed_seconds=round(time.monotonic() - start, 2), exit_code=returncode,
                    global_step=steps, logged_losses_finite=finite_losses,
                    adapter_weights_sha256=sha256_file(weights) if weights else None)
    (run / 'run-report.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    if metadata['status'] != 'complete':
        raise SystemExit('training failed or final adapter missing; check training.log')


if __name__ == '__main__':
    main()
