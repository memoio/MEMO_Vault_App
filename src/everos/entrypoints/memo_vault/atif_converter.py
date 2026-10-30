#!/usr/bin/env python3
"""Convert complete DeepSeek Harness v4 sessions to ATIF-v1.7."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .adp_converter import arguments, blocks_text, sha256
from .adp_converter import convert as audit_source

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "ATIF-v1.7"
SCHEMA_SOURCE = "https://github.com/neulab/agent-data-protocol/blob/main/schema/atif.py"
OUTPUT_NAMES = (
    "session.atif.jsonl",
    "session.atif.pretty.json",
    "atif_source_map.json",
    "atif_conversion_report.json",
)


def location(row: dict, line: int) -> dict:
    return {
        "line": line,
        "seq": row.get("seq"),
        "time": row.get("time"),
        "type": row["type"],
        "turn": row.get("data", {}).get("turn"),
        "harness_step": row.get("data", {}).get("step"),
        "event_metadata": {
            k: deepcopy(v)
            for k, v in row.items()
            if k not in {"type", "seq", "time", "data"}
        },
    }


def timestamp(milliseconds: int) -> str:
    if type(milliseconds) is not int:
        raise ValueError("event time must be integer Unix milliseconds")
    value = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def metrics_from_usage(usage: dict) -> dict:
    """ATIF prompt_tokens include cache hits; retain raw counters for auditing."""
    result = {"extra": {"raw_usage": deepcopy(usage)}}
    for key in [
        "inputTokens",
        "outputTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
        "totalTokens",
    ]:
        if key in usage and (type(usage[key]) is not int or usage[key] < 0):
            raise ValueError(f"invalid usage counter: {key}")
    if "outputTokens" in usage:
        result["completion_tokens"] = usage["outputTokens"]
    if "cacheReadTokens" in usage:
        result["cached_tokens"] = usage["cacheReadTokens"]
    if "totalTokens" in usage and "outputTokens" in usage:
        # In this export totalTokens = uncached input + cache hits + output.
        result["prompt_tokens"] = usage["totalTokens"] - usage["outputTokens"]
    elif (
        "inputTokens" in usage
        and not usage.get("cacheReadTokens")
        and not usage.get("cacheWriteTokens")
    ):
        result["prompt_tokens"] = usage["inputTokens"]
    # Without totalTokens and with cache activity, raw counters remain available;
    # avoid guessing whether inputTokens already includes cache hits.
    if "prompt_tokens" in result and result["prompt_tokens"] < result.get(
        "cached_tokens", 0
    ):
        raise ValueError("prompt token count cannot be negative or below cached tokens")
    return result


def validate_trajectory(trajectory: dict) -> None:
    schema = json.loads((ROOT / "atif_schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(trajectory)
    seen = set()
    for expected_id, step in enumerate(trajectory["steps"], 1):
        if step["step_id"] != expected_id:
            raise ValueError("ATIF step_id must be consecutive from 1")
        if step["source"] != "agent" and any(
            k in step
            for k in (
                "tool_calls",
                "reasoning_content",
                "metrics",
                "model_name",
                "reasoning_effort",
            )
        ):
            raise ValueError("agent fields cannot appear on non-agent steps")
        local_ids = set()
        for call in step.get("tool_calls", []):
            cid = call["tool_call_id"]
            if cid in seen:
                raise ValueError(f"duplicate tool_call_id: {cid}")
            seen.add(cid)
            local_ids.add(cid)
        result_ids = [
            r["source_call_id"] for r in step.get("observation", {}).get("results", [])
        ]
        if len(result_ids) != len(set(result_ids)) or set(result_ids) != local_ids:
            raise ValueError(
                "tool results must link exactly once to calls in the same step"
            )
        if step.get("llm_call_count") == 0 and any(
            k in step for k in ("reasoning_content", "metrics")
        ):
            raise ValueError("deterministic dispatch cannot carry reasoning or metrics")


def convert(
    rows: list[dict], source_info: dict | None = None
) -> tuple[dict, dict, dict]:
    """Build ATIF directly from raw events; reuse existing source consistency checks."""
    audit, old_map, old_report = audit_source(rows, source_info)
    details = audit["details"]
    lines = (source_info or {}).get("line_numbers", list(range(1, len(rows) + 1)))
    trajectory = {
        "schema_version": SCHEMA_VERSION,
        "session_id": rows[0]["id"],
        "trajectory_id": rows[0]["id"],
        "agent": {
            "name": "deepseek-harness",
            "version": "unknown",
            "extra": {"session_format_version": rows[0]["version"]},
        },
        "steps": [],
        "extra": {
            "session": deepcopy(rows[0]),
            "metadata_events": deepcopy(details["metadata_events"]),
            "conversion": {
                "converter": "everos.memo_vault.atif_converter",
                "converter_version": "1.0.0",
                "schema_version": SCHEMA_VERSION,
                "schema_reference": SCHEMA_SOURCE,
                "schema_file_sha256": sha256((ROOT / "atif_schema.json").read_bytes()),
                "validation_scope": "local text-output subset plus semantic checks",
                "source": deepcopy(old_report["source"]),
                "history_policy": "keep all exported history; "
                "do not infer per-step copied-context flags",
            },
        },
    }
    steps = trajectory["steps"]
    # Source events retain chronology; grouped observations retain their own times.
    event_steps = {}
    calls_to_steps = {}
    audit_steps = {s["line"]: s for s in details["assistant_steps"]}
    result_metadata = {link["call_id"]: link for link in details["tool_links"]}
    selection = {}
    for line, row in zip(lines, rows, strict=True):
        kind, data = row["type"], row.get("data", {})
        if kind == "model/selection":
            selection = data
        if kind in {"system/message", "user/message", "assistant/message"}:
            source = {
                "system/message": "system",
                "user/message": "user",
                "assistant/message": "agent",
            }[kind]
            message = data if kind == "user/message" else data["message"]
            blocks = message["content"]
            step = {
                "step_id": len(steps) + 1,
                "source": source,
                "message": "",
                "extra": {
                    "source": location(row, line),
                    "message_metadata": {
                        k: deepcopy(v) for k, v in message.items() if k != "content"
                    },
                },
            }
            if "time" in row:
                step["timestamp"] = timestamp(row["time"])
            if source == "agent":
                original = audit_steps[line]
                step["message"] = "".join(
                    b["text"] for b in blocks if b["type"] == "text"
                )
                reasonings = original["reasoning_blocks"]
                if reasonings:
                    step["reasoning_content"] = "\n\n".join(
                        b["text"] for b in reasonings
                    )
                step["llm_call_count"] = 1
                step["extra"].update(
                    {
                        "reasoning_blocks": deepcopy(reasonings),
                        "block_order": [
                            {
                                "block_index": i,
                                "type": b["type"],
                                "metadata": {
                                    k: deepcopy(v)
                                    for k, v in b.items()
                                    if k not in {"text", "arguments"}
                                },
                            }
                            for i, b in enumerate(blocks)
                        ],
                        "event_metadata": deepcopy(original["metadata"]),
                        "stream": deepcopy(original["stream"]),
                    }
                )
                model = message.get("source", {}).get("model") or selection.get("model")
                if model:
                    step["model_name"] = model
                    trajectory["agent"].setdefault("model_name", model)
                if selection.get("reasoningEffort") is not None:
                    step["reasoning_effort"] = selection["reasoningEffort"]
                if "usage" in data:
                    step["metrics"] = metrics_from_usage(data["usage"])
                for block in blocks:
                    if block["type"] == "tool-call":
                        calls_to_steps[block["id"]] = step
            else:
                step["message"] = blocks_text(blocks, line)
                step["extra"]["original_blocks"] = deepcopy(blocks)
                step["extra"]["event_metadata"] = {
                    k: deepcopy(v)
                    for k, v in data.items()
                    if k not in {"message", "content"}
                }
            steps.append(step)
            event_steps[line] = step["step_id"]
        elif kind == "tool/call":
            step = calls_to_steps[data["callId"]]
            step.setdefault("tool_calls", []).append(
                {
                    "tool_call_id": data["callId"],
                    "function_name": data["name"],
                    "arguments": arguments(data["arguments"], line),
                    "extra": {
                        "source": location(row, line),
                        "raw_arguments": data["arguments"],
                        "event_metadata": {
                            k: deepcopy(v) for k, v in data.items() if k != "arguments"
                        },
                    },
                }
            )
            event_steps[line] = step["step_id"]
        elif kind == "tool/result":
            message = data["message"]
            cid = message["toolCallId"]
            step = calls_to_steps[cid]
            link = result_metadata[cid]
            step.setdefault("observation", {"results": []})["results"].append(
                {
                    "source_call_id": cid,
                    "content": blocks_text(message["content"], line),
                    "extra": {
                        "source": location(row, line),
                        "is_error": link["is_error"],
                        "event_metadata": deepcopy(link["result_metadata"]),
                        "message_metadata": deepcopy(link["result_message_metadata"]),
                        "block_metadata": deepcopy(link["result_block_metadata"]),
                    },
                }
            )
            event_steps[line] = step["step_id"]
    trajectory["final_metrics"] = {"total_steps": len(steps)}
    validate_trajectory(trajectory)
    source_map = {
        "schema_version": SCHEMA_VERSION,
        "trajectory_id": trajectory["trajectory_id"],
        "source": deepcopy(old_map["source"]),
        "events": [
            {
                **{k: v for k, v in e.items() if k != "content_indices"},
                "step_id": event_steps.get(e["line"]),
            }
            for e in old_map["events"]
        ],
    }
    report = {
        k: deepcopy(v)
        for k, v in old_report.items()
        if k not in {"profile", "content_items", "content_counts", "notes", "checks"}
    }
    report.update(
        {
            "schema_version": SCHEMA_VERSION,
            "steps": len(steps),
            "step_counts": dict(Counter(s["source"] for s in steps)),
            "checks": {
                "local_schema": True,
                "sequential_steps": True,
                "call_result_links": True,
                "source_event_consistency": True,
                "all_records_classified": True,
            },
            "notes": [
                "Local ATIF-v1.7 text-output schema; not a full upstream validator.",
                "One session, one trajectory; tool results grouped "
                "with the triggering agent step.",
                "Prompt tokens include cache hits; raw usage counters preserved.",
                "No tool renaming, inferred tool definitions, "
                "or inferred success labels.",
            ],
        }
    )
    return trajectory, source_map, report
