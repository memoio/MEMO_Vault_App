"""Validate and run AgentLab-specific, no-truncation QLoRA jobs with LLaMA-Factory."""

import argparse
from collections import Counter
from importlib.metadata import version
import json
import math
from pathlib import Path
import subprocess
import sys
import time

from .agentlab_qwen_model import REVISION, sha256_file, verify_fixed_model


EXPECTED_COUNTS = {
    "smoke": {"train": 200, "validation": 28, "test": 23},
    "full": {"train": 7464, "validation": 670, "test": 926},
}
REPORT_FILES = (
    "train.jsonl",
    "validation.jsonl",
    "test.jsonl",
    "manifest.jsonl",
    "excluded.jsonl",
    "dataset_info.json",
)


def reserve_run_dir(path):
    Path(path).mkdir(parents=True, exist_ok=False)


def _read_jsonl(path):
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"blank JSONL line at {path}:{line_number}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL row is not an object at {path}:{line_number}")
            rows.append(row)
    return rows


def validate_data(data_dir, mode, cutoff_len, probe_unverified_data=False):
    if mode not in {"smoke", "scale", "full"}:
        raise ValueError("mode must be smoke, scale, or full")
    if cutoff_len < 2:
        raise ValueError("cutoff_len must be at least 2")
    if probe_unverified_data and mode != "smoke":
        raise ValueError("--probe-unverified-data cannot enable scale/full; require training_ready=true")
    data = Path(data_dir)
    report_path = data / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("model_revision") != REVISION:
        raise ValueError("prepared data uses a different Qwen model revision")
    if report.get("template") != "qwen3_5_nothink" or report.get("truncation") is not False:
        raise ValueError("expected qwen3_5_nothink data with truncation=false")

    ready = report.get("training_ready") is True
    if not ready and not (mode == "smoke" and probe_unverified_data):
        raise ValueError(
            "data is not marked training_ready=true; only a smoke may opt into "
            "--probe-unverified-data"
        )
    probe_only = not ready

    expected_hashes = report.get("output_sha256")
    if not isinstance(expected_hashes, dict):
        raise ValueError("prepared data report has no output hashes")
    for name in REPORT_FILES:
        path = data / name
        if not path.is_file() or sha256_file(path) != expected_hashes.get(name):
            raise ValueError(f"prepared data hash mismatch: {name}")
    registry = json.loads((data / "dataset_info.json").read_text(encoding="utf-8"))
    if set(registry) != {"agentlab_train", "agentlab_validation"} or any(
        entry.get("file_name") == "test.jsonl" for entry in registry.values()
    ):
        raise ValueError("dataset registry must expose train/validation only, never test")

    rows_by_split = {
        part: _read_jsonl(data / f"{part}.jsonl") for part in ("train", "validation", "test")
    }
    manifest = _read_jsonl(data / "manifest.jsonl")
    manifest_counts = Counter()
    next_index = Counter()
    maximum = Counter()
    for row in manifest:
        part = row.get("split")
        if part not in rows_by_split:
            raise ValueError(f"unknown manifest split: {part}")
        if row.get("sample_index") != next_index[part]:
            raise ValueError(f"non-contiguous manifest order in {part}")
        next_index[part] += 1
        if row.get("truncated") is not False:
            raise ValueError(f"manifest marks a truncated sample in {part}")
        tokens = row.get("tokens")
        if not isinstance(tokens, int) or tokens < 1:
            raise ValueError(f"invalid token count in {part} manifest")
        maximum[part] = max(maximum[part], tokens)
        manifest_counts[part] += 1
        if part in ("train", "validation") and tokens > cutoff_len:
            raise ValueError(
                f"{part} sample {row.get('id')} has {tokens} tokens above cutoff {cutoff_len}; "
                "training is stopped without truncation or row removal"
            )
        if ready and part in ("train", "validation") and row.get("training_ready") is not True:
            raise ValueError(f"{part} manifest contains a sample not marked training_ready=true")

    for part, rows in rows_by_split.items():
        if manifest_counts[part] != len(rows) or manifest_counts[part] != report["counts"][part]["samples"]:
            raise ValueError(f"manifest/data/report count mismatch in {part}")
    if mode in EXPECTED_COUNTS:
        for part, expected_count in EXPECTED_COUNTS[mode].items():
            if manifest_counts[part] != expected_count:
                raise ValueError(f"{mode} expects {expected_count} {part} samples, got {manifest_counts[part]}")
    if manifest_counts["test"] == 0:
        raise ValueError("prepared data is missing the held-out test partition")

    dataset_config = {
        "dataset_dir": str(data.resolve()),
        "dataset": "agentlab_train",
        "template": "qwen3_5_nothink",
        "eval_dataset": "agentlab_validation",
        "test_dataset": None,
        "cutoff_len": cutoff_len,
    }
    return {
        "training_ready": ready,
        "probe_only": probe_only,
        "counts": {part: manifest_counts[part] for part in ("train", "validation")},
        "all_counts": dict(manifest_counts),
        "max_tokens": dict(maximum),
        "dataset_config": dataset_config,
        "report_sha256": sha256_file(report_path),
    }


def build_training_config(
    data_dir,
    model_dir,
    run_dir,
    mode,
    cutoff_len,
    max_steps=10,
    epochs=1.0,
    learning_rate=2e-4,
    gradient_accumulation=1,
    eval_steps=5,
):
    if mode == "scale" and not 50 <= max_steps <= 100:
        raise ValueError("scale mode requires 50 to 100 optimizer steps")
    if max_steps < 1 or epochs <= 0 or learning_rate <= 0 or gradient_accumulation < 1 or eval_steps < 0:
        raise ValueError("invalid training step, epoch, learning-rate, accumulation, or eval settings")
    run = Path(run_dir).resolve()
    data = Path(data_dir).resolve()
    model = Path(model_dir).resolve()
    if run.exists() or run == data or data in run.parents or run in data.parents:
        raise ValueError("run-dir must be new and separate from model/data")
    config = {
        "model_name_or_path": str(model),
        "model_revision": REVISION,
        "trust_remote_code": False,
        "quantization_bit": 4,
        "quantization_method": "bnb",
        "quantization_type": "nf4",
        "double_quantization": True,
        "stage": "sft",
        "do_train": True,
        "finetuning_type": "lora",
        "lora_rank": 8,
        "lora_alpha": 16,
        "lora_target": "q_proj,v_proj",
        "freeze_vision_tower": True,
        "freeze_multi_modal_projector": True,
        "dataset_dir": str(data),
        "dataset": "agentlab_train",
        "template": "qwen3_5_nothink",
        "cutoff_len": cutoff_len,
        "mask_history": True,
        "train_on_prompt": False,
        "packing": False,
        "preprocessing_num_workers": 1,
        "gradient_checkpointing": True,
        "do_eval": eval_steps > 0,
        "eval_dataset": "agentlab_validation" if eval_steps else None,
        "eval_strategy": "steps" if eval_steps else "no",
        "eval_steps": eval_steps if eval_steps else None,
        "output_dir": str(run / "adapter"),
        "overwrite_output_dir": False,
        "per_device_train_batch_size": 1,
        "gradient_accumulation_steps": gradient_accumulation,
        "learning_rate": learning_rate,
        "logging_steps": 1,
        "logging_nan_inf_filter": False,
        "save_strategy": "steps",
        "save_steps": max_steps if mode in {"smoke", "scale"} else 100,
        "report_to": "none",
        "fp16": True,
        "seed": 42,
    }
    if mode in {"smoke", "scale"}:
        config["max_steps"] = max_steps
    else:
        config["num_train_epochs"] = epochs
    return config


def _assess_training_state(state, mode, max_steps):
    history = state.get("log_history", [])
    train_losses = [item["loss"] for item in history if "loss" in item]
    eval_losses = [item["eval_loss"] for item in history if "eval_loss" in item]
    finite_train = bool(train_losses) and all(
        isinstance(value, (int, float)) and math.isfinite(value) for value in train_losses
    )
    finite_eval = bool(eval_losses) and all(
        isinstance(value, (int, float)) and math.isfinite(value) for value in eval_losses
    )
    step_count = state.get("global_step", 0)
    expected = step_count == max_steps if mode in {"smoke", "scale"} else step_count > 0
    return step_count, finite_train, finite_eval, expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "scale", "full"), required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cutoff-len", type=int, required=True)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--gradient-accumulation", type=int, default=1)
    parser.add_argument("--eval-steps", type=int, default=5)
    parser.add_argument("--probe-unverified-data", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.probe_unverified_data and args.mode != "smoke":
        parser.error("--probe-unverified-data cannot enable scale/full; require training_ready=true")
    if args.mode == "full" and args.cutoff_len != 262144:
        parser.error("full mode requires cutoff_len=262144")
    if args.mode == "smoke" and args.cutoff_len != 131072:
        parser.error("smoke mode requires cutoff_len=131072")

    data_dir = args.data_dir.resolve()
    model_dir = args.model.resolve()
    run_dir = args.run_dir.resolve()
    if run_dir.exists() or run_dir == data_dir or data_dir in run_dir.parents or run_dir in data_dir.parents:
        parser.error("run-dir must be new and separate from prepared data")
    validation = validate_data(data_dir, args.mode, args.cutoff_len, args.probe_unverified_data)
    verify_fixed_model(model_dir)
    if not args.prepare_only and version("llamafactory") != "0.9.5":
        raise ValueError(f"LLaMA-Factory 0.9.5 required, found {version('llamafactory')}")
    config = build_training_config(
        data_dir,
        model_dir,
        run_dir,
        args.mode,
        args.cutoff_len,
        args.max_steps,
        args.epochs,
        args.learning_rate,
        args.gradient_accumulation,
        args.eval_steps,
    )
    reserve_run_dir(run_dir)
    config_path = run_dir / "training.yaml"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    run_report = {
        "mode": args.mode,
        "status": "prepared",
        "training_ready": validation["training_ready"],
        "probe_only": validation["probe_only"],
        "counts": validation["all_counts"],
        "max_tokens": validation["max_tokens"],
        "cutoff_len": args.cutoff_len,
        "data_report_sha256": validation["report_sha256"],
        "training_config_sha256": sha256_file(config_path),
        "test_used_for_training": False,
        "truncation_allowed": False,
        "notes": [
            "Current published loss masks are unknown; unreviewed data is only permitted as a technical smoke probe.",
            "The held-out test partition is never registered in the training configuration.",
        ],
    }
    (run_dir / "run-report.json").write_text(
        json.dumps(run_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(run_report, ensure_ascii=False, indent=2), flush=True)
    if args.prepare_only:
        return

    started = time.monotonic()
    run_report["status"] = "running"
    (run_dir / "run-report.json").write_text(
        json.dumps(run_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (run_dir / "training.log").open("x", encoding="utf-8") as log:
        result = subprocess.run(
            [sys.executable, "-m", "llamafactory.cli", "train", str(config_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    adapter = run_dir / "adapter"
    weight = next(
        (adapter / name for name in ("adapter_model.safetensors", "adapter_model.bin") if (adapter / name).is_file()),
        None,
    )
    state_path = adapter / "trainer_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    steps, finite_train, finite_eval, expected = _assess_training_state(
        state, args.mode, args.max_steps
    )
    eval_expected = args.eval_steps > 0
    complete = bool(
        result.returncode == 0
        and weight
        and (adapter / "adapter_config.json").is_file()
        and finite_train
        and (finite_eval or not eval_expected)
        and expected
    )
    run_report.update(
        status="complete" if complete else "failed",
        elapsed_seconds=round(time.monotonic() - started, 2),
        exit_code=result.returncode,
        global_step=steps,
        finite_training_loss=finite_train,
        finite_validation_loss=finite_eval,
        adapter_weights_sha256=sha256_file(weight) if weight else None,
        adapter_dir=str(adapter.resolve()) if adapter.is_dir() else None,
    )
    (run_dir / "run-report.json").write_text(
        json.dumps(run_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(run_report, ensure_ascii=False, indent=2))
    if not complete:
        raise SystemExit("training failed or final adapter/finite metrics are missing; inspect training.log")


if __name__ == "__main__":
    main()
