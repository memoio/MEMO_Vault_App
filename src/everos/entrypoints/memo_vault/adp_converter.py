#!/usr/bin/env python3
"""Deterministic offline DeepSeek Harness v4 -> paper-era ADP converter."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent
PROFILE = "deepseek-paper-adp-output-v1"
MAX_INPUT_BYTES = 256 * 1024 * 1024
METADATA_TYPES = {
    "permission/preset",
    "sandbox/mode",
    "approval/policy",
    "agent/inbox/spliced",
    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "request/header",
    "web/deepseek-search-llm-request",
    "session/end-seed",
    "session/title",
    "model/selection",
    "request/context",
    "session/title-llm-request",
    "command/run",
    "command/done",
    "session-log-deepseek/delivery-accepted",
}


def json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options = {"indent": 2} if pretty else {"separators": (",", ":")}
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, **options) + "\n"
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strict_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def bad_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=bad_constant)


def read_records(path: Path) -> tuple[list[dict], dict]:
    """Read one JSONL member in place; do not extract archive paths."""
    path = Path(path)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("input exceeds 256 MiB limit")
    source = path.read_bytes()
    member = None
    if zipfile.is_zipfile(io.BytesIO(source)):
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            members = [
                m
                for m in archive.infolist()
                if not m.is_dir() and m.filename.endswith(".jsonl")
            ]
            if len(members) != 1:
                raise ValueError("ZIP must contain exactly one JSONL file")
            if members[0].file_size > MAX_INPUT_BYTES:
                raise ValueError("uncompressed JSONL exceeds 256 MiB limit")
            member = members[0].filename
            raw = archive.read(members[0])
    else:
        raw = source
    rows, line_numbers = [], []
    # JSONL uses LF (including CRLF), not Unicode text separators inside strings.
    for line, text in enumerate(raw.decode("utf-8-sig").split("\n"), 1):
        if not text.strip():
            continue
        try:
            row = strict_json(text)
        except ValueError as exc:
            raise ValueError(f"line {line}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"line {line}: record must be an object")
        rows.append(row)
        line_numbers.append(line)
    return rows, {
        "input_name": path.name,
        "input_sha256": sha256(source),
        "member": member,
        "jsonl_sha256": sha256(raw),
        "jsonl_bytes": len(raw),
        "line_numbers": line_numbers,
    }


def validate_trajectory(trajectory: dict) -> None:
    schema = json.loads((ROOT / "adp_schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(trajectory)


def arguments(value: Any, line: int) -> dict:
    try:
        parsed = strict_json(value) if isinstance(value, str) else value
    except ValueError as exc:
        raise ValueError(f"line {line}: invalid arguments: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"line {line}: arguments must be a JSON object")
    return parsed


def blocks_text(blocks: Any, line: int) -> str:
    if not isinstance(blocks, list):
        raise ValueError(f"line {line}: content must be a block list")
    for block in blocks:
        if (
            not isinstance(block, dict)
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
        ):
            raise ValueError(f"line {line}: unsupported or invalid text block")
    return "".join(b["text"] for b in blocks)


def convert(
    rows: list[dict], source_info: dict | None = None
) -> tuple[dict, dict, dict]:
    """Return trajectory, source map and report. Reject inconsistent event links."""
    rows = deepcopy(rows)
    info = deepcopy(source_info or {})
    lines = info.pop("line_numbers", list(range(1, len(rows) + 1)))
    if len(lines) != len(rows):
        raise ValueError("line_numbers length mismatch")
    if not rows or rows[0].get("type") != "session" or rows[0].get("version") != 4:
        raise ValueError("first record must be a version 4 session")
    if not isinstance(rows[0].get("id"), str) or not rows[0]["id"]:
        raise ValueError("session id must be a nonempty string")
    previous = -1
    for line, row in zip(lines[1:], rows[1:], strict=True):
        seq = row.get("seq")
        if type(seq) is not int or seq <= previous:
            raise ValueError(f"line {line}: seq must increase in original file order")
        if row.get("type") == "session":
            raise ValueError(f"line {line}: multiple session headers")
        if not isinstance(row.get("data"), dict) or not isinstance(
            row.get("type"), str
        ):
            raise ValueError(f"line {line}: event requires type and object data")
        previous = seq

    details = {
        "conversion": {
            "profile": PROFILE,
            "converter_version": "1.0.0",
            "source": info,
            "schema_file_sha256": sha256((ROOT / "adp_schema.json").read_bytes()),
            "schema_basis": "neulab/agent-data-collection@"
            "d9c2cb280d32048d6446ee12ffb368c0e49a787e/README.md",
            "schema_scope": "locally authored paper-era output subset, "
            "not upstream schema copy",
            "description_policy": "reasoning once on first action; "
            "original blocks in assistant_steps",
        },
        "session": rows[0],
        "assistant_steps": [],
        "tool_links": [],
        "system_messages": [],
        "user_messages": [],
        "metadata_events": [],
    }
    trajectory = {"id": rows[0]["id"], "content": [], "details": details}
    source_map = {
        "trajectory_id": trajectory["id"],
        "profile": PROFILE,
        "source": info,
        "content": [],
        "events": [],
    }
    counts = Counter(r["type"] for r in rows)
    embedded, calls, results = {}, {}, {}
    unknown = set()
    assistant_steps = details["assistant_steps"]

    def add(item, line, row, **extra):
        index = len(trajectory["content"])
        trajectory["content"].append(item)
        source_map["content"].append(
            {
                "content_index": index,
                "line": line,
                "seq": row.get("seq"),
                "time": row.get("time"),
                "type": row["type"],
                **extra,
            }
        )
        return index

    def first_description(step):
        if step["content_indices"]:
            return None
        reasoning = "\n\n".join(b["text"] for b in step["reasoning_blocks"])
        return reasoning if step["reasoning_blocks"] else None

    for line, row in zip(lines, rows, strict=True):
        t = row["type"]
        data = row.get("data", {})
        event_start = len(trajectory["content"])
        classification = "metadata"
        if t == "session":
            classification = "session_header"
        elif t == "system/message":
            blocks_text(data["message"]["content"], line)
            details["system_messages"].append(
                {"line": line, "seq": row["seq"], "data": data}
            )
            classification = "system_context"
        elif t == "user/message":
            index = add(
                {
                    "class_": "text_observation",
                    "source": "user",
                    "name": None,
                    "content": blocks_text(data["content"], line),
                },
                line,
                row,
            )
            details["user_messages"].append(
                {
                    "line": line,
                    "content_index": index,
                    "metadata": {k: v for k, v in data.items() if k != "content"},
                    "original_blocks": data["content"],
                }
            )
            classification = "user_observation"
        elif t == "assistant/message":
            message = data["message"]
            blocks = message["content"]
            if not isinstance(blocks, list):
                raise ValueError(f"line {line}: assistant block list required")
            step = {
                "line": line,
                "seq": row["seq"],
                "turn": data.get("turn"),
                "step": data.get("step"),
                "metadata": {
                    k: v for k, v in data.items() if k not in {"message", "stream"}
                },
                "message_metadata": {
                    k: v for k, v in message.items() if k != "content"
                },
                "block_order": [],
                "reasoning_blocks": [],
                "content_indices": [],
                "stream": {
                    "present": "stream" in data,
                    "sha256": sha256(json_bytes(data.get("stream"))),
                },
            }
            assistant_steps.append(step)
            saw_call = False
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") not in {
                    "reasoning",
                    "text",
                    "tool-call",
                }:
                    raise ValueError(f"line {line}: unsupported assistant block")
                if block["type"] in {"reasoning", "text"} and not isinstance(
                    block.get("text"), str
                ):
                    raise ValueError(f"line {line}: block text must be a string")
                if block["type"] == "reasoning":
                    step["reasoning_blocks"].append(block)
            for bi, block in enumerate(blocks):
                bt = block["type"]
                order = {"block_index": bi, "type": bt}
                step["block_order"].append(order)
                if bt == "text":
                    if saw_call:
                        raise ValueError(
                            f"line {line}: unsupported text-after-tool block order"
                        )
                    index = add(
                        {
                            "class_": "message_action",
                            "content": block["text"],
                            "description": first_description(step),
                        },
                        line,
                        row,
                        block_index=bi,
                    )
                    step["content_indices"].append(index)
                    order["content_index"] = index
                    order["block_metadata"] = {
                        k: v for k, v in block.items() if k != "text"
                    }
                elif bt == "tool-call":
                    saw_call = True
                    cid = block.get("id")
                    if not isinstance(cid, str) or not cid or cid in embedded:
                        raise ValueError(
                            f"line {line}: duplicate or invalid embedded "
                            f"call ID {cid!r}"
                        )
                    arguments(block.get("arguments"), line)
                    embedded[cid] = {
                        "block": block,
                        "step": step,
                        "line": line,
                        "block_index": bi,
                    }
                    order["call_id"] = cid
            classification = "assistant_actions_and_reasoning"
        elif t == "tool/call":
            cid = data.get("callId")
            if not isinstance(cid, str) or not cid or cid in calls:
                raise ValueError(
                    f"line {line}: duplicate or invalid tool call ID {cid!r}"
                )
            args = arguments(data.get("arguments"), line)
            if cid not in embedded:
                raise ValueError(
                    f"line {line}: orphan tool call {cid!r}, "
                    "no preceding assistant block"
                )
            e = embedded[cid]
            block, step = e["block"], e["step"]
            # Python considers True == 1; compare JSON encodings to retain JSON types.
            embedded_args = json.dumps(
                arguments(block["arguments"], e["line"]),
                sort_keys=True,
                allow_nan=False,
            )
            event_args = json.dumps(args, sort_keys=True, allow_nan=False)
            if block.get("name") != data.get("name") or embedded_args != event_args:
                raise ValueError(
                    f"line {line}: embedded/event tool call mismatch {cid!r}"
                )
            if (step["turn"], step["step"]) != (data.get("turn"), data.get("step")):
                raise ValueError(f"line {line}: tool call step mismatch {cid!r}")
            index = add(
                {
                    "class_": "api_action",
                    "function": data["name"],
                    "kwargs": args,
                    "description": first_description(step),
                },
                line,
                row,
                call_id=cid,
                assistant_line=e["line"],
                block_index=e["block_index"],
            )
            step["content_indices"].append(index)
            link = {
                "call_id": cid,
                "action_index": index,
                "call_line": line,
                "assistant_line": e["line"],
                "assistant_block_index": e["block_index"],
                "turn": data.get("turn"),
                "step": data.get("step"),
                "raw_arguments": data["arguments"],
                "embedded_block": block,
            }
            calls[cid] = link
            details["tool_links"].append(link)
            classification = "tool_action_verified_against_assistant"
        elif t == "tool/result":
            message = data["message"]
            cid = message.get("toolCallId")
            if cid not in calls:
                raise ValueError(f"line {line}: orphan tool result {cid!r}")
            if cid in results:
                raise ValueError(f"line {line}: duplicate tool result {cid!r}")
            link = calls[cid]
            if (link["turn"], link["step"]) != (data.get("turn"), data.get("step")):
                raise ValueError(f"line {line}: tool result step mismatch {cid!r}")
            if "isError" in message and type(message["isError"]) is not bool:
                raise ValueError(f"line {line}: isError must be boolean")
            index = add(
                {
                    "class_": "text_observation",
                    "source": "environment",
                    "name": None,
                    "content": blocks_text(message["content"], line),
                },
                line,
                row,
                call_id=cid,
                action_index=link["action_index"],
            )
            link.update(
                {
                    "observation_index": index,
                    "result_line": line,
                    "is_error": message.get("isError", False),
                    "result_metadata": {
                        k: v for k, v in data.items() if k != "message"
                    },
                    "result_message_metadata": {
                        k: v for k, v in message.items() if k != "content"
                    },
                    "result_block_metadata": [
                        {k: v for k, v in b.items() if k != "text"}
                        for b in message["content"]
                    ],
                }
            )
            results[cid] = index
            classification = "tool_observation"
        else:
            details["metadata_events"].append({"line": line, "event": row})
            if t not in METADATA_TYPES:
                unknown.add(t)
                classification = "unknown_preserved"
        source_map["events"].append(
            {
                "line": line,
                "seq": row.get("seq"),
                "time": row.get("time"),
                "type": t,
                "classification": classification,
                "record_sha256": sha256(json_bytes(row)),
                "content_indices": list(range(event_start, len(trajectory["content"]))),
            }
        )

    if embedded.keys() != calls.keys():
        raise ValueError(
            f"missing tool call events: {sorted(embedded.keys() - calls.keys())}"
        )
    if calls.keys() != results.keys():
        raise ValueError(
            f"missing result for calls: {sorted(calls.keys() - results.keys())}"
        )
    validate_trajectory(trajectory)
    report = {
        "status": "validated_with_warnings" if unknown else "validated",
        "profile": PROFILE,
        "trajectory_id": trajectory["id"],
        "trajectories": 1,
        "source": info,
        "input_records": len(rows),
        "event_counts": dict(sorted(counts.items())),
        "content_items": len(trajectory["content"]),
        "content_counts": dict(
            sorted(Counter(x["class_"] for x in trajectory["content"]).items())
        ),
        "user_messages": counts["user/message"],
        "assistant_messages": counts["assistant/message"],
        "tool_calls": len(calls),
        "tool_results": len(results),
        "deduplicated_call_representations": len(embedded),
        "tool_errors": sum(c["is_error"] for c in calls.values()),
        "aborted_turns": sum(
            r["type"] == "turn/end"
            and r["data"].get("reason", {}).get("kind") == "aborted"
            for r in rows
        ),
        "reasoning_blocks": sum(len(s["reasoning_blocks"]) for s in assistant_steps),
        "seed_boundaries": counts["session/end-seed"],
        "unclassified_events": 0,
        "unknown_event_types": sorted(unknown),
        "checks": {
            "schema": True,
            "tool_pairs": True,
            "embedded_calls": True,
            "sequence_order": True,
            "all_records_classified": True,
        },
        "notes": [
            "One exported session is one trajectory; inherited history is retained.",
            "All tool interfaces retain original names, including pwsh.",
            "Stream chunks are not replayed; hashes reference the original archive.",
            "Completed turns do not imply successful task outcomes.",
            "Schema validation covers this converter's paper-era output subset, "
            "not ATIF.",
        ],
    }
    return trajectory, source_map, report
