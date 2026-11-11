"""Identity checks and stable fingerprints for the pinned AgentLab Qwen base/adapter."""

import argparse
import hashlib
import json
from pathlib import Path


REVISION = "2fc06364715b967f1860aea9cf38778875588b17"
TOKENIZER_HASHES = {
    "tokenizer.json": "5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42",
    "tokenizer_config.json": "49e2b6e395f959f077f1e992b338919c0d4a9732fc6e613995e06557f843500c",
}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_fixed_model(model_dir):
    path = Path(model_dir)
    if not path.is_dir():
        raise ValueError(f"model directory does not exist: {path}")
    for name, expected in TOKENIZER_HASHES.items():
        file_path = path / name
        if not file_path.is_file() or sha256_file(file_path) != expected:
            raise ValueError(f"fixed Qwen tokenizer file is missing or has the wrong hash: {name}")
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Qwen config.json is missing or invalid") from exc
    if (
        config.get("model_type") != "qwen3_5"
        or "Qwen3_5ForConditionalGeneration" not in config.get("architectures", [])
        or config.get("text_config", {}).get("max_position_embeddings") != 262144
    ):
        raise ValueError("local weights do not match the recorded Qwen3.5-0.8B architecture config")
    if not any((path / name).is_file() for name in ("model.safetensors.index.json", "model.safetensors")):
        raise ValueError("model weight file/index is missing")
    return {"model_revision": REVISION, "model_type": config["model_type"]}


def _directory_fingerprint(path, label):
    path = Path(path)
    if not path.is_dir():
        raise ValueError(f"{label} directory does not exist: {path}")
    digest = hashlib.sha256()
    file_count = 0
    for file_path in sorted(p for p in path.rglob("*") if p.is_file()):
        relative = file_path.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(file_path)))
        digest.update(b"\n")
        file_count += 1
    if not file_count:
        raise ValueError(f"{label} directory has no files")
    return digest.hexdigest()


def model_fingerprint(model_dir, adapter_dir=None):
    identity = verify_fixed_model(model_dir)
    base_digest = _directory_fingerprint(model_dir, "base model")
    result = f"qwen35-{REVISION}:sha256:{base_digest}"
    if adapter_dir is not None:
        adapter = Path(adapter_dir)
        if not (adapter / "adapter_config.json").is_file() or not any(
            (adapter / name).is_file()
            for name in ("adapter_model.safetensors", "adapter_model.bin")
        ):
            raise ValueError("adapter config or weights are missing")
        result += f"+lora:{_directory_fingerprint(adapter, 'adapter')}"
    return {**identity, "fingerprint": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--adapter", type=Path)
    args = parser.parse_args()
    print(json.dumps(model_fingerprint(args.model, args.adapter), indent=2))


if __name__ == "__main__":
    main()
