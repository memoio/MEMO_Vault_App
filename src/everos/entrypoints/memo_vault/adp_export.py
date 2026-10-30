"""ADP export helpers."""

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from everos.umif.models import ToolCall
from everos.umif.package import read_package
from everos.umif.validator import validate_package

from .adp_converter import convert, json_bytes, read_records, validate_trajectory
from .atif_converter import convert as convert_atif
from .atif_converter import validate_trajectory as validate_atif
from .imports import _SENSITIVE_TRACE_KEY, _SENSITIVE_TRACE_TEXT
from .storage import VaultStore


def redact_trajectory(value: Any) -> tuple[Any, int]:
    """Redact matching fields and text throughout an object without truncation."""
    result, details = redact_with_details(value)
    return result, len(details)


def redact_with_details(value: Any) -> tuple[Any, list[dict[str, str]]]:
    """Return the redacted value and masked locations, never original values."""
    details: list[dict[str, str]] = []

    def category(path: list[str]) -> str:
        if len(path) >= 3 and path[0] == "steps":
            if "tool_calls" in path:
                return "tool_arguments"
            if "observation" in path:
                return "tool_result"
            if path[2] in {"message", "reasoning_content"}:
                return "message"
        if len(path) >= 2 and path[0] == "content" and path[1].isdigit():
            items = value.get("content", []) if isinstance(value, dict) else []
            index = int(path[1])
            if (
                isinstance(items, list)
                and index < len(items)
                and isinstance(items[index], dict)
            ):
                item = items[index]
                if item.get("class_") == "api_action":
                    return "tool_arguments"
                if item.get("source") == "environment":
                    return "tool_result"
                return "message"
        return "metadata"

    def record(path: list[str], snippet: str) -> None:
        pointer = "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in path)
        details.append(
            {"category": category(path), "path": pointer, "snippet": snippet}
        )

    def visit(item: Any, key: str = "", path: list[str] | None = None) -> Any:
        path = path or []
        token_count = key in {
            "inputTokens",
            "outputTokens",
            "cacheReadTokens",
            "cacheWriteTokens",
            "totalTokens",
            "maxTokens",
            "max_tokens",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
        } and (item is None or type(item) in (int, float, bool))
        if key and _SENSITIVE_TRACE_KEY.search(key) and not token_count:
            record(path, "[REDACTED]")
            return "[REDACTED]"
        if isinstance(item, dict):
            return {k: visit(v, str(k), [*path, str(k)]) for k, v in item.items()}
        if isinstance(item, list):
            return [visit(v, path=[*path, str(i)]) for i, v in enumerate(item)]
        if isinstance(item, str):
            if item.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(item)
                except (ValueError, TypeError):
                    pass
                else:
                    if isinstance(decoded, (dict, list)):
                        before = len(details)
                        redacted = visit(decoded, path=[*path, "$json"])
                        if len(details) != before:
                            return json.dumps(
                                redacted, ensure_ascii=False, separators=(",", ":")
                            )

            matches = list(_SENSITIVE_TRACE_TEXT.finditer(item))
            if not matches:
                return item
            chunks, positions = [], []
            cursor = length = 0
            for match in matches:
                prefix = item[cursor : match.start()] + match.group(1) + match.group(2)
                chunks.append(prefix)
                length += len(prefix)
                positions.append(length)
                chunks.append("[REDACTED]")
                length += len("[REDACTED]")
                cursor = match.end()
            chunks.append(item[cursor:])
            masked = "".join(chunks)
            for pos in positions:
                start, end = max(0, pos - 45), min(len(masked), pos + 55)
                snippet = (
                    ("…" if start else "")
                    + masked[start:end]
                    + ("…" if end < len(masked) else "")
                )
                record(path, snippet)
            return masked
        return item

    result = visit(value)
    return result, details


def _trace_id(batch_id: str, session_id: str) -> str:
    return hashlib.sha256(json_bytes([batch_id, session_id])).hexdigest()


def list_traces(store: VaultStore) -> list[dict[str, Any]]:
    with store.connect() as db:
        rows = db.execute("""
            WITH sessions AS (
                SELECT batch_id, conversation_id FROM normalized_messages
                UNION SELECT batch_id, conversation_id FROM normalized_tool_events
            )
            SELECT b.id batch_id, b.source_type, b.original_filename, b.project_id,
                   b.umif_package_relpath,
                   s.conversation_id,
                   (SELECT count(*) FROM normalized_tool_events t
                    WHERE t.batch_id=s.batch_id AND t.conversation_id=s.conversation_id
                      AND t.event_type='tool/call') calls,
                   (SELECT count(*) FROM normalized_tool_events t
                    WHERE t.batch_id=s.batch_id AND t.conversation_id=s.conversation_id
                      AND t.event_type='tool/result') results
            FROM sessions s JOIN import_batches b ON b.id=s.batch_id
            WHERE b.status='archived' AND b.source_type IN ('deepseek_harness','claude')
            ORDER BY b.archived_at DESC, b.id, s.conversation_id
        """).fetchall()
        consents = {
            r["id"]: bool(r["consent"])
            for r in db.execute("SELECT id, consent FROM adp_traces")
        }
    titles: dict[tuple[str, str], str] = {}
    loaded: set[str] = set()
    for row in rows:
        if row["source_type"] != "claude" or row["batch_id"] in loaded:
            continue
        loaded.add(row["batch_id"])
        try:
            package = read_package(_safe_path(store, row["umif_package_relpath"]))
            for session in package.sessions:
                title = session.payload.get("title")
                if isinstance(title, str) and title.strip():
                    titles[(row["batch_id"], session.record_id)] = title.strip()
        except (ValueError, TypeError, OSError, KeyError):
            pass  # Listing remains available; validation reports missing evidence.
    result = []
    for row in rows:
        trace_id = _trace_id(row["batch_id"], row["conversation_id"])
        source_label = (
            "DeepSeek" if row["source_type"] == "deepseek_harness" else "Claude"
        )
        result.append(
            {
                "id": trace_id,
                "batch_id": row["batch_id"],
                "session_id": row["conversation_id"],
                "source": row["source_type"],
                "project_id": row["project_id"],
                "title": titles.get((row["batch_id"], row["conversation_id"]))
                or f"{source_label} 会话 · {row['conversation_id'][:12]}",
                "tool_call_count": row["calls"],
                "tool_result_count": row["results"],
                "consent": consents.get(trace_id, False),
                "eligible": row["calls"] > 0,
            }
        )
    return result


def set_consent(store: VaultStore, trace_ids: list[str], consent: bool) -> None:
    traces = {trace["id"]: trace for trace in list_traces(store)}
    if any(i not in traces or not traces[i]["eligible"] for i in trace_ids):
        raise ValueError("轨迹不存在或没有工具调用")
    with store.connect() as db:
        for trace_id in trace_ids:
            trace = traces[trace_id]
            db.execute(
                """INSERT INTO adp_traces(id,batch_id,payload_json,consent)
                          VALUES(?,?,?,?) ON CONFLICT(id)
                          DO UPDATE SET consent=excluded.consent""",
                (
                    trace_id,
                    trace["batch_id"],
                    json.dumps({"session_id": trace["session_id"]}),
                    int(consent),
                ),
            )


def _safe_path(store: VaultStore, relative: str) -> Path:
    root = store.data_dir.resolve()
    path = (root / relative).resolve()
    if not relative or not path.is_relative_to(root):
        raise ValueError("归档路径无效")
    return path


def _claude_raw_trajectory(conversation: dict[str, Any]) -> dict[str, Any]:
    """Map original Claude blocks in order; retain the original record for fidelity."""
    native_id = conversation.get("uuid") or conversation.get("id")
    messages = conversation.get("chat_messages", conversation.get("messages"))
    if not isinstance(native_id, str) or not isinstance(messages, list):
        raise ValueError("Claude 会话结构无效")
    content: list[dict[str, Any]] = []
    links: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Claude 消息结构无效")
        role = message.get("sender", message.get("role"))
        blocks = message.get("content")
        if blocks is None:
            blocks = [{"type": "text", "text": message.get("text", "")}]
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        if not isinstance(blocks, list):
            raise ValueError("Claude 内容结构无效")
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError("Claude 内容块无效")
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ValueError("Claude 文本块无效")
                if not text:
                    continue
                if role in ("human", "user"):
                    content.append(
                        {
                            "class_": "text_observation",
                            "source": "user",
                            "content": text,
                            "name": None,
                        }
                    )
                elif role == "assistant":
                    content.append(
                        {
                            "class_": "message_action",
                            "content": text,
                            "description": None,
                        }
                    )
            elif kind == "tool_use":
                cid, name, args = block.get("id"), block.get("name"), block.get("input")
                if (
                    not isinstance(cid, str)
                    or not cid
                    or cid in links
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(args, dict)
                ):
                    raise ValueError("工具调用的 ID、名称或参数无效")
                links[cid] = {"call_id": cid, "action_index": len(content)}
                content.append(
                    {
                        "class_": "api_action",
                        "function": name,
                        "kwargs": args,
                        "description": None,
                    }
                )
            elif kind == "tool_result":
                cid = block.get("tool_use_id")
                if cid not in links or "observation_index" in links[cid]:
                    raise ValueError("工具结果缺少对应调用或重复")
                parts = block.get("content")
                if isinstance(parts, str):
                    result_text = parts
                elif isinstance(parts, list) and all(
                    isinstance(p, dict)
                    and p.get("type") in {"text", "knowledge"}
                    and isinstance(p.get("text"), str)
                    for p in parts
                ):
                    result_text = "\n".join(p["text"] for p in parts)
                else:
                    raise ValueError("工具结果包含不支持的内容")
                links[cid].update(
                    observation_index=len(content), is_error=bool(block.get("is_error"))
                )
                content.append(
                    {
                        "class_": "text_observation",
                        "source": "environment",
                        "content": result_text,
                        "name": None,
                    }
                )
    if not links or any("observation_index" not in link for link in links.values()):
        raise ValueError("没有完整配对的工具调用和结果")
    trajectory = {
        "id": native_id,
        "content": content,
        "details": {
            "conversion": {
                "profile": "claude-source-paper-adp-v1",
                "schema_scope": "local paper-era subset",
            },
            "tool_links": list(links.values()),
            "source_conversation": conversation,
        },
    }
    validate_trajectory(trajectory)
    return trajectory


def _claude_source(
    store: VaultStore, batch: Any, package: Any, session_id: str
) -> dict[str, Any]:
    session = next((s for s in package.sessions if s.record_id == session_id), None)
    if session is None:
        raise ValueError("会话在 UMIF 中不存在")
    provenance = session.provenance
    archive_name, member = provenance.get("archive_name"), provenance.get("member_path")
    if (
        not isinstance(archive_name, str)
        or Path(archive_name).name != archive_name
        or not isinstance(member, str)
    ):
        raise ValueError("Claude 来源位置无效")
    source = next(
        (
            s
            for s in package.manifest.source_exports
            if s.get("archive_name") == archive_name
        ),
        None,
    )
    if not source or not isinstance(source.get("archive_sha256"), str):
        raise ValueError("Claude 来源哈希缺失")
    base = _safe_path(store, batch["raw_relpath"])
    path = (
        base
        if base.name == archive_name
        else (base.parent / "source" / archive_name).resolve()
    )
    if not path.is_relative_to(store.data_dir.resolve()) or not path.is_file():
        raise ValueError("Claude 原始归档缺失")
    if (
        path.stat().st_size > 256 * 1024 * 1024
        or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        != source["archive_sha256"]
    ):
        raise ValueError("Claude 来源归档过大或哈希不匹配")
    with zipfile.ZipFile(path) as archive:
        members = [
            m for m in archive.infolist() if m.filename == member and not m.is_dir()
        ]
        if len(members) != 1 or members[0].file_size > 256 * 1024 * 1024:
            raise ValueError("Claude 来源成员无效或过大")
        obj = json.loads(archive.read(members[0]))
    pointer = provenance.get("json_pointer", "")
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("Claude 来源指针无效")
    for part in pointer.split("/")[1:] if pointer else []:
        part = part.replace("~1", "/").replace("~0", "~")
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    if not isinstance(obj, dict) or (
        obj.get("uuid") or obj.get("id")
    ) != session.payload.get("native_session_id"):
        raise ValueError("Claude 来源会话不一致")
    trajectory = _claude_raw_trajectory(obj)
    expected = {
        e.payload.get("call_id")
        for e in package.events
        if isinstance(e, ToolCall) and e.scope.get("session_id") == session_id
    }
    actual = {link["call_id"] for link in trajectory["details"]["tool_links"]}
    if actual != expected:
        raise ValueError("Claude 来源与已归档工具调用不一致")
    return trajectory


def _claude_atif(adp: dict[str, Any]) -> dict[str, Any]:
    """Group Claude source calls and their observations without inventing actions."""
    steps: list[dict[str, Any]] = []
    links = adp["details"]["tool_links"]
    actions = {link["action_index"]: link for link in links}
    observations = {link["observation_index"]: link for link in links}
    pending: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        calls = {call["tool_call_id"] for call in pending["tool_calls"]}
        results = {
            r["source_call_id"]
            for r in pending.get("observation", {}).get("results", [])
        }
        if calls != results:
            raise ValueError("Claude 工具调用和结果无法在同一步骤中配对")
        steps.append(pending)
        pending = None

    for index, item in enumerate(adp["content"]):
        kind = item["class_"]
        if kind == "api_action":
            if pending and pending.get("observation"):
                flush()
            if pending is None:
                pending = {
                    "step_id": len(steps) + 1,
                    "source": "agent",
                    "message": "",
                    "extra": {},
                    "tool_calls": [],
                }
            link = actions[index]
            pending["tool_calls"].append(
                {
                    "tool_call_id": link["call_id"],
                    "function_name": item["function"],
                    "arguments": item["kwargs"],
                    "extra": {},
                }
            )
        elif kind == "text_observation" and item["source"] == "environment":
            if pending is None or index not in observations:
                raise ValueError("Claude 工具结果没有相应调用")
            link = observations[index]
            pending.setdefault("observation", {"results": []})["results"].append(
                {
                    "source_call_id": link["call_id"],
                    "content": item["content"],
                    "extra": {"is_error": link.get("is_error")},
                }
            )
        else:
            flush()
            source = "agent" if kind == "message_action" else "user"
            steps.append(
                {
                    "step_id": len(steps) + 1,
                    "source": source,
                    "message": item["content"],
                    "extra": {},
                }
            )
    flush()
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": adp["id"],
        "trajectory_id": adp["id"],
        "agent": {"name": "claude", "version": "unknown"},
        "steps": steps,
        "final_metrics": {"total_steps": len(steps)},
        "extra": {
            "conversion": {
                "profile": "claude-source-atif-v1",
                "schema_scope": "local text-output subset",
            },
            "source_conversation": adp["details"]["source_conversation"],
        },
    }
    validate_atif(trajectory)
    return trajectory


def prepare(
    store: VaultStore, trace_ids: list[str], format: str = "adp"
) -> dict[str, Any]:
    if not trace_ids or len(trace_ids) > 100 or len(set(trace_ids)) != len(trace_ids):
        raise ValueError("请选择 1 至 100 条不同轨迹")
    available = {t["id"]: t for t in list_traces(store)}
    if any(i not in available for i in trace_ids):
        raise ValueError("所选轨迹不存在")
    traces = []
    for trace_id in trace_ids:
        trace = available[trace_id]
        try:
            if not trace["eligible"]:
                raise ValueError("没有真实工具调用")
            with store.connect() as db:
                batch = db.execute(
                    "SELECT * FROM import_batches WHERE id=?", (trace["batch_id"],)
                ).fetchone()
            if trace["source"] == "deepseek_harness":
                path = _safe_path(store, batch["raw_relpath"])
                if (
                    path.stat().st_size > 256 * 1024 * 1024
                    or hashlib.sha256(path.read_bytes()).hexdigest()
                    != batch["file_sha256"]
                ):
                    raise ValueError("原始归档哈希不匹配")
                rows, info = read_records(path)
                trajectory, _, _ = (convert_atif if format == "atif" else convert)(
                    rows, info
                )
                session_id = (
                    trajectory["session_id"] if format == "atif" else trajectory["id"]
                )
                if session_id != trace["session_id"]:
                    raise ValueError("归档会话与轨迹不一致")
            else:
                path = _safe_path(store, batch["umif_package_relpath"])
                package = read_package(path)
                check = validate_package(package, path)
                if (
                    not check.valid
                    or package.manifest.package_id != batch["umif_package_id"]
                ):
                    raise ValueError("UMIF 归档校验失败")
                trajectory = _claude_source(store, batch, package, trace["session_id"])
                if format == "atif":
                    trajectory = _claude_atif(trajectory)
            trajectory["extra" if format == "atif" else "details"][
                "training_consent"
            ] = trace["consent"]
            trajectory, redactions = redact_with_details(trajectory)
            (validate_atif if format == "atif" else validate_trajectory)(trajectory)
            traces.append(
                {
                    "id": trace_id,
                    "valid": True,
                    "redaction_count": len(redactions),
                    "redactions": redactions,
                    "trajectory": trajectory,
                }
            )
        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            OSError,
            zipfile.BadZipFile,
            ValidationError,
        ) as exc:
            if isinstance(
                exc, (OSError, KeyError, IndexError, TypeError, ValidationError)
            ):
                message = "来源归档或转换后的数据无效，请检查来源文件。"
            else:
                message, _ = redact_trajectory(str(exc))
            traces.append({"id": trace_id, "valid": False, "error": message})
    valid = all(t["valid"] for t in traces)
    raw = b"".join(json_bytes(t["trajectory"]) for t in traces) if valid else b""
    return {
        "valid": valid,
        "format": format,
        "traces": traces,
        "preview_sha256": hashlib.sha256(raw).hexdigest() if valid else None,
    }
