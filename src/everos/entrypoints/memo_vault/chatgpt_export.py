"""Convert a ChatGPT account export ZIP into a source-traceable UMIF package."""

from __future__ import annotations

import hashlib
import io
import json
import math
import mimetypes
import stat
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from everos.umif.canonical import content_hash, record_id
from everos.umif.models import Asset, Document, Manifest, Message, Principal, Session, ToolCall, ToolResult, UmifPackage
from everos.umif.package import seal_package

from .import_contracts import ImportWorkflowError

PARSER_VERSION = "chatgpt-export-1"
_BLOCKED_ARCHIVES = {".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz"}


def _timestamp(value: object) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return None, None
    try:
        instant = datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None, None
    return int(instant.timestamp() * 1000), instant.isoformat()


def _branch(mapping: dict[str, Any], current: object) -> set[str] | None:
    if not isinstance(current, str) or current not in mapping:
        return None
    result: set[str] = set()
    node_id: str | None = current
    while node_id is not None:
        if node_id in result:
            return None
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            return None
        result.add(node_id)
        parent = node.get("parent")
        if parent is not None and not isinstance(parent, str):
            return None
        node_id = parent
    return result


def convert_chatgpt_export(
    payload: bytes,
    filename: str,
    project_id: str,
    *,
    max_unpacked_bytes: int,
    max_files: int,
    max_compression_ratio: int,
) -> UmifPackage:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Invalid ChatGPT ZIP archive.") from exc
    members: dict[str, bytes] = {}
    seen_names: set[str] = set()
    total = 0
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > max_files:
            raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "ZIP entry count exceeds the limit.")
        for info in infos:
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if (path.is_absolute() or not info.filename or "\\" in info.filename
                    or ":" in info.filename or ".." in path.parts or len(path.parts) > 12
                    or stat.S_ISLNK(mode) or (kind and kind not in {stat.S_IFREG, stat.S_IFDIR})
                    or path.as_posix() in seen_names):
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Unsafe or duplicate ZIP member path.")
            seen_names.add(path.as_posix())
            if info.is_dir():
                continue
            if path.suffix.lower() in _BLOCKED_ARCHIVES:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Nested archive is not allowed.")
            if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Unsupported ZIP member encoding.")
            total += info.file_size
            if total > max_unpacked_bytes or (info.file_size > 1024 * 1024 and
                    info.file_size / max(info.compress_size, 1) > max_compression_ratio):
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "ZIP resource limit exceeded.", status_code=413)
            try:
                with archive.open(info) as stream:
                    data = stream.read(max_unpacked_bytes + 1)
            except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as exc:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "ZIP member cannot be decoded.") from exc
            if len(data) != info.file_size:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "ZIP member size mismatch.")
            members[path.as_posix()] = data

    manifest: dict[str, Any] = {}
    if "export_manifest.json" in members:
        try:
            parsed_manifest = json.loads(members["export_manifest.json"])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportWorkflowError("CHATGPT_SCHEMA_UNSUPPORTED", "Invalid export_manifest.json.") from exc
        if isinstance(parsed_manifest, dict):
            manifest = parsed_manifest

    def logical_files(name: str) -> list[str]:
        logical = manifest.get("logical_files")
        entry = logical.get(name) if isinstance(logical, dict) else None
        paths = entry.get("files") if isinstance(entry, dict) else None
        if isinstance(paths, list) and paths and all(isinstance(path, str) for path in paths):
            return paths
        return [name] if name in members else []

    conversation_sources: list[tuple[dict[str, Any], str, int]] = []
    for member_name in logical_files("conversations.json"):
        raw = members.get(member_name)
        if raw is None:
            raise ImportWorkflowError("CHATGPT_SCHEMA_UNSUPPORTED", "A listed conversations part is missing.")
        try:
            rows = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportWorkflowError("CHATGPT_SCHEMA_UNSUPPORTED", "Invalid conversations JSON part.") from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ImportWorkflowError("CHATGPT_SCHEMA_UNSUPPORTED", "Unsupported conversation list.")
        conversation_sources.extend((row, member_name, index) for index, row in enumerate(rows))
    if not logical_files("conversations.json"):
        raise ImportWorkflowError("CHATGPT_SCHEMA_UNSUPPORTED", "ChatGPT ZIP has no conversations data.")

    digest = hashlib.sha256(payload).hexdigest()
    try:
        owner = json.loads(members.get("user.json", b"{}"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        owner = {}
    owner_id = owner.get("id") if isinstance(owner, dict) else None
    owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest() if isinstance(owner_id, str) and owner_id else digest
    provider_key = "chatgpt:" + owner_key
    scope = {"workspace_id": "chatgpt", "project_id": project_id}
    principals = tuple(
        Principal(record_id=record_id(provider_key, "principal", role),
                  source={"provider": "chatgpt", "native_id": role}, scope=scope,
                  sequence=index, provenance={"content_sha256": content_hash(body)}, payload=body)
        for index, (role, body) in enumerate((
            ("user", {"kind": "user", "name": "user"}),
            ("assistant", {"kind": "assistant", "name": "assistant"}),
        ))
    )
    principal_ids = {row.payload["kind"]: row.record_id for row in principals}
    sessions: list[Session] = []
    events: list[Message | ToolCall | ToolResult] = []
    asset_sources: dict[str, dict[str, Any]] = {}
    skipped: Counter[str] = Counter()
    warnings: list[str] = []
    for ci, (conversation, member_name, source_index) in enumerate(conversation_sources):
        native = conversation.get("conversation_id") or conversation.get("id")
        mapping = conversation.get("mapping")
        if not isinstance(native, str) or not native or not isinstance(mapping, dict):
            raise ImportWorkflowError("CHATGPT_SCHEMA_UNSUPPORTED", "Conversation ID or mapping is invalid.")
        main = _branch(mapping, conversation.get("current_node"))
        if main is None:
            warnings.append(f"Conversation {ci + 1} has an incomplete current branch; its messages are archived but not extracted.")
        session_id = record_id(provider_key, "session", native)
        session_body = {"native_session_id": native, "title": conversation.get("title") or "Untitled",
                        "current_node": conversation.get("current_node"),
                        "is_archived": bool(conversation.get("is_archived")),
                        "is_do_not_remember": bool(conversation.get("is_do_not_remember"))}
        ms, occurred = _timestamp(conversation.get("create_time"))
        sessions.append(Session(
            record_id=session_id, source={"provider": "chatgpt", "native_id": native},
            scope=scope, sequence=ci, timestamp_ms=ms, occurred_at=occurred,
            provenance={"content_sha256": content_hash(session_body), "archive_name": filename,
                        "member_path": member_name, "json_pointer": f"/{source_index}"}, payload=session_body,
        ))
        rows: list[tuple[float, str, str, str, str, object, bool]] = []
        tool_rows: list[tuple[float, str, str, str, str, object, str]] = []
        calls_by_node: dict[str, str] = {}
        for node_id, node in mapping.items():
            if not isinstance(node_id, str) or not isinstance(node, dict):
                skipped["invalid_node"] += 1
                continue
            message = node.get("message")
            if not isinstance(message, dict):
                skipped["empty_node"] += 1
                continue
            metadata = message.get("metadata")
            if isinstance(metadata, dict):
                for attachment in metadata.get("attachments") or []:
                    if isinstance(attachment, dict):
                        path = next((attachment[key] for key in ("path", "relative_path", "file_path")
                                     if isinstance(attachment.get(key), str)), None)
                        if path:
                            asset_sources[path] = {"filename": attachment.get("name") or attachment.get("filename"),
                                                   "source_project_id": None}
            author = message.get("author")
            role = author.get("role") if isinstance(author, dict) else None
            content = message.get("content")
            content_type = content.get("content_type") if isinstance(content, dict) else None
            recipient = message.get("recipient")
            parts = content.get("parts") if isinstance(content, dict) else None
            text = "\n".join(part for part in parts if isinstance(part, str) and part).strip() if isinstance(parts, list) else ""
            when = message.get("create_time")
            sort_time = float(when) if isinstance(when, (int, float)) and not isinstance(when, bool) and math.isfinite(when) else float("inf")
            message_id = message.get("id") or node_id
            if role == "assistant" and isinstance(recipient, str) and recipient not in {"", "all"}:
                if content_type == "text" and text and isinstance(message_id, str):
                    calls_by_node[node_id] = message_id
                    tool_rows.append((sort_time, node_id, message_id, "call", text, when, recipient))
                else:
                    skipped["unsupported_tool_call"] += 1
                continue
            if role == "tool" or (isinstance(author, dict) and role not in {"user", "assistant"} and author.get("name")):
                parent = node.get("parent")
                if content_type == "text" and text and isinstance(message_id, str) and isinstance(parent, str):
                    tool_rows.append((sort_time, node_id, message_id, "result", text, when, parent))
                else:
                    skipped["unsupported_tool_result"] += 1
                continue
            if role not in {"user", "assistant"}:
                skipped["non_user_assistant"] += 1
                continue
            if content_type != "text":
                skipped[str(content_type or "unknown_content")] += 1
                continue
            if not isinstance(parts, list):
                skipped["unsupported_text_parts"] += 1
                continue
            skipped["non_text_parts"] += sum(not isinstance(part, str) for part in parts)
            if not text:
                skipped["empty_text"] += 1
                continue
            if not isinstance(message_id, str):
                skipped["invalid_message_id"] += 1
                continue
            rows.append((sort_time, node_id, message_id, role, text, when, bool(main and node_id in main)))
        # Source timestamps establish order for side branches; the active path keeps parent order.
        ordered_main = list(reversed(_ordered_path(mapping, conversation.get("current_node")))) if main else []
        rank = {node_id: index for index, node_id in enumerate(ordered_main)}
        rows.sort(key=lambda row: (0, rank[row[1]]) if row[1] in rank else (1, row[0], row[1]))
        for sequence, (_, node_id, message_id, role, text, when, on_main) in enumerate(rows):
            body = {"role": role, "content_parts": [{"type": "text", "text": text}],
                    "author_principal_id": principal_ids[role]}
            ms, occurred = _timestamp(when)
            allow_memory = on_main and not conversation.get("is_do_not_remember")
            events.append(Message(
                record_id=record_id(provider_key, "event.message", native + ":" + message_id),
                source={"provider": "chatgpt", "native_id": native + ":" + message_id,
                        "node_id": node_id, "parent_node_id": mapping[node_id].get("parent")},
                scope={**scope, "session_id": session_id}, sequence=sequence,
                timestamp_ms=ms, occurred_at=occurred,
                provenance={"content_sha256": content_hash(body), "archive_name": filename,
                            "member_path": member_name, "json_pointer": f"/{source_index}/mapping/{_pointer(node_id)}/message"},
                routing_hints={"user_memory": "allow" if allow_memory else "deny"},
                extensions={"chatgpt": {"branch": "current" if on_main else "other"}},
                payload=body,
            ))
            if not allow_memory:
                skipped["not_memory_input"] += 1
        for _, node_id, message_id, event_kind, text, when, target in sorted(tool_rows, key=lambda row: (row[0], row[1])):
            ms, occurred = _timestamp(when)
            event_scope = {**scope, "session_id": session_id}
            if event_kind == "call":
                body = {"call_id": message_id, "tool_name": target, "arguments": {"text": text}}
                cls, kind = ToolCall, "event.tool_call"
            else:
                call_id = calls_by_node.get(target)
                if call_id is None:
                    skipped["orphan_tool_result"] += 1
                    continue
                body = {"call_id": call_id, "content_parts": [{"type": "text", "text": text}], "is_error": False}
                cls, kind = ToolResult, "event.tool_result"
            events.append(cls(
                record_id=record_id(provider_key, kind, native + ":" + message_id),
                source={"provider": "chatgpt", "native_id": native + ":" + message_id, "node_id": node_id},
                scope=event_scope, sequence=len(rows) + sum(e.scope.get("session_id") == session_id and e.record_type != "event.message" for e in events),
                timestamp_ms=ms, occurred_at=occurred,
                provenance={"content_sha256": content_hash(body), "archive_name": filename,
                            "member_path": member_name, "json_pointer": f"/{source_index}/mapping/{_pointer(node_id)}/message"},
                payload=body,
            ))

    for library_member in logical_files("library_files.json"):
        raw_library = members.get(library_member)
        if raw_library is None:
            raise ImportWorkflowError("CHATGPT_SCHEMA_UNSUPPORTED", "A listed library files part is missing.")
        try:
            library_files = json.loads(raw_library)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportWorkflowError("CHATGPT_SCHEMA_UNSUPPORTED", "Invalid library_files.json.") from exc
        if isinstance(library_files, list):
            for row in library_files:
                if not isinstance(row, dict):
                    continue
                path = next((row[key] for key in ("path", "relative_path", "file_path")
                             if isinstance(row.get(key), str)), None)
                if path:
                    asset_sources[path] = {"filename": row.get("name") or row.get("filename"),
                                           "source_project_id": row.get("project_id")}

    assets: list[Asset] = []
    documents: list[Document] = []
    blobs: dict[str, bytes] = {}
    for source_path, info in sorted(asset_sources.items()):
        raw = members.get(source_path)
        if raw is None:
            skipped["unresolved_attachment"] += 1
            warnings.append("An attachment reference has no exact file in the export ZIP; it remains in raw evidence.")
            continue
        title = info.get("filename")
        if not isinstance(title, str) or not title:
            title = PurePosixPath(source_path).name
        title = PurePosixPath(title).name or "attachment.bin"
        digest_file = hashlib.sha256(raw).hexdigest()
        blob_path = f"blobs/sha256/{digest_file[:2]}/{digest_file}"
        blobs[blob_path] = raw
        media = mimetypes.guess_type(title)[0] or "application/octet-stream"
        asset_body = {"filename": title, "blob_path": blob_path, "sha256": "sha256:" + digest_file,
                      "size_bytes": len(raw), "media_type": media}
        source = {"provider": "chatgpt", "native_id": source_path}
        origin = {"archive_name": filename, "member_path": source_path}
        asset_id = record_id(provider_key, "asset", source_path)
        assets.append(Asset(record_id=asset_id, source=source, scope=scope, sequence=len(assets),
                            provenance={**origin, "content_sha256": content_hash(asset_body)}, payload=asset_body))
        text = raw.decode("utf-8", errors="replace") if media.startswith("text/") and len(raw) <= 2_000_000 else ""
        document_body = {"title": title, "text": text, "kind": "attachment", "asset_id": asset_id,
                         "source_project_id": info.get("source_project_id")}
        documents.append(Document(record_id=record_id(provider_key, "document", source_path),
                                  source=source, scope=scope, sequence=len(documents),
                                  provenance={**origin, "content_sha256": content_hash(document_body)},
                                  payload=document_body))

    identity = sorted((r.record_id, r.provenance["content_sha256"]) for r in (*principals, *sessions, *events, *assets, *documents))
    manifest = Manifest(
        package_id="pkg_" + content_hash([project_id, identity]).split(":")[1],
        generator={"name": "everos-chatgpt", "version": PARSER_VERSION},
        source_exports=[{"provider": "chatgpt", "archive_name": filename,
                         "archive_sha256": "sha256:" + digest}],
        default_scope=scope, files=[],
    )
    return seal_package(UmifPackage(
        manifest=manifest, principals=principals, sessions=tuple(sessions), events=tuple(events),
        optional_records={"assets.jsonl": tuple(assets), "documents.jsonl": tuple(documents)}, blobs=blobs,
        import_report={"warnings": warnings, "skipped_event_counts": dict(skipped),
                       "counts": {"sessions": len(sessions), "messages": sum(isinstance(e, Message) for e in events),
                                  "tool_calls": sum(isinstance(e, ToolCall) for e in events),
                                  "tool_results": sum(isinstance(e, ToolResult) for e in events),
                                  "memory_input_messages": sum(isinstance(e, Message) and e.routing_hints["user_memory"] == "allow" for e in events),
                                  "documents": len(documents), "assets": len(assets)},
                       "retained_source_files": sorted(members)},
    ))


def _ordered_path(mapping: dict[str, Any], current: object) -> list[str]:
    path: list[str] = []
    while isinstance(current, str) and current in mapping:
        path.append(current)
        current = mapping[current].get("parent")
    return path


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
