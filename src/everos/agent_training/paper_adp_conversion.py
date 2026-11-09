#!/usr/bin/env python3
"""Convert selected source records to the five-type paper-era ADP profile.

Input: prepared_v1 selections and their pinned raw source files. Output is a
separate, untruncated paper_adp_v2 snapshot, preserving the original splits.
This is an experimental data conversion, not a Qwen training dataset.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator

from everos.agent_training.prepare_datasets import RAW, ROOT, iter_records

SELECTIONS = ROOT / ".memo-vault/agent-training/datasets/prepared_v1"
OUTPUT = ROOT / ".memo-vault/agent-training/datasets/paper_adp_v2"
UPSTREAM_REV = "ab1180306a3d4d27e9195448c90f030607eba981"


def _item_schema(kind: str, fields: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"class_": {"const": kind}, **fields},
        "required": ["class_", *required],
        "additionalProperties": False,
    }


_STRING_NULL = {"type": ["string", "null"]}
_SCHEMA = {
    "type": "object",
    "required": ["id", "content", "details"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "details": {"type": "object", "additionalProperties": {"type": "string"}},
        "content": {
            "type": "array",
            "minItems": 1,
            "items": {
                "oneOf": [
                    _item_schema(
                        "text_observation",
                        {
                            "content": {"type": "string"},
                            "name": _STRING_NULL,
                            "source": {"enum": ["user", "agent", "environment"]},
                        },
                        ["content", "name", "source"],
                    ),
                    _item_schema(
                        "message_action",
                        {
                            "content": {"type": "string"},
                            "description": _STRING_NULL,
                        },
                        ["content", "description"],
                    ),
                    _item_schema(
                        "api_action",
                        {
                            "function": {"type": "string", "minLength": 1},
                            "kwargs": {"type": "object"},
                            "description": _STRING_NULL,
                        },
                        ["function", "kwargs", "description"],
                    ),
                    _item_schema(
                        "code_action",
                        {
                            "language": {"enum": ["python", "bash"]},
                            "content": {"type": "string"},
                            "description": _STRING_NULL,
                        },
                        ["language", "content", "description"],
                    ),
                    _item_schema(
                        "web_observation",
                        {
                            "html": _STRING_NULL,
                            "axtree": _STRING_NULL,
                            "url": _STRING_NULL,
                            "image_observation": {"type": "null"},
                            "viewport_size": {"type": "null"},
                        },
                        ["html", "axtree", "url", "image_observation", "viewport_size"],
                    ),
                ]
            },
        },
    },
}
_VALIDATOR = Draft202012Validator(_SCHEMA)


def validate_paper_trajectory(trajectory: dict) -> None:
    """Validate this converter's local five-type profile, not full upstream ADP."""
    _VALIDATOR.validate(trajectory)


def _trajectory(identifier: str) -> dict:
    return {"id": identifier, "content": [], "details": {}}


def _obs(text: str, source: str) -> dict:
    return {
        "class_": "text_observation",
        "content": text,
        "name": None,
        "source": source,
    }


def _msg(text: str, description: str | None = None) -> dict:
    return {"class_": "message_action", "content": text, "description": description}


def _code(language: str, content: str, description: str | None = None) -> dict:
    return {
        "class_": "code_action",
        "language": language,
        "content": content,
        "description": description,
    }


def _api(name: str, kwargs: dict, description: str | None = None) -> dict:
    return {
        "class_": "api_action",
        "function": name,
        "kwargs": kwargs,
        "description": description,
    }


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(x, dict)
        and x.get("type") == "text"
        and isinstance(x.get("text"), str)
        for x in value
    ):
        return "\n".join(x["text"] for x in value)
    raise ValueError("unsupported_content_block")


def convert_codeact(row: dict) -> dict:
    t = _trajectory(str(row["id"]))
    system, links = [], []
    pending = None
    for item in row["conversations"]:
        role, text = item.get("role"), _text(item.get("content"))
        if role == "system":
            system.append(text)
        elif role == "user":
            is_result = text.lstrip().lower().startswith("observation:")
            index = len(t["content"])
            t["content"].append(_obs(text, "environment" if is_result else "user"))
            if is_result and pending is not None:
                links[pending]["observation_index"] = index
                pending = None
        elif role == "assistant":
            matches = list(
                re.finditer(r"<execute>(.*?)</execute>", text, flags=re.S | re.I)
            )
            if len(matches) > 1 or (matches and pending is not None):
                raise ValueError("ambiguous_code_actions")
            if not matches:
                t["content"].append(_msg(text))
                continue
            m = matches[0]
            description = text[: m.start()].strip() or None
            code = m.group(1).strip()
            if not code:
                raise ValueError("empty_code")
            links.append({"action_index": len(t["content"])})
            pending = len(links) - 1
            t["content"].append(_code("python", code, description))
            after = text[m.end() :].strip()
            if after:
                t["content"].append(_msg(after))
        else:
            raise ValueError("unsupported_role")
    if not links:
        raise ValueError("no_code_actions")
    t["details"]["system_messages_json"] = json.dumps(system, ensure_ascii=False)
    t["details"]["tool_links_json"] = json.dumps(links, ensure_ascii=False)
    return t


def convert_agentinstruct(row: dict) -> dict:
    t = _trajectory(str(row["id"]))
    pending = None
    links = []
    flags = []
    for item in row["conversations"]:
        role, text = item.get("from"), _text(item.get("value"))
        flags.append(item.get("loss"))
        if role == "human":
            index = len(t["content"])
            t["content"].append(
                _obs(text, "environment" if pending is not None else "user")
            )
            if pending is not None:
                links[pending]["observation_index"] = index
                pending = None
        elif role == "gpt":
            match = re.search(
                r"Act:\s*bash\s*```bash\s*(.*?)```", text, flags=re.S | re.I
            )
            if not match:
                t["content"].append(_msg(text))
                continue
            if pending is not None:
                raise ValueError("consecutive_unpaired_actions")
            links.append({"action_index": len(t["content"])})
            pending = len(links) - 1
            t["content"].append(
                _code(
                    "bash",
                    match.group(1).strip(),
                    text[: match.start()].strip() or None,
                )
            )
            after = text[match.end() :].strip()
            if after:
                t["content"].append(_msg(after))
        else:
            raise ValueError("unsupported_role")
    if not links:
        raise ValueError("no_bash_actions")
    t["details"]["source_loss_flags_json"] = json.dumps(flags)
    t["details"]["tool_links_json"] = json.dumps(links, ensure_ascii=False)
    return t


def convert_swe(row: dict) -> dict:
    if row.get("resolved") is not True:
        raise ValueError("not_resolved")
    t = _trajectory(str(row["traj_id"]))
    messages = (
        json.loads(row["messages"])
        if isinstance(row["messages"], str)
        else row["messages"]
    )
    systems, links = [], {}
    for item in messages:
        role, text = item.get("role"), _text(item.get("content"))
        if role == "system":
            systems.append(text)
        elif role == "user":
            t["content"].append(_obs(text, "user"))
        elif role == "assistant":
            calls = item.get("tool_calls") or []
            if not calls:
                if text:
                    t["content"].append(_msg(text))
                continue
            for i, call in enumerate(calls):
                fn = call.get("function", {})
                cid, name, args = call.get("id"), fn.get("name"), fn.get("arguments")
                args = json.loads(args) if isinstance(args, str) else args
                if (
                    not isinstance(cid, str)
                    or not cid
                    or cid in links
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(args, dict)
                ):
                    raise ValueError("invalid_tool_call")
                description = text if i == 0 and text else None
                links[cid] = {
                    "call_id": cid,
                    "action_index": len(t["content"]),
                    "source_kwargs": args,
                }
                if name == "bash" and isinstance(args.get("command"), str):
                    t["content"].append(_code("bash", args["command"], description))
                else:
                    t["content"].append(_api(name, args, description))
        elif role == "tool":
            ids = item.get("tool_call_ids") or (
                [item["tool_call_id"]] if item.get("tool_call_id") else []
            )
            if (
                len(ids) != 1
                or ids[0] not in links
                or "observation_index" in links[ids[0]]
            ):
                raise ValueError("ambiguous_tool_result")
            links[ids[0]].update(
                observation_index=len(t["content"]),
                is_error=item.get("is_error", False),
            )
            t["content"].append(_obs(text, "environment"))
        else:
            raise ValueError("unsupported_role")
    if not links:
        raise ValueError("no_tool_calls")
    for link in links.values():
        if "observation_index" not in link:
            index = link["action_index"]
            item = t["content"][index]
            if index == len(t["content"]) - 1 and item.get("function") == "submit":
                link["terminal"] = True
            else:
                raise ValueError("incomplete_tool_pairs")
    t["details"].update(
        system_messages_json=json.dumps(systems, ensure_ascii=False),
        tool_links_json=json.dumps(list(links.values()), ensure_ascii=False),
        source_outcome_json=json.dumps(
            {"resolved": row["resolved"], "model": row.get("model")}, ensure_ascii=False
        ),
    )
    return t


def convert_mind2web(row: dict) -> dict:
    t = _trajectory(str(row["annotation_id"]))
    t["content"].append(_obs(_text(row["confirmed_task"]), "user"))
    action_ids = []
    for item in row["actions"]:
        html = _text(item.get("raw_html"))
        operation = item.get("operation", {})
        candidates = item.get("pos_candidates") or []
        targets = [x for x in candidates if x.get("is_original_target")]
        targets = targets or [x for x in candidates if x.get("is_top_level_target")]
        if len(targets) != 1 or not targets[0].get("backend_node_id"):
            raise ValueError("ambiguous_web_target")
        name = str(operation.get("op", "")).lower()
        if name not in {"click", "type", "select"}:
            raise ValueError("unsupported_web_action")
        node = str(targets[0]["backend_node_id"])
        # Keep the native target ID. Browser-specific mapping is later work.
        t["content"].append(
            {
                "class_": "web_observation",
                "html": html,
                "axtree": None,
                "url": None,
                "image_observation": None,
                "viewport_size": None,
            }
        )
        t["content"].append(
            _api(name, {"backend_node_id": node, "value": operation.get("value", "")})
        )
        action_ids.append(item.get("action_uid"))
    if not row["actions"]:
        raise ValueError("no_web_actions")
    t["details"]["source_web_json"] = json.dumps(
        {
            "website": row.get("website"),
            "domain": row.get("domain"),
            "action_uids": action_ids,
            "mapping": "native_backend_node_id; not upstream xpath",
        },
        ensure_ascii=False,
    )
    return t


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=RAW)
    parser.add_argument("--selections-dir", type=Path, default=SELECTIONS)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error(
            "Output directory exists; choose a new directory to avoid overwriting"
        )
    selected = defaultdict(dict)
    selected_count = Counter()
    for path in sorted(args.selections_dir.rglob("*.jsonl")):
        for line in path.open(encoding="utf-8"):
            t = json.loads(line)
            source = t["details"]["source"]
            key = (source["relative_file"], source["row_index"])
            if key in selected[source["dataset"]]:
                raise ValueError(f"duplicate source selection: {key}")
            selected[source["dataset"]][key] = {
                "source": source,
                "output": str(path.relative_to(args.selections_dir)),
                "id": t["id"],
            }
            selected_count[source["dataset"]] += 1
    if set(selected) != {"agentinstruct", "codeactinstruct", "swe_smith", "mind2web"}:
        raise ValueError("selection must include all four datasets")
    manifest_path = args.raw_dir / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_hashes = {}
    for dataset, entries in selected.items():
        listed = {f["path"]: f for f in manifest["datasets"][dataset]["files"]}
        for relative_file in sorted({key[0] for key in entries}):
            p = args.raw_dir / relative_file
            entry = listed[str(p.relative_to(args.raw_dir / dataset))]
            actual = _sha256(p)
            if actual != entry["sha256"]:
                raise ValueError(f"raw source hash mismatch: {p}")
            raw_hashes[relative_file] = actual
    converters = {
        "agentinstruct": convert_agentinstruct,
        "codeactinstruct": convert_codeact,
        "swe_smith": convert_swe,
        "mind2web": convert_mind2web,
    }
    args.output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".paper_adp_v2-", dir=args.output_dir.parent)
    )
    stats = {
        name: {
            "selected": selected_count[name],
            "converted": 0,
            "failed": 0,
            "classes": Counter(),
            "failures": Counter(),
        }
        for name in selected
    }
    try:
        failed_records = (staging / "failed_records.jsonl").open("w", encoding="utf-8")
        with failed_records:
            for dataset, converter in converters.items():
                print(
                    f"Converting {dataset}: {selected_count[dataset]} selected",
                    flush=True,
                )
                found = set()
                for path, index, row in iter_records(dataset, args.raw_dir):
                    relative = str(path.relative_to(args.raw_dir))
                    key = (relative, index)
                    if key not in selected[dataset]:
                        continue
                    choice = selected[dataset][key]
                    found.add(key)
                    try:
                        t = converter(row)
                        if t["id"] != choice["id"]:
                            raise ValueError("source_id_mismatch")
                        t["details"]["source_json"] = json.dumps(
                            choice["source"], ensure_ascii=False, sort_keys=True
                        )
                        t["details"]["source_file_sha256"] = raw_hashes[relative]
                        t["details"]["conversion_profile"] = "vault-paper-five-type-v2"
                        t["details"]["review_required"] = "true"
                        t["details"]["training_ready"] = "false"
                        t["details"]["upstream_reference_commit"] = UPSTREAM_REV
                        validate_paper_trajectory(t)
                        out_path = staging / choice["output"]
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        with out_path.open("a", encoding="utf-8") as out:
                            out.write(
                                json.dumps(t, ensure_ascii=False, allow_nan=False)
                                + "\n"
                            )
                        stats[dataset]["converted"] += 1
                        stats[dataset]["classes"].update(
                            x["class_"] for x in t["content"]
                        )
                    except (ValueError, TypeError, KeyError) as exc:
                        stats[dataset]["failed"] += 1
                        stats[dataset]["failures"][str(exc)[:100]] += 1
                        failed_records.write(
                            json.dumps(
                                {
                                    "dataset": dataset,
                                    "source_file": relative,
                                    "row_index": index,
                                    "id": choice["id"],
                                    "error": str(exc)[:300],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                missing = set(selected[dataset]) - found
                if missing:
                    raise ValueError(
                        f"{dataset}: missing {len(missing)} selected source records"
                    )
        report = {
            "version": "vault-paper-five-type-v2",
            "created_utc": datetime.now(UTC).isoformat(),
            "paper_url": "https://arxiv.org/pdf/2510.24702",
            "upstream_reference_commit": UPSTREAM_REV,
            "selection_report_sha256": _sha256(args.selections_dir / "report.json"),
            "raw_manifest_sha256": _sha256(manifest_path),
            "raw_source_sha256": raw_hashes,
            "scope": ("Reconvert selected source records, not full raw corpora"),
            "notes": [
                "Full raw HTML and text retained without truncation; "
                "other raw fields recoverable from source.",
                "Web action uses native backend_node_id, "
                "not upstream browser-specific xpath.",
                "Unknown URLs, accessibility trees, images "
                "and viewport sizes remain null.",
                "All trajectories are review-only; "
                "no SFT conversion or training performed.",
                "Source split and prior dedup limitations are inherited unchanged.",
            ],
            "datasets": stats,
        }
        (staging / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        staging.rename(args.output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    print(f"Output: {args.output_dir}", flush=True)
    return 1 if any(x["failed"] for x in stats.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
