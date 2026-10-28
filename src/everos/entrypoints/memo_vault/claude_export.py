"""Claude export adapter: source records become UMIF without writing Vault state."""

from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from everos.component.utils.datetime import from_iso_format, to_timestamp_ms
from everos.umif.canonical import content_hash, record_id
from everos.umif.models import (
    Asset,
    Document,
    Manifest,
    MemoryAssertion,
    Message,
    Principal,
    Session,
    ToolCall,
    ToolResult,
    UmifPackage,
    UmifRecord,
)
from everos.umif.package import seal_package

from .import_contracts import ImportWorkflowError

PARSER_VERSION = "claude-export-2"


def _rows(value: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(value, dict) and key in value:
        value = value[key]
    if isinstance(value, dict) and (
        "uuid" in value or "id" in value or "content" in value or "memory" in value
    ):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise ImportWorkflowError(
            "CLAUDE_SCHEMA_UNSUPPORTED", f"Unsupported Claude {key} schema."
        )
    return value


def _located_rows(value: Any, key: str) -> list[tuple[dict[str, Any], str]]:
    """Decode aggregate lists and per-record files without losing source pointers."""
    if key == "memories" and isinstance(value, dict) and "memory_files" in value:
        files = value["memory_files"]
        if not isinstance(files, list) or any(
            not isinstance(row, dict) for row in files
        ):
            raise ImportWorkflowError(
                "CLAUDE_SCHEMA_UNSUPPORTED", "Unsupported memory_files schema."
            )
        rows = []
        account = value.get("account_uuid")
        if not isinstance(account, str) or not account:
            raise ImportWorkflowError(
                "CLAUDE_SCHEMA_UNSUPPORTED", "Memory file owner is missing."
            )
        for index, item in enumerate(files):
            path = item.get("path")
            if (
                not isinstance(path, str)
                or not path
                or not isinstance(item.get("content"), str)
            ):
                raise ImportWorkflowError(
                    "CLAUDE_SCHEMA_UNSUPPORTED", "Unsupported memory file record."
                )
            rows.append(
                (
                    {
                        **item,
                        "id": "memory-file:" + account + ":" + path,
                        "account_uuid": account,
                    },
                    f"/memory_files/{index}",
                )
            )
        return rows
    wrapped = isinstance(value, dict) and key in value
    unwrapped = value[key] if wrapped else value
    prefix = "/" + key if wrapped else ""
    rows = _rows(value, key)
    return [
        (row, prefix + f"/{index}" if isinstance(unwrapped, list) else prefix)
        for index, row in enumerate(rows)
    ]


def convert_claude_export(
    archives: Sequence[Path],
    project_id: str,
    *,
    max_unpacked_bytes: int = 512 * 1024 * 1024,
    max_files: int = 10000,
    source_account_id: str | None = None,
) -> UmifPackage:
    """Resolve all parts together, retaining unsupported source data in raw evidence."""
    groups: dict[str, list[tuple[dict[str, Any], str, str, str]]] = {
        key: [] for key in ("users", "conversations", "projects", "memories")
    }
    binaries: list[tuple[str, str, bytes]] = []
    exports = []
    warnings: list[str] = []
    skipped: dict[str, int] = {}
    total = count = 0
    for path in sorted(archives, key=lambda p: p.name):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        exports.append(
            {
                "provider": "claude",
                "archive_name": path.name,
                "archive_sha256": "sha256:" + digest.hexdigest(),
            }
        )
        try:
            with zipfile.ZipFile(path) as z:
                if len(z.infolist()) + count > max_files:
                    raise ImportWorkflowError(
                        "IMPORT_SECURITY_REJECTED", "Too many ZIP members."
                    )
                seen_names = set()
                for info in z.infolist():
                    member = PurePosixPath(info.filename)
                    if (
                        member.is_absolute()
                        or ".." in member.parts
                        or "\\" in info.filename
                        or ":" in info.filename
                        or len(member.parts) > 12
                        or stat.S_ISLNK(info.external_attr >> 16)
                        or info.filename in seen_names
                    ):
                        raise ImportWorkflowError(
                            "IMPORT_SECURITY_REJECTED",
                            "Unsafe or duplicate ZIP member path.",
                        )
                    seen_names.add(info.filename)
                    if info.is_dir():
                        continue
                    count += 1
                    total += info.file_size
                    if (
                        count > max_files
                        or total > max_unpacked_bytes
                        or info.flag_bits & 1
                        or info.compress_type not in {0, 8}
                        or info.file_size > max(1, info.compress_size) * 200
                    ):
                        raise ImportWorkflowError(
                            "IMPORT_SECURITY_REJECTED",
                            "ZIP resource or compression limit exceeded.",
                        )
                    with z.open(info) as handle:
                        raw = handle.read(min(max_unpacked_bytes, info.file_size) + 1)
                    if len(raw) != info.file_size:
                        raise ImportWorkflowError(
                            "IMPORT_SECURITY_REJECTED", "ZIP member size mismatch."
                        )
                    name = member.stem.lower().replace("-", "_")
                    category = next(
                        (k for k in groups if name == k or name.startswith(k + "_")),
                        None,
                    )
                    if (
                        category is None
                        and len(member.parts) > 1
                        and member.parts[0] in groups
                    ):
                        category = member.parts[0]
                    if member.suffix.lower() == ".json" and name == "login_history":
                        skipped["account_metadata"] = (
                            skipped.get("account_metadata", 0) + 1
                        )
                        continue
                    if member.suffix.lower() == ".json" and category:
                        for row, pointer in _located_rows(json.loads(raw), category):
                            groups[category].append(
                                (row, path.name, info.filename, pointer)
                            )
                    else:
                        binaries.append((path.name, info.filename, raw))
                        if member.suffix.lower() == ".json":
                            skipped["unrecognized_json_file"] = (
                                skipped.get("unrecognized_json_file", 0) + 1
                            )
        except (
            zipfile.BadZipFile,
            RuntimeError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            raise ImportWorkflowError(
                "CLAUDE_INVALID_EXPORT", "Invalid ZIP member or JSON export."
            ) from exc
    if not any(groups.values()):
        raise ImportWorkflowError(
            "CLAUDE_SCHEMA_UNSUPPORTED", "No supported Claude export schema found."
        )
    account_ids = {
        str(r.get("uuid") or r.get("id"))
        for r, *_ in groups["users"]
        if r.get("uuid") or r.get("id")
    }
    for row, *_ in groups["conversations"]:
        source_account = row.get("account")
        if isinstance(source_account, dict) and source_account.get("uuid"):
            account_ids.add(str(source_account["uuid"]))
    for row, *_ in groups["memories"]:
        if row.get("account_uuid"):
            account_ids.add(str(row["account_uuid"]))
    if source_account_id is not None:
        if not source_account_id.strip():
            raise ImportWorkflowError(
                "CLAUDE_ACCOUNT_REQUIRED", "Export owner must not be empty."
            )
        account_ids.add(source_account_id)
    if groups["projects"] and not account_ids:
        raise ImportWorkflowError(
            "CLAUDE_ACCOUNT_REQUIRED",
            "A project-only ZIP does not identify its exporting account. "
            "Import the complete manifest with account metadata "
            "or provide verified owner context.",
        )
    account_ids = sorted(account_ids)
    if len(account_ids) > 1:
        raise ImportWorkflowError(
            "CLAUDE_MULTIPLE_ACCOUNTS", "Import one Claude account at a time."
        )
    account = account_ids[0] if account_ids else "local-owner"
    if not account_ids:
        warnings.append(
            "Source account identifier is absent; using the local "
            "Vault owner namespace."
        )
    provider = "claude:" + account
    base_scope = {"workspace_id": account, "project_id": project_id}
    records: dict[str, UmifRecord] = {}
    blobs: dict[str, bytes] = {}
    principals = []
    sessions = []
    events = []
    assets = []
    documents = []
    memories = []

    def make(
        cls: type[UmifRecord],
        kind: str,
        native: str,
        body: dict[str, Any],
        seq: int,
        origin: tuple[str, str, str],
        scope: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> UmifRecord:
        ms = None
        if timestamp:
            try:
                ms = to_timestamp_ms(from_iso_format(timestamp))
            except (ValueError, TypeError):
                warnings.append(
                    "An invalid source timestamp was retained as unavailable."
                )
        record = cls(
            record_id=record_id(provider, kind, native),
            record_type=kind,
            source={"provider": "claude", "native_id": native, "account_id": account},
            scope=scope or base_scope,
            sequence=seq,
            timestamp_ms=ms,
            occurred_at=timestamp if ms is not None else None,
            provenance={
                "content_sha256": content_hash(body),
                "archive_name": origin[0],
                "member_path": origin[1],
                "json_pointer": origin[2],
                "time_quality": "original" if ms is not None else "missing",
            },
            payload=body,
        )
        previous = records.get(record.record_id)
        if previous:
            if (
                previous.provenance["content_sha256"]
                != record.provenance["content_sha256"]
            ):
                raise ImportWorkflowError(
                    "CLAUDE_SOURCE_CONFLICT",
                    "Conflicting source IDs across export parts.",
                )
            return previous
        records[record.record_id] = record
        return record

    for role in ("user", "assistant", "tool", "system", "developer"):
        principals.append(
            make(
                Principal,
                "principal",
                role,
                {"name": role, "kind": role},
                len(principals),
                ("", "", ""),
            )
        )
    principal_ids = {r.payload["kind"]: r.record_id for r in principals}

    def add_document(
        native: str,
        title: str,
        text: str,
        kind: str,
        origin: tuple[str, str, str],
        source_project: str | None = None,
    ) -> None:
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"blobs/sha256/{digest[:2]}/{digest}"
        blobs[relative] = raw
        asset = make(
            Asset,
            "asset",
            native,
            {
                "filename": title,
                "blob_path": relative,
                "sha256": "sha256:" + digest,
                "size_bytes": len(raw),
                "media_type": "text/plain",
            },
            len(assets),
            origin,
        )
        if asset not in assets:
            assets.append(asset)
        doc = make(
            Document,
            "document",
            native,
            {
                "title": title,
                "text": text,
                "kind": kind,
                "asset_id": asset.record_id,
                "source_project_id": source_project,
            },
            len(documents),
            origin,
        )
        if doc not in documents:
            documents.append(doc)

    for archive, member, raw in binaries:
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"blobs/sha256/{digest[:2]}/{digest}"
        blobs[relative] = raw
        asset = make(
            Asset,
            "asset",
            member + ":" + digest,
            {
                "filename": PurePosixPath(member).name,
                "blob_path": relative,
                "sha256": "sha256:" + digest,
                "size_bytes": len(raw),
                "media_type": "application/octet-stream",
            },
            len(assets),
            (archive, member, ""),
        )
        if asset not in assets:
            assets.append(asset)
    asset_lookup: dict[str, list[str]] = {}
    for asset in assets:
        for name in {asset.provenance["member_path"], asset.payload["filename"]}:
            asset_lookup.setdefault(name, []).append(asset.record_id)

    for row, archive, member, pointer in groups["projects"]:
        native = str(row.get("uuid") or row.get("id") or content_hash(row))
        origin = (archive, member, pointer)
        instruction = row.get("prompt_template") or row.get("instructions")
        if isinstance(instruction, str) and instruction.strip():
            add_document(
                native + ":instructions",
                str(row.get("name") or "Project") + " instructions",
                instruction,
                "project_instruction",
                origin,
                native,
            )
        for index, doc in enumerate(row.get("docs") or row.get("documents") or []):
            if not isinstance(doc, dict):
                raise ImportWorkflowError(
                    "CLAUDE_SCHEMA_UNSUPPORTED", "Unsupported project document schema."
                )
            text = doc.get("content") or doc.get("text")
            if isinstance(text, str):
                add_document(
                    native + ":" + str(doc.get("uuid") or doc.get("id") or index),
                    str(doc.get("filename") or doc.get("title") or "Project document"),
                    text,
                    "project_knowledge",
                    (archive, member, pointer + f"/docs/{index}"),
                    native,
                )
            else:
                name = str(
                    doc.get("path") or doc.get("filename") or doc.get("file_name") or ""
                )
                candidates = sorted(set(asset_lookup.get(name, [])))
                if len(candidates) == 1:
                    document = make(
                        Document,
                        "document",
                        native + ":" + str(doc.get("uuid") or doc.get("id") or index),
                        {
                            "title": str(
                                doc.get("title")
                                or doc.get("filename")
                                or "Project document"
                            ),
                            "text": "",
                            "kind": "project_knowledge",
                            "asset_id": candidates[0],
                            "source_project_id": native,
                        },
                        len(documents),
                        (archive, member, pointer + f"/docs/{index}"),
                    )
                    if document not in documents:
                        documents.append(document)
                else:
                    warnings.append(
                        "A project document has no text or matching file; "
                        "retained in source evidence."
                    )

    for row, archive, member, pointer in groups["conversations"]:
        native = str(row.get("uuid") or row.get("id") or "")
        messages = row.get("chat_messages", row.get("messages"))
        if not native or not isinstance(messages, list):
            raise ImportWorkflowError(
                "CLAUDE_SCHEMA_UNSUPPORTED", "Unsupported Claude conversation schema."
            )
        session = make(
            Session,
            "session",
            native,
            {
                "native_session_id": native,
                "title": row.get("name", ""),
                "source_project_id": row.get("project_uuid") or row.get("project_id"),
                "kind": "chat",
            },
            len(sessions),
            (archive, member, pointer),
        )
        if session not in sessions:
            sessions.append(session)
        scope = {**base_scope, "session_id": session.record_id}
        sequence = sum(e.scope.get("session_id") == session.record_id for e in events)
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ImportWorkflowError(
                    "CLAUDE_SCHEMA_UNSUPPORTED", "Unsupported Claude message schema."
                )
            role = {
                "human": "user",
                "user": "user",
                "assistant": "assistant",
                "system": "system",
                "developer": "developer",
            }.get(message.get("sender", message.get("role")))
            if role is None:
                skipped["unsupported_role"] = skipped.get("unsupported_role", 0) + 1
                continue
            mid = str(
                message.get("uuid") or message.get("id") or f"{native}:message:{index}"
            )
            origin = (archive, member, pointer + f"/chat_messages/{index}")
            content = message.get("content")
            if content is None:
                content = [{"type": "text", "text": message.get("text", "")}]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                raise ImportWorkflowError(
                    "CLAUDE_SCHEMA_UNSUPPORTED", "Unsupported message content schema."
                )
            parts = []
            tool_blocks = []
            for block in content:
                if not isinstance(block, dict):
                    raise ImportWorkflowError(
                        "CLAUDE_SCHEMA_UNSUPPORTED", "Unsupported content block schema."
                    )
                kind = block.get("type")
                if kind == "text":
                    parts.append({"type": "text", "text": str(block.get("text", ""))})
                elif kind == "artifact" and isinstance(block.get("content"), str):
                    add_document(
                        str(block.get("id") or mid + ":artifact"),
                        str(block.get("title") or "Artifact"),
                        block["content"],
                        "artifact",
                        origin,
                        row.get("project_uuid") or row.get("project_id"),
                    )
                elif kind in {"tool_use", "tool_result"}:
                    tool_blocks.append(block)
                else:
                    skipped[str(kind or "unknown_content")] = (
                        skipped.get(str(kind or "unknown_content"), 0) + 1
                    )
            if not parts and message.get("text") and not content:
                parts = [{"type": "text", "text": str(message["text"])}]
            for attachment in message.get("attachments") or message.get("files") or []:
                if not isinstance(attachment, dict):
                    warnings.append(
                        "Unsupported attachment reference retained in raw evidence."
                    )
                    continue
                name = (
                    attachment.get("file_name")
                    or attachment.get("filename")
                    or attachment.get("name")
                )
                candidates = sorted(set(asset_lookup.get(str(name), [])))
                if len(candidates) == 1:
                    parts.append({"type": "asset", "asset_id": candidates[0]})
                else:
                    warnings.append(
                        "An attachment is missing or ambiguous; "
                        "its reference remains in raw evidence."
                    )
                    skipped["unresolved_attachment"] = (
                        skipped.get("unresolved_attachment", 0) + 1
                    )
            body = {
                "role": role,
                "content_parts": parts,
                "author_principal_id": principal_ids.get(
                    role, principal_ids["assistant"]
                ),
            }
            event = make(
                Message,
                "event.message",
                mid,
                body,
                sequence,
                origin,
                scope,
                message.get("created_at"),
            )
            if role in {"system", "developer"}:
                event.routing_hints["user_memory"] = "deny"
            if event in events:
                continue
            private_keys = ("parent_uuid", "parent_message_uuid", "branch_id")
            event.extensions["claude"] = {
                key: message[key] for key in private_keys if key in message
            }
            if any(key in message for key in private_keys):
                warnings.append(
                    "Branch metadata is preserved; "
                    "messages use the exported list order."
                )
            events.append(event)
            sequence += 1
            for block_index, block in enumerate(tool_blocks):
                call = block["type"] == "tool_use"
                call_id = block.get("id") if call else block.get("tool_use_id")
                if not call_id:
                    warnings.append(
                        "Tool block without a call identifier was "
                        "retained only in raw evidence."
                    )
                    continue
                body = (
                    {
                        "call_id": call_id,
                        "tool_name": block.get("name"),
                        "arguments": block.get("input"),
                        "assistant_message_id": event.record_id,
                    }
                    if call
                    else {
                        "call_id": call_id,
                        "content_parts": [
                            {"type": "text", "text": str(block.get("content", ""))}
                        ],
                        "is_error": bool(block.get("is_error")),
                    }
                )
                events.append(
                    make(
                        ToolCall if call else ToolResult,
                        "event.tool_call" if call else "event.tool_result",
                        f"{mid}:tool:{block_index}",
                        body,
                        sequence,
                        origin,
                        scope,
                        message.get("created_at"),
                    )
                )
                sequence += 1

    for row, archive, member, pointer in groups["memories"]:
        text = row.get("content") or row.get("text") or row.get("memory")
        if not isinstance(text, str):
            raise ImportWorkflowError(
                "CLAUDE_SCHEMA_UNSUPPORTED", "Unsupported Claude saved memory schema."
            )
        native = str(
            row.get("uuid")
            or row.get("id")
            or content_hash({"text": text, "project": row.get("project_uuid")})
        )
        assertion = make(
            MemoryAssertion,
            "memory.assertion",
            native,
            {
                "text": text,
                "origin_kind": "platform_summary",
                "requires_review": True,
                "source_project_id": row.get("project_uuid") or row.get("project_id"),
            },
            len(memories),
            (archive, member, pointer),
            timestamp=row.get("updated_at") or row.get("created_at"),
        )
        if assertion not in memories:
            memories.append(assertion)

    source_projects = {
        str(r.get("uuid") or r.get("id")): r.get("name", "")
        for r, *_ in groups["projects"]
    }
    for category in ("conversations", "memories"):
        for row, *_ in groups[category]:
            project = row.get("project_uuid") or row.get("project_id")
            if project:
                source_projects.setdefault(str(project), "")
    if any(event.timestamp_ms is None for event in events):
        warnings.append(
            "Some source timestamps are missing. "
            "Original times stay missing; extraction may infer processing times."
        )
    optional = {
        "assets.jsonl": tuple(assets),
        "documents.jsonl": tuple(documents),
        "memories.jsonl": tuple(memories),
    }
    # Package identity depends on content and scope, excluding transport metadata.
    identity = sorted(
        (r.record_id, r.provenance["content_sha256"]) for r in records.values()
    )
    manifest = Manifest(
        package_id="pkg_" + content_hash([project_id, identity]).split(":")[1],
        generator={"name": "everos-claude", "version": PARSER_VERSION},
        source_exports=exports,
        default_scope=base_scope,
        files=[],
    )
    return seal_package(
        UmifPackage(
            manifest=manifest,
            principals=tuple(principals),
            sessions=tuple(sessions),
            events=tuple(events),
            optional_records=optional,
            blobs=blobs,
            import_report={
                "warnings": sorted(set(warnings)),
                "skipped_event_counts": skipped,
                "source_projects": [
                    {"id": key, "name": value}
                    for key, value in sorted(source_projects.items())
                ],
                "counts": {
                    "sessions": len(sessions),
                    "messages": sum(isinstance(e, Message) for e in events),
                    "assets": len(assets),
                    "documents": len(documents),
                    "assertions": len(memories),
                },
            },
        )
    )
