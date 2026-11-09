#!/usr/bin/env python3
"""Prepare versioned, review-only ADP candidates from downloaded source shards.

Run: PYTHONPATH=src /root/anaconda3/bin/python -m everos.agent_training.prepare_datasets
Requires pyarrow and ijson. No network access or model training is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from everos.entrypoints.memo_vault.adp_converter import validate_trajectory

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / ".memo-vault/agent-training/datasets/raw"
OUTPUT = ROOT / ".memo-vault/agent-training/datasets/prepared_v1"
QUOTAS = {
    "agentinstruct": (150, 50),
    "codeactinstruct": (150, 50),
    "swe_smith": (100, 100),
    "mind2web": (100, 100),
}


def stable_number(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], 16)


def assign_split(dataset: str, group: str, seed: int) -> str:
    value = stable_number(f"{seed}:split:{dataset}:{group}") % 100
    return "train" if value < 80 else "validation" if value < 90 else "test"


def dataset_group(dataset: str, row: dict) -> str:
    if dataset == "swe_smith":
        return row["instance_id"].split(".", 1)[0].lower()
    if dataset == "mind2web":
        return str(row["website"]).strip().lower()
    if dataset == "codeactinstruct":
        parts = row["id"].split("/")
        return "/".join(parts[:4]) if len(parts) >= 4 else row["id"]
    return str(row["id"])


def obs(text: str, source: str) -> dict:
    return {
        "class_": "text_observation",
        "source": source,
        "name": None,
        "content": text,
    }


def message(text: str) -> dict:
    return {"class_": "message_action", "content": text, "description": None}


def action(function: str, kwargs: dict) -> dict:
    return {
        "class_": "api_action",
        "function": function,
        "kwargs": kwargs,
        "description": None,
    }


def trajectory(identifier: str) -> dict:
    return {
        "id": identifier,
        "content": [],
        "details": {
            "conversion": {
                "profile": "vault-paper-adp-candidate-v1",
                "lossy": False,
                "training_ready": False,
                "review_required": True,
            },
            "tool_links": [],
        },
    }


def limit_text(t: dict, text: str, limit: int) -> str:
    if len(text) > limit:
        t["details"]["conversion"]["lossy"] = True
        t["details"]["conversion"].setdefault("truncations", []).append(
            {
                "content_index": len(t["content"]),
                "original_chars": len(text),
                "kept_chars": limit,
            }
        )
        return text[:limit]
    return text


def convert_codeact(row: dict, max_observation_chars: int = 8000) -> dict:
    t = trajectory(str(row["id"]))
    pending = False
    system = []
    for item in row["conversations"]:
        role, text = item.get("role"), item.get("content")
        if not isinstance(text, str):
            raise ValueError("unsupported_content")
        if role == "system":
            system.append(text)
        elif role == "user":
            environment = text.lstrip().lower().startswith("observation:")
            if environment and not pending:
                raise ValueError("orphan_observation")
            t["content"].append(
                obs(
                    limit_text(t, text, max_observation_chars),
                    "environment" if environment else "user",
                )
            )
            if environment:
                t["details"]["tool_links"][-1]["observation_index"] = (
                    len(t["content"]) - 1
                )
                pending = False
        elif role == "assistant":
            matches = list(
                re.finditer(r"<execute>(.*?)</execute>", text, flags=re.S | re.I)
            )
            if len(matches) > 1 or (matches and pending):
                raise ValueError("ambiguous_code_actions")
            if not matches:
                t["content"].append(message(text))
                continue
            m = matches[0]
            before, after = text[: m.start()].strip(), text[m.end() :].strip()
            if before:
                t["content"].append(message(before))
            index = len(t["content"])
            t["content"].append(action("execute_python", {"code": m.group(1).strip()}))
            t["details"]["tool_links"].append({"action_index": index})
            pending = True
            if after:
                t["content"].append(message(after))
                t["details"]["conversion"]["review_notes"] = [
                    "assistant_text_after_tool_call"
                ]
        else:
            raise ValueError("unsupported_role")
    if not t["details"]["tool_links"]:
        raise ValueError("no_tool_action")
    if pending:
        t["details"]["conversion"]["review_notes"] = ["unobserved_final_action"]
    t["details"]["system_messages"] = system
    t["details"]["conversion"]["tool_mapping"] = "codeact_execute_tag_to_execute_python"
    return t


def convert_swe(row: dict, max_observation_chars: int = 8000) -> dict:
    if row.get("resolved") is not True:
        raise ValueError("not_resolved")
    t = trajectory(str(row["traj_id"]))
    links, systems = {}, []
    messages = (
        json.loads(row["messages"])
        if isinstance(row["messages"], str)
        else row["messages"]
    )
    for m in messages:
        role, text = m.get("role"), m.get("content")
        if isinstance(text, list):
            if any(
                x.get("type") != "text" or not isinstance(x.get("text"), str)
                for x in text
            ):
                raise ValueError("unsupported_content_block")
            text = "\n".join(x["text"] for x in text)
        if not isinstance(text, str):
            raise ValueError("unsupported_content")
        if role == "system":
            systems.append(text)
        elif role == "user":
            t["content"].append(obs(limit_text(t, text, max_observation_chars), "user"))
        elif role == "assistant":
            if text:
                t["content"].append(message(limit_text(t, text, max_observation_chars)))
            for call in m.get("tool_calls") or []:
                cid = call.get("id")
                fn = call.get("function", {})
                args = fn.get("arguments")
                args = json.loads(args) if isinstance(args, str) else args
                if (
                    not isinstance(cid, str)
                    or cid in links
                    or not isinstance(args, dict)
                    or not isinstance(fn.get("name"), str)
                ):
                    raise ValueError("invalid_tool_call")
                links[cid] = {"call_id": cid, "action_index": len(t["content"])}
                t["content"].append(action(fn["name"], args))
        elif role == "tool":
            ids = m.get("tool_call_ids") or (
                [m["tool_call_id"]] if m.get("tool_call_id") else []
            )
            if (
                len(ids) != 1
                or ids[0] not in links
                or "observation_index" in links[ids[0]]
            ):
                raise ValueError("ambiguous_tool_result")
            link = links[ids[0]]
            link.update(
                observation_index=len(t["content"]), is_error=m.get("is_error", False)
            )
            t["content"].append(
                obs(limit_text(t, text, max_observation_chars), "environment")
            )
        else:
            raise ValueError("unsupported_role")
    for link in links.values():
        if "observation_index" in link:
            continue
        index = link["action_index"]
        item = t["content"][index]
        if index == len(t["content"]) - 1 and item["function"] == "submit":
            link["terminal"] = True
        else:
            raise ValueError("incomplete_tool_pairs")
    if not links:
        raise ValueError("incomplete_tool_pairs")
    t["details"].update(
        tool_links=list(links.values()),
        system_messages=systems,
        source_outcome={"resolved": row["resolved"], "model": row.get("model")},
    )
    return t


def convert_agent(row: dict, max_observation_chars: int = 8000) -> dict:
    t = trajectory(str(row["id"]))
    pending = False
    source_loss = []
    for m in row["conversations"]:
        text, role = m.get("value"), m.get("from")
        if not isinstance(text, str):
            raise ValueError("unsupported_content")
        source_loss.append(m.get("loss"))
        if role == "human":
            t["content"].append(
                obs(
                    limit_text(t, text, max_observation_chars),
                    "environment" if pending else "user",
                )
            )
            pending = False
        elif role == "gpt":
            match = re.search(
                r"Act:\s*bash\s*```bash\s*(.*?)```", text, flags=re.S | re.I
            )
            if match:
                if pending:
                    raise ValueError("consecutive_unpaired_actions")
                t["content"].append(action("bash", {"command": match.group(1).strip()}))
                pending = True
            else:
                t["content"].append(message(limit_text(t, text, max_observation_chars)))
        else:
            raise ValueError("unsupported_role")
    if not any(x["class_"] == "api_action" for x in t["content"]):
        raise ValueError("no_recognized_tool_action")
    t["details"]["source_loss_flags"] = source_loss
    t["details"]["conversion"]["review_notes"] = [
        "agentinstruct_bash_only",
        "thought_text_not_preserved",
    ]
    t["details"]["conversion"]["lossy"] = True
    return t


def convert_web(row: dict, max_observation_chars: int = 8000) -> dict:
    t = trajectory(str(row["annotation_id"]))
    t["content"].append(obs(row["confirmed_task"], "user"))
    for item in row["actions"]:
        html = item.get("cleaned_html")
        operation = item.get("operation", {})
        candidates = item.get("pos_candidates") or []
        original = [c for c in candidates if c.get("is_original_target")]
        targets = original or [c for c in candidates if c.get("is_top_level_target")]
        if (
            not isinstance(html, str)
            or len(targets) != 1
            or not targets[0].get("backend_node_id")
        ):
            raise ValueError("ambiguous_web_target")
        name = str(operation.get("op", "")).lower()
        if name not in {"click", "type", "select"}:
            raise ValueError("unsupported_web_action")
        t["content"].append(
            obs(limit_text(t, html, max_observation_chars), "environment")
        )
        t["content"].append(
            action(
                name,
                {
                    "backend_node_id": str(targets[0]["backend_node_id"]),
                    "value": operation.get("value", ""),
                },
            )
        )
    if not row["actions"]:
        raise ValueError("no_web_actions")
    t["details"]["conversion"]["review_notes"] = [
        "web_observation_encoded_as_text",
        "live_page_not_verified",
    ]
    return t


def iter_records(dataset: str, raw: Path):
    if dataset == "mind2web":
        import ijson

        for path in sorted((raw / dataset / "data/train").glob("*.json")):
            with path.open("rb") as stream:
                for index, row in enumerate(ijson.items(stream, "item")):
                    yield path, index, row
    else:
        import pyarrow.parquet as pq

        for path in sorted((raw / dataset).rglob("*.parquet")):
            index = 0
            for batch in pq.ParquetFile(path).iter_batches(batch_size=16):
                for row in batch.to_pylist():
                    yield path, index, row
                    index += 1


def task_fingerprint(t: dict) -> str:
    users = [
        x["content"]
        for x in t["content"]
        if x["class_"] == "text_observation" and x["source"] == "user"
    ]
    if not users:
        raise ValueError("missing_user_context")
    value = unicodedata.normalize("NFKC", users[-1]).casefold()
    value = re.sub(r"\s+", " ", value).strip()
    return hashlib.sha256(value.encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=RAW)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-observation-chars", type=int, default=8000)
    args = parser.parse_args()
    if args.max_observation_chars < 1:
        parser.error("--max-observation-chars must be positive")
    manifest_path = args.raw_dir / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if any(
        manifest["datasets"].get(name, {}).get("status") != "complete"
        for name in QUOTAS
    ):
        raise ValueError(
            "All four source datasets must be marked complete in download manifest"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    converters = {
        "agentinstruct": convert_agent,
        "codeactinstruct": convert_codeact,
        "swe_smith": convert_swe,
        "mind2web": convert_web,
    }
    grouped = defaultdict(list)
    reasons = defaultdict(Counter)
    seen_fingerprints = {}
    duplicate_count = 0
    source_counts = Counter()
    for name in QUOTAS:
        print(f"Processing {name}...", flush=True)
        for path, index, row in iter_records(name, args.raw_dir):
            source_counts[name] += 1
            if name == "agentinstruct" and (not row["id"].startswith("os_")):
                reasons[name]["unsupported_agentinstruct_subset"] += 1
                continue
            try:
                t = converters[name](row, args.max_observation_chars)
                validate_trajectory(t)
                fingerprint = task_fingerprint(t)
                group = dataset_group(name, row)
                prior = seen_fingerprints.get(fingerprint)
                if prior:
                    duplicate_count += 1
                    reasons[name]["exact_duplicate_task"] += 1
                    continue
                seen_fingerprints[fingerprint] = (name, group)
                split = assign_split(name, group, args.seed)
                t["details"]["source"] = {
                    "dataset": name,
                    "repo": manifest["datasets"][name]["repo"],
                    "revision": manifest["datasets"][name]["revision"],
                    "relative_file": str(path.relative_to(args.raw_dir)),
                    "row_index": index,
                    "group": group,
                    "task_sha256": fingerprint,
                    "split": split,
                }
                grouped[(name, split)].append(t)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                reasons[name][str(exc)[:100]] += 1
        print(f"  source records: {source_counts[name]}", flush=True)
    stats = {}
    for name, quotas in QUOTAS.items():
        stats[name] = {
            "source_records": source_counts[name],
            "excluded": dict(reasons[name]),
            "splits": {},
        }
        for split in ("train", "validation", "test"):
            items = grouped[(name, split)]
            random.Random(f"{args.seed}:{name}:{split}").shuffle(items)
            if split == "train":
                # Keep entire groups together when allocating general and domain pools.
                by_group = defaultdict(list)
                for item in items:
                    by_group[item["details"]["source"]["group"]].append(item)
                groups = list(by_group)
                random.Random(f"{args.seed}:{name}:pools").shuffle(groups)
                pools = {"general": [], "domain": []}
                for group in groups:
                    choice = min(
                        pools,
                        key=lambda p: (
                            len(pools[p]) / max(1, quotas[0 if p == "general" else 1])
                        ),
                    )
                    limit = quotas[0 if choice == "general" else 1]
                    pools[choice].extend(
                        by_group[group][: max(0, limit - len(pools[choice]))]
                    )
                selections = pools
            else:
                selections = {split: items[:20]}
            for pool, selected in selections.items():
                path = args.output_dir / split / pool / f"{name}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as out:
                    for item in selected:
                        out.write(
                            json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n"
                        )
                stats[name]["splits"][pool] = {
                    "selected": len(selected),
                    "available": len(items),
                    "lossy": sum(
                        item["details"]["conversion"]["lossy"] for item in selected
                    ),
                    "path": str(path.relative_to(args.output_dir)),
                }
    report = {
        "version": "vault-adp-candidates-v1",
        "created_utc": datetime.now(UTC).isoformat(),
        "seed": args.seed,
        "max_observation_chars": args.max_observation_chars,
        "raw_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "split_policy": (
            "stable 80/10/10 hash by source group; "
            "repository and website groups held together"
        ),
        "duplicate_policy": (
            "exact normalized last user observation; first occurrence retained; "
            "semantic near-duplicates not checked"
        ),
        "exact_duplicates_removed": duplicate_count,
        "datasets": stats,
        "limitations": [
            "all outputs are review-only candidates, not training-ready",
            "cross-source partial or semantic duplicate leakage not ruled out",
            "text observations may be truncated; see per-record metadata",
            "agentinstruct only os/bash parsed; other subsets excluded",
            "tool schemas missing unless supplied separately",
            "web HTML is represented as text and may lack action outcome",
        ],
    }
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(stats, indent=2, ensure_ascii=False), flush=True)
    print(f"Output: {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
