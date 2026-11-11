import hashlib
import json

import pytest

from everos.agent_training.agentlab_qlora import reserve_run_dir, validate_data


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def build_smoke_dir(tmp_path, *, train_tokens=100, ready=False):
    data = tmp_path / "prepared"
    data.mkdir()
    train = [{"system": "s", "conversations": []} for _ in range(200)]
    validation = [{"system": "s", "conversations": []} for _ in range(28)]
    test = [{"system": "s", "conversations": []} for _ in range(23)]
    write_jsonl(data / "train.jsonl", train)
    write_jsonl(data / "validation.jsonl", validation)
    write_jsonl(data / "test.jsonl", test)
    manifest_rows = []
    for split, rows, token_count in (
        ("train", train, train_tokens),
        ("validation", validation, 100),
        ("test", test, 100),
    ):
        for i in range(len(rows)):
            manifest_rows.append({"id": f"{split}-{i}", "split": split, "tokens": token_count,
                                  "sample_index": i, "truncated": False,
                                  "training_ready": ready})
    write_jsonl(data / "manifest.jsonl", manifest_rows)
    (data / "excluded.jsonl").write_text("", encoding="utf-8")
    (data / "dataset_info.json").write_text(
        json.dumps(
            {
                "agentlab_train": {"file_name": "train.jsonl"},
                "agentlab_validation": {"file_name": "validation.jsonl"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    names = ("train.jsonl", "validation.jsonl", "test.jsonl", "manifest.jsonl", "excluded.jsonl", "dataset_info.json")
    report = {
        "model_revision": "2fc06364715b967f1860aea9cf38778875588b17",
        "template": "qwen3_5_nothink",
        "truncation": False,
        "training_ready": ready,
        "output_sha256": {name: hashlib.sha256((data / name).read_bytes()).hexdigest() for name in names},
        "counts": {
            "train": {"samples": 200, "max_tokens": train_tokens},
            "validation": {"samples": 28, "max_tokens": 100},
            "test": {"samples": 23},
        },
    }
    (data / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return data


def test_smoke_needs_explicit_probe_and_excludes_test_from_training(tmp_path):
    data = build_smoke_dir(tmp_path)
    with pytest.raises(ValueError):
        validate_data(data, "smoke", 131072, False)

    result = validate_data(data, "smoke", 131072, True)

    assert result["probe_only"] is True
    assert result["counts"] == {"train": 200, "validation": 28}
    assert "test" not in result["dataset_config"]


def test_cutoff_overflow_and_hash_mismatch_fail_closed(tmp_path):
    data = build_smoke_dir(tmp_path, train_tokens=131073)
    with pytest.raises(ValueError, match="cutoff"):
        validate_data(data, "smoke", 131072, True)
    (data / "train.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        validate_data(data, "smoke", 262144, True)


@pytest.mark.parametrize("mode", ["scale", "full"])
def test_scale_and_full_refuse_unreviewed_data(tmp_path, mode):
    data = build_smoke_dir(tmp_path)
    with pytest.raises(ValueError, match="training_ready"):
        validate_data(data, mode, 262144, True)


def test_run_directory_is_never_overwritten(tmp_path):
    path = tmp_path / "run"
    reserve_run_dir(path)
    with pytest.raises(FileExistsError):
        reserve_run_dir(path)
