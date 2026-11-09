#!/usr/bin/env python3
"""Download pinned raw dataset shards for the Vault Agent prototype.

Run from the project root: python src/everos/agent_training/download_datasets.py
Files are stored in .memo-vault/agent-training/datasets/raw by default, outside Git.
This downloads source shards, not split, filtered, or training-ready examples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = PROJECT_ROOT / ".memo-vault/agent-training/datasets/raw"
DATASETS = {
    "agentinstruct": {
        "repo": "zai-org/AgentInstruct",
        "revision": "e252cf78ced8a0ea5f62cfd591784cdbbddbac8a",
        "files": [
            "README.md",
            "data/alfworld-00000-of-00001-302ad687bb3817a4.parquet",
            "data/db-00000-of-00001-916a87c4725da8c0.parquet",
            "data/kg-00000-of-00001-9e159f6d0557d229.parquet",
            "data/mind2web-00000-of-00001-fc25d47330eea0fc.parquet",
            "data/os-00000-of-00001-971539c34fcc7500.parquet",
            "data/webshop-00000-of-00001-9f2ae60445e11b4e.parquet",
        ],
    },
    "codeactinstruct": {
        "repo": "xingyaoww/code-act",
        "revision": "afba34367a8609a1d0044eded531548ab71a58cf",
        "files": ["README.md", "data/codeact-00000-of-00001-41e65239aebc832c.parquet"],
    },
    "swe_smith": {
        "repo": "SWE-bench/SWE-smith-trajectories",
        "revision": "08e109b4a59eaeebf80e4675cd125d42e7ac99a4",
        "files": ["README.md", "data/tool-00000-of-00008.parquet"],
    },
    "mind2web": {
        "repo": "osunlp/Mind2Web",
        "revision": "17ece8eb89862368edc0cc806acee6fca5163474",
        "files": [
            "README.md",
            "data/train/train_10.json",
            "data/train/train_0.json",
            "data/train/train_1.json",
            "data/train/train_2.json",
        ],
    },
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_file(
    url: str, destination: Path, expected_size: int, expected_sha256: str | None
) -> dict:
    """Download atomically; only reuse local content verified against upstream hash."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (
        destination.is_file()
        and expected_sha256
        and destination.stat().st_size == expected_size
        and file_sha256(destination) == expected_sha256
    ):
        return {"size": expected_size, "sha256": expected_sha256, "status": "reused"}
    request = urllib.request.Request(
        url, headers={"User-Agent": "memo-vault-agent-training/1.0"}
    )
    fd, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".part", dir=destination.parent
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            os.fdopen(fd, "wb") as output,
            urllib.request.urlopen(request, timeout=120) as response,
        ):
            while block := response.read(1024 * 1024):
                output.write(block)
                digest.update(block)
                size += len(block)
        actual_sha = digest.hexdigest()
        if size != expected_size or (expected_sha256 and actual_sha != expected_sha256):
            raise ValueError(
                f"Integrity check failed for {destination.name}: "
                f"size={size}, sha256={actual_sha}"
            )
        os.replace(temporary, destination)
        return {"size": size, "sha256": actual_sha, "status": "downloaded"}
    finally:
        temporary.unlink(missing_ok=True)


def repository_files(repo: str, revision: str) -> dict[str, dict]:
    url = f"https://huggingface.co/api/datasets/{repo}/tree/{revision}?recursive=true&expand=true&limit=50"
    request = urllib.request.Request(
        url, headers={"User-Agent": "memo-vault-agent-training/1.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        entries = json.load(response)
    return {entry["path"]: entry for entry in entries if entry.get("type") == "file"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--datasets", nargs="+", choices=DATASETS, default=list(DATASETS)
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created_utc": datetime.now(UTC).isoformat(),
        "stage": "raw_download_only",
        "datasets": {},
    }
    failed = False
    for name in dict.fromkeys(args.datasets):
        config = DATASETS[name]
        result = {
            "repo": config["repo"],
            "revision": config["revision"],
            "files": [],
            "status": "pending",
        }
        manifest["datasets"][name] = result
        try:
            upstream = repository_files(config["repo"], config["revision"])
            for filename in config["files"]:
                entry = upstream[filename]
                lfs_hash = entry.get("lfs", {}).get("oid")
                url = (
                    f"https://huggingface.co/datasets/{config['repo']}"
                    f"/resolve/{config['revision']}/"
                    f"{urllib.parse.quote(filename, safe='/')}"
                )
                print(f"{name}: {filename} ({entry['size']:,} bytes)", flush=True)
                file_result = download_file(
                    url, args.output_dir / name / filename, entry["size"], lfs_hash
                )
                result["files"].append(
                    {
                        "path": filename,
                        "source_url": url,
                        "git_oid": entry.get("oid"),
                        "upstream_lfs_sha256": lfs_hash,
                        **file_result,
                    }
                )
            result["status"] = "complete"
        except (OSError, ValueError, KeyError) as exc:
            failed = True
            result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            print(f"{name}: {result['error']}", file=sys.stderr, flush=True)
    manifest_path = args.output_dir / "download_manifest.json"
    temporary_path = manifest_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary_path, manifest_path)
    print(f"Manifest: {manifest_path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
