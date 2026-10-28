"""Convert DeepSeek web account export archives to UMIF."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
import zlib
from collections import Counter
from datetime import datetime
from pathlib import PurePosixPath

from everos.umif.canonical import content_hash, record_id
from everos.umif.models import Manifest, Message, Principal, Session, UmifPackage
from everos.umif.package import seal_package

from .import_contracts import ImportWorkflowError

PARSER_VERSION = "deepseek-web-1"


def convert_deepseek_web(
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
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED", "Invalid ZIP archive."
        ) from exc
    members: dict[str, bytes] = {}
    total = 0
    seen: set[str] = set()
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > max_files:
            raise ImportWorkflowError(
                "IMPORT_SECURITY_REJECTED", "ZIP entry count exceeds limit."
            )
        for info in infos:
            path = PurePosixPath(info.filename)
            kind = stat.S_IFMT(info.external_attr >> 16)
            if (
                not info.filename
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in info.filename
                or ":" in info.filename
                or len(path.parts) > 12
                or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                or path.as_posix() in seen
            ):
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED", "Unsafe or duplicate ZIP path."
                )
            seen.add(path.as_posix())
            if info.is_dir():
                continue
            if path.suffix.lower() in {
                ".zip",
                ".tar",
                ".gz",
                ".7z",
                ".rar",
                ".bz2",
                ".xz",
            }:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED", "Nested archive is not allowed."
                )
            if info.flag_bits & 1 or info.compress_type not in {
                zipfile.ZIP_STORED,
                zipfile.ZIP_DEFLATED,
            }:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED", "Unsupported ZIP encoding."
                )
            total += info.file_size
            if total > max_unpacked_bytes or (
                info.file_size > 1024 * 1024
                and info.file_size / max(info.compress_size, 1) > max_compression_ratio
            ):
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "ZIP resource limit exceeded.",
                    status_code=413,
                )
            try:
                with archive.open(info) as stream:
                    data = stream.read(max_unpacked_bytes + 1)
            except (
                zipfile.BadZipFile,
                RuntimeError,
                OSError,
                EOFError,
                zlib.error,
            ) as exc:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED", "ZIP member cannot be decoded."
                ) from exc
            if len(data) != info.file_size:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED", "ZIP member size mismatch."
                )
            members[path.as_posix()] = data
    try:
        conversations = json.loads(members["conversations.json"])
        user = json.loads(members["user.json"])
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportWorkflowError(
            "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED",
            "Missing or invalid DeepSeek web export JSON.",
        ) from exc
    if not isinstance(conversations, list) or not isinstance(user, dict):
        raise ImportWorkflowError(
            "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "Invalid DeepSeek web export structure."
        )
    account = user.get("user_id")
    if not isinstance(account, str) or not account:
        raise ImportWorkflowError(
            "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "DeepSeek export user ID is missing."
        )
    account_key = hashlib.sha256(account.encode()).hexdigest()
    provider = "deepseek_web"
    namespace = provider + ":" + account_key
    scope = {"workspace_id": provider, "project_id": project_id}
    principals = []
    for sequence, role in enumerate(("user", "assistant")):
        body = {"kind": role, "name": role}
        principals.append(
            Principal(
                record_id=record_id(namespace, "principal", role),
                source={"provider": provider, "native_id": role},
                scope=scope,
                sequence=sequence,
                provenance={"content_sha256": content_hash(body)},
                payload=body,
            )
        )
    principal_ids = {p.payload["kind"]: p.record_id for p in principals}
    sessions, events, warnings = [], [], []
    skipped: Counter[str] = Counter()
    for ci, conversation in enumerate(conversations):
        if (
            not isinstance(conversation, dict)
            or not isinstance(conversation.get("id"), str)
            or not isinstance(conversation.get("mapping"), dict)
        ):
            raise ImportWorkflowError(
                "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "Invalid conversation ID or mapping."
            )
        native = conversation["id"]
        if not native:
            raise ImportWorkflowError(
                "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "Empty conversation ID."
            )
        mapping = conversation["mapping"]
        children: dict[str | None, list[str]] = {}
        for node_id, node in mapping.items():
            if not isinstance(node_id, str) or not isinstance(node, dict):
                raise ImportWorkflowError(
                    "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "Invalid conversation node."
                )
            parent = node.get("parent")
            if parent is not None and (
                not isinstance(parent, str) or parent not in mapping
            ):
                raise ImportWorkflowError(
                    "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "Invalid node parent."
                )
            children.setdefault(parent, []).append(node_id)
        roots = children.get(None, [])
        if mapping and not roots:
            raise ImportWorkflowError(
                "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "Conversation graph has no root."
            )
        session_id = record_id(namespace, "session", native)
        body = {
            "native_session_id": f"web:{account_key}:{native}",
            "title": conversation.get("title") or "Untitled",
        }
        sessions.append(
            Session(
                record_id=session_id,
                source={"provider": provider, "native_id": native},
                scope=scope,
                sequence=ci,
                provenance={
                    "content_sha256": content_hash(body),
                    "archive_name": filename,
                    "member_path": "conversations.json",
                    "json_pointer": f"/{ci}",
                },
                payload=body,
            )
        )
        stack = [(node_id, len(roots) != 1) for node_id in reversed(roots)]
        visited: set[str] = set()
        sequence = 0
        has_branch = False
        while stack:
            node_id, ambiguous = stack.pop()
            if node_id in visited:
                raise ImportWorkflowError(
                    "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED",
                    "Conversation graph contains a cycle.",
                )
            visited.add(node_id)
            node = mapping[node_id]
            descendants = children.get(node_id, [])
            has_branch = has_branch or len(descendants) > 1 or ambiguous
            stack.extend(
                (child, ambiguous or len(descendants) > 1)
                for child in reversed(descendants)
            )
            message = node.get("message")
            if message is None:
                continue
            if not isinstance(message, dict) or not isinstance(
                message.get("fragments"), list
            ):
                raise ImportWorkflowError(
                    "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "Invalid message fragments."
                )
            for fi, fragment in enumerate(message["fragments"]):
                if not isinstance(fragment, dict):
                    raise ImportWorkflowError(
                        "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED", "Invalid message fragment."
                    )
                typ = fragment.get("type")
                if typ not in {"REQUEST", "RESPONSE"}:
                    skipped[str(typ or "unknown")] += 1
                    continue
                text = fragment.get("content")
                if not isinstance(text, str) or not text.strip():
                    skipped["empty_text"] += 1
                    continue
                role = "user" if typ == "REQUEST" else "assistant"
                stamp = message.get("inserted_at")
                try:
                    dt = (
                        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        if isinstance(stamp, str)
                        else None
                    )
                    if dt is not None and dt.utcoffset() is None:
                        dt = None
                except (ValueError, OverflowError):
                    dt = None
                item = {
                    "role": role,
                    "content_parts": [{"type": "text", "text": text}],
                    "author_principal_id": principal_ids[role],
                }
                source_id = f"web:{account_key}:{native}:{node_id}:{fi}"
                pointer_id = node_id.replace("~", "~0").replace("/", "~1")
                events.append(
                    Message(
                        record_id=record_id(namespace, "event.message", source_id),
                        source={
                            "provider": provider,
                            "native_id": source_id,
                            "node_id": node_id,
                        },
                        scope={**scope, "session_id": session_id},
                        sequence=sequence,
                        timestamp_ms=int(dt.timestamp() * 1000) if dt else None,
                        occurred_at=dt.isoformat() if dt else None,
                        provenance={
                            "content_sha256": content_hash(item),
                            "archive_name": filename,
                            "member_path": "conversations.json",
                            "json_pointer": (
                                f"/{ci}/mapping/{pointer_id}/message/fragments/{fi}"
                            ),
                        },
                        routing_hints={"user_memory": "deny" if ambiguous else "allow"},
                        payload=item,
                    )
                )
                sequence += 1
        if len(visited) != len(mapping):
            raise ImportWorkflowError(
                "DEEPSEEK_WEB_SCHEMA_UNSUPPORTED",
                "Conversation graph contains disconnected nodes or a cycle.",
            )
        if has_branch:
            warnings.append(
                f"第 {ci + 1} 个会话存在无法确定当前分支的分叉；"
                "分叉消息已归档，但不参与记忆抽取。"
            )
    manifest = Manifest(
        package_id=record_id(
            namespace, "package", hashlib.sha256(payload).hexdigest() + ":" + project_id
        ),
        generator={"name": "everos-deepseek-web", "version": PARSER_VERSION},
        source_exports=[{"provider": provider, "product": "deepseek-web"}],
        default_scope=scope,
        files=[],
        privacy={"classification": "private", "contains_personal_data": True},
    )
    return seal_package(
        UmifPackage(
            manifest=manifest,
            principals=tuple(principals),
            sessions=tuple(sessions),
            events=tuple(events),
            import_report={"skipped_event_counts": dict(skipped), "warnings": warnings},
        )
    )
