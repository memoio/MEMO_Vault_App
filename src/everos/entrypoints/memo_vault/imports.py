"""Safe parsing and durable archival for user supplied import files."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import stat
import unicodedata
import uuid
import zipfile
import zlib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from everos.component.utils.datetime import get_utc_now
from everos.umif.canonical import content_hash as canonical_content_hash

from .storage import VaultStore

DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_UNPACKED_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_FILES = 100
DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO = 200
MAX_ARCHIVE_PATH_DEPTH = 8
PREVIEW_TTL_SECONDS = 60 * 60
_ALLOWED_EXTENSIONS = {".json", ".jsonl", ".md", ".txt", ".zip"}
_ALLOWED_MEDIA_TYPES = {
    ".json": {"application/json", "application/x-json", "text/json", "text/plain"},
    ".jsonl": {
        "application/jsonl",
        "application/x-jsonl",
        "application/x-ndjson",
        "application/ndjson",
        "text/plain",
    },
    ".md": {"text/markdown", "text/x-markdown", "text/plain"},
    ".txt": {"text/plain", "text/markdown", "text/x-markdown"},
    ".zip": {"application/zip", "application/x-zip-compressed"},
}
_GENERIC_MEDIA_TYPES = {"application/octet-stream", "binary/octet-stream"}
_NESTED_ARCHIVE_EXTENSIONS = {
    ".7z",
    ".bz2",
    ".gz",
    ".rar",
    ".tar",
    ".tgz",
    ".xz",
    ".zip",
}
_ROLE_HEADING = re.compile(
    r"^#{1,3}\s*(user|assistant|system|developer|tool)\s*:?[ \t]*$", re.I
)
_ROLE_LINE = re.compile(r"^(user|assistant|system|developer|tool)\s*:\s*(.*)$", re.I)
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SENSITIVE_TRACE_KEY = re.compile(
    r"(?:secret|token|password|passwd|authorization|api[_-]?key|private[_-]?key)",
    re.I,
)
_SENSITIVE_TRACE_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|authorization)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_PREVIEW_TRACE_TEXT_LIMIT = 2000


from .import_contracts import (
    DEEPSEEK_HARNESS_PARSER_VERSION, PARSER_VERSION, ImportWorkflowError,
    ParsedImport, ParsedMessage, ParsedToolEvent,
)


def _content_hash(content: str) -> str:
    canonical = " ".join(unicodedata.normalize("NFKC", content).split())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timestamp_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("timestamp must be a number or ISO-8601 string")
    if isinstance(value, (int, float)):
        # EverOS and this API use Unix epoch milliseconds.
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timestamp must be a positive Unix epoch value")
        return int(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be a number or ISO-8601 string") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    raise ValueError("timestamp must be a number or ISO-8601 string")


def _from_json(value: Any) -> list[tuple[str, str, int | None, str | None]]:
    messages = value.get("messages") if isinstance(value, dict) else value
    if not isinstance(messages, list) or not messages:
        raise ImportWorkflowError(
            "IMPORT_PARSE_FAILED",
            "JSON must be a non-empty message array or contain a non-empty "
            "messages array.",
        )
    result: list[tuple[str, str, int | None, str | None]] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            raise ImportWorkflowError(
                "IMPORT_PARSE_FAILED", f"Message {index + 1} must be an object."
            )
        role, content = item.get("role"), item.get("content")
        if not isinstance(role, str) or not role.strip():
            raise ImportWorkflowError(
                "IMPORT_PARSE_FAILED", f"Message {index + 1} is missing a role."
            )
        if not isinstance(content, str) or not content.strip():
            raise ImportWorkflowError(
                "IMPORT_PARSE_FAILED", f"Message {index + 1} is missing text content."
            )
        try:
            timestamp = _timestamp_ms(item.get("timestamp", item.get("timestamp_ms")))
        except ValueError as exc:
            raise ImportWorkflowError(
                "IMPORT_PARSE_FAILED", f"Message {index + 1}: {exc}."
            ) from exc
        source_id = item.get("id", item.get("message_id"))
        result.append(
            (
                role.strip().lower(),
                content.strip(),
                timestamp,
                str(source_id) if source_id is not None else None,
            )
        )
    return result


def _from_text(
    text: str, *, markdown: bool
) -> list[tuple[str, str, int | None, str | None]]:
    lines = text.splitlines()
    labelled: list[tuple[str, list[str]]] = []
    current_role: str | None = None
    current_lines: list[str] = []

    for line in lines:
        heading = _ROLE_HEADING.match(line) if markdown else None
        speaker = heading or _ROLE_LINE.match(line)
        if speaker:
            if current_role is not None:
                content = "\n".join(current_lines).strip()
                if content:
                    labelled.append((current_role, [content]))
            current_role = speaker.group(1).lower()
            current_lines = (
                [speaker.group(2)] if not heading and speaker.group(2) else []
            )
        elif current_role is not None:
            current_lines.append(line)

    if current_role is not None:
        content = "\n".join(current_lines).strip()
        if content:
            labelled.append((current_role, [content]))

    if not labelled:
        content = text.strip()
        if not content:
            raise ImportWorkflowError("IMPORT_PARSE_FAILED", "The text file is empty.")
        labelled = [("user", [content])]
    return [(role, content[0], None, None) for role, content in labelled]


def _validate_content_type(filename: str, content_type: str | None) -> str:
    extension = Path(filename.replace("\\", "/")).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED",
            "Unsupported file type. Upload a .json, .jsonl, .md, .txt or safe .zip file.",
        )
    declared_type = (content_type or "").split(";", 1)[0].strip().lower()
    if (
        declared_type
        and declared_type not in _GENERIC_MEDIA_TYPES
        and declared_type not in _ALLOWED_MEDIA_TYPES[extension]
    ):
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED",
            f"The declared media type {declared_type!r} does not match "
            f"{extension} files.",
            status_code=415,
        )
    return extension


def _decode_text(payload: bytes) -> str:
    if not payload:
        raise ImportWorkflowError("IMPORT_PARSE_FAILED", "The file is empty.")
    if b"\x00" in payload:
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED", "Binary content is not allowed in text imports."
        )
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED", "The file must contain valid UTF-8 text."
        ) from exc
    if any(
        (ord(character) < 32 and character not in "\t\n\r")
        or ord(character) == 127
        for character in text
    ):
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED",
            "The file contains binary control characters and cannot be "
            "imported as text.",
        )
    return text


def _parse_text_file(filename: str, payload: bytes) -> ParsedImport:
    extension = Path(filename.replace("\\", "/")).suffix.lower()
    text = _decode_text(payload)
    if extension == ".json":
        try:
            records = _from_json(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ImportWorkflowError(
                "IMPORT_PARSE_FAILED",
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}.",
            ) from exc
    elif extension == ".jsonl":
        from .deepseek_harness import parse_deepseek_harness

        parsed_session = parse_deepseek_harness(payload)
        messages = tuple(
            ParsedMessage(
                conversation_id=parsed_session.session_id,
                ordinal=ordinal,
                role=message.role,
                content=message.content,
                timestamp_ms=message.timestamp_ms,
                source_message_id=message.source_message_id,
                content_hash=_content_hash(message.content),
                memory_input=message.memory_input,
            )
            for ordinal, message in enumerate(parsed_session.messages)
        )
        tool_events = tuple(
            ParsedToolEvent(
                conversation_id=parsed_session.session_id,
                ordinal=ordinal,
                source_event_id=event.source_event_id,
                source_event_seq=event.seq,
                event_type=event.event_type,
                timestamp_ms=event.timestamp_ms,
                turn=event.turn,
                step=event.step,
                call_id=event.call_id,
                tool_name=event.tool_name,
                arguments=event.arguments,
                result_content=event.result_content,
                result_text=event.result_text,
                is_error=event.is_error,
                source_event_seqs=event.source_event_seqs,
                content_hash=_content_hash(
                    json.dumps(
                        {
                            "event_type": event.event_type,
                            "call_id": event.call_id,
                            "tool_name": event.tool_name,
                            "arguments": event.arguments,
                            "result_content": event.result_content,
                            "is_error": event.is_error,
                            "source_event_seqs": event.source_event_seqs,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ),
            )
            for ordinal, event in enumerate(parsed_session.tool_events)
        )
        return ParsedImport(
            messages=messages,
            conversation_count=1,
            duplicate_message_count=0,
            tool_call_count=sum(event.event_type == "tool/call" for event in tool_events),
            tool_events=tool_events,
            tool_result_count=sum(event.event_type == "tool/result" for event in tool_events),
            skipped_event_counts=parsed_session.skipped_event_counts,
            warnings=tuple(parsed_session.warnings),
            parser_version=DEEPSEEK_HARNESS_PARSER_VERSION,
        )
    else:
        records = _from_text(text, markdown=extension == ".md")

    messages: list[ParsedMessage] = []
    seen: set[str] = set()
    duplicate_count = 0
    for ordinal, (role, content, timestamp, source_id) in enumerate(records):
        content_hash = _content_hash(content)
        duplicate = content_hash in seen
        if duplicate:
            duplicate_count += 1
        seen.add(content_hash)
        messages.append(
            ParsedMessage(
                conversation_id="conversation-1",
                ordinal=ordinal,
                role=role,
                content=content,
                timestamp_ms=timestamp,
                source_message_id=source_id,
                content_hash=content_hash,
                duplicate=duplicate,
            )
        )
    return ParsedImport(tuple(messages), 1, duplicate_count)


def _archive_member_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED", "ZIP contains an unsafe member path."
        )
    if re.match(r"^[a-zA-Z]:", name):
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED", "ZIP contains an absolute drive path."
        )
    segments = name.rstrip("/").split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED", "ZIP contains a path traversal member."
        )
    path = PurePosixPath(name)
    if len(path.parts) - 1 > MAX_ARCHIVE_PATH_DEPTH:
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED",
            f"ZIP member paths may be nested at most {MAX_ARCHIVE_PATH_DEPTH} levels.",
        )
    return path


def _parse_zip_file(
    payload: bytes,
    *,
    max_unpacked_bytes: int,
    max_files: int,
    max_compression_ratio: int,
) -> ParsedImport:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImportWorkflowError(
            "IMPORT_SECURITY_REJECTED", "The file is not a valid ZIP archive."
        ) from exc

    with archive:
        entries = archive.infolist()
        if not entries:
            raise ImportWorkflowError("IMPORT_PARSE_FAILED", "The ZIP archive is empty.")
        if len(entries) > max_files:
            raise ImportWorkflowError(
                "IMPORT_SECURITY_REJECTED",
                f"ZIP contains more than the configured {max_files} entries.",
                status_code=413,
            )

        unpacked_total = 0
        supported: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        seen_paths: set[str] = set()
        for info in entries:
            path = _archive_member_path(info.filename)
            normalized_path = path.as_posix()
            if normalized_path in seen_paths:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED", "ZIP contains duplicate member paths."
                )
            seen_paths.add(normalized_path)

            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            if stat.S_ISLNK(unix_mode) or (
                file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}
            ):
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "ZIP contains a symbolic link or special file.",
                )
            if info.flag_bits & 0x1:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED", "Encrypted ZIP entries are not supported."
                )
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "ZIP uses an unsupported compression method.",
                )
            unpacked_total += info.file_size
            if unpacked_total > max_unpacked_bytes:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "ZIP exceeds the configured total uncompressed size limit.",
                    status_code=413,
                )
            if (
                info.file_size > 1024 * 1024
                and info.file_size / max(info.compress_size, 1)
                > max_compression_ratio
            ):
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "ZIP contains a member with a suspicious compression ratio.",
                    status_code=413,
                )

            if info.is_dir():
                continue
            if path.suffix.lower() in _NESTED_ARCHIVE_EXTENSIONS:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "Nested archives are not supported (maximum nesting depth is 0).",
                )
            if path.suffix.lower() in {".json", ".md", ".txt"}:
                supported.append((info, path))

        if not supported:
            raise ImportWorkflowError(
                "IMPORT_PARSE_FAILED",
                "ZIP must contain at least one .json, .md or .txt file.",
            )

        messages: list[ParsedMessage] = []
        seen_hashes: set[str] = set()
        for info, path in sorted(supported, key=lambda item: item[1].as_posix()):
            try:
                with archive.open(info, "r") as member:
                    member_payload = member.read(max_unpacked_bytes + 1)
            except (
                zipfile.BadZipFile,
                RuntimeError,
                OSError,
                EOFError,
                zlib.error,
            ) as exc:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    f"ZIP member {path.as_posix()} could not be safely decompressed.",
                ) from exc
            if (
                len(member_payload) > max_unpacked_bytes
                or len(member_payload) != info.file_size
            ):
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    f"ZIP member {path.as_posix()} exceeded its declared size.",
                    status_code=413,
                )
            parsed = _parse_text_file(path.name, member_payload)
            conversation_id = (
                "zip-" + hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest()[:16]
            )
            for message in parsed.messages:
                duplicate = message.content_hash in seen_hashes
                seen_hashes.add(message.content_hash)
                messages.append(
                    ParsedMessage(
                        conversation_id=conversation_id,
                        ordinal=message.ordinal,
                        role=message.role,
                        content=message.content,
                        timestamp_ms=message.timestamp_ms,
                        source_message_id=message.source_message_id,
                        content_hash=message.content_hash,
                        duplicate=duplicate,
                        memory_input=message.memory_input,
                    )
                )
        duplicate_count = sum(message.duplicate for message in messages)
        return ParsedImport(tuple(messages), len(supported), duplicate_count)


def parse_import_file(
    filename: str,
    payload: bytes,
    *,
    content_type: str | None = None,
    max_archive_unpacked_bytes: int = DEFAULT_MAX_ARCHIVE_UNPACKED_BYTES,
    max_archive_files: int = DEFAULT_MAX_ARCHIVE_FILES,
    max_archive_compression_ratio: int = DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO,
) -> ParsedImport:
    """Parse generic JSON, Markdown, text or a safely bounded ZIP archive."""
    extension = _validate_content_type(filename, content_type)
    if extension == ".zip":
        return _parse_zip_file(
            payload,
            max_unpacked_bytes=max_archive_unpacked_bytes,
            max_files=max_archive_files,
            max_compression_ratio=max_archive_compression_ratio,
        )
    return _parse_text_file(filename, payload)


def validate_project_id(project_id: str) -> str:
    value = project_id.strip()
    if not _PROJECT_ID.fullmatch(value) or value in {".", ".."}:
        raise ImportWorkflowError(
            "INVALID_PROJECT_ID",
            "Use a lowercase project ID containing letters, numbers, hyphens "
            "or underscores (up to 64 characters).",
        )
    return value


class ImportService:
    """Owns ephemeral previews and atomic local evidence archival."""

    def __init__(
        self,
        store: VaultStore,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_archive_unpacked_bytes: int = DEFAULT_MAX_ARCHIVE_UNPACKED_BYTES,
        max_archive_files: int = DEFAULT_MAX_ARCHIVE_FILES,
        max_archive_compression_ratio: int = DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO,
    ) -> None:
        self.store = store
        self.max_file_bytes = max_file_bytes
        self.max_archive_unpacked_bytes = max_archive_unpacked_bytes
        self.max_archive_files = max_archive_files
        self.max_archive_compression_ratio = max_archive_compression_ratio
        self.preview_dir = store.data_dir / "previews"
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_expired_previews()

    @staticmethod
    def safe_filename(filename: str | None) -> str:
        value = (filename or "upload").replace("\\", "/").rsplit("/", 1)[-1]
        value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
        return value[:180] or "upload.txt"

    def _cleanup_expired_previews(self) -> None:
        cutoff = get_utc_now().timestamp() - PREVIEW_TTL_SECONDS
        for path in self.preview_dir.glob("*"):
            try:
                if path.stat().st_mtime < cutoff:
                    if path.is_dir():
                        import shutil
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
            except OSError:
                continue

    def _convert_to_umif(
        self, filename: str, source_type: str, payload: bytes, project_id: str
    ) -> tuple[Any, ParsedImport]:
        from .umif_projection import project_for_vault

        if source_type == "chatgpt":
            from .chatgpt_export import convert_chatgpt_export
            package = convert_chatgpt_export(
                payload, filename, project_id,
                max_unpacked_bytes=self.max_archive_unpacked_bytes,
                max_files=self.max_archive_files,
                max_compression_ratio=self.max_archive_compression_ratio,
            )
        elif source_type == "claude":
            import tempfile
            from .claude_export import convert_claude_export
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / filename
                path.write_bytes(payload)
                package = convert_claude_export([path], project_id)
        elif source_type == "deepseek_harness":
            from .deepseek_harness import convert_harness_v4
            from .deepseek_web import convert_deepseek_web

            limits = ImportLimits(
                max_file_bytes=self.max_file_bytes,
                max_unpacked_bytes=self.max_archive_unpacked_bytes,
                max_files=self.max_archive_files,
                max_compression_ratio=self.max_archive_compression_ratio,
            )
            web_export = False
            if Path(filename).suffix.lower() == ".zip":
                try:
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        names = {
                            item.filename for item in archive.infolist() if not item.is_dir()
                        }
                except (zipfile.BadZipFile, OSError) as exc:
                    raise ImportWorkflowError(
                        "IMPORT_SECURITY_REJECTED", "Invalid ZIP archive."
                    ) from exc
                web_export = "conversations.json" in names
                if web_export and any(
                    PurePosixPath(name).name == "session.v4.jsonl" for name in names
                ):
                    raise ImportWorkflowError(
                        "IMPORT_PARSE_FAILED", "ZIP contains multiple DeepSeek formats."
                    )
            if web_export:
                package = convert_deepseek_web(
                    payload,
                    filename,
                    project_id,
                    max_unpacked_bytes=self.max_archive_unpacked_bytes,
                    max_files=self.max_archive_files,
                    max_compression_ratio=self.max_archive_compression_ratio,
                )
            else:
                session = read_harness_export(payload, filename, limits)
                package = convert_harness_v4(session, project_id)
        else:
            parsed_source = parse_import_file(
                filename,
                payload,
                max_archive_unpacked_bytes=self.max_archive_unpacked_bytes,
                max_archive_files=self.max_archive_files,
                max_archive_compression_ratio=self.max_archive_compression_ratio,
            )
            package = convert_parsed_markdown(
                parsed_source, project_id, source_identity=hashlib.sha256(payload).hexdigest()
            )
        export = {
            **package.manifest.source_exports[0],
            "archive_name": filename,
            "archive_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "exported_at": None,
        }
        package = package.model_copy(update={
            "manifest": package.manifest.model_copy(update={"source_exports": [export]})
        })
        return package, project_for_vault(package)

    def preview(
        self,
        filename: str,
        source_type: str,
        payload: bytes,
        *,
        content_type: str | None = None,
        project_id: str | None = None,
        _package: Any = None,
        _source_files: tuple[Path, ...] = (),
        _job_id: str | None = None,
    ) -> dict[str, Any]:
        self._cleanup_expired_previews()
        if source_type not in {"generic", "deepseek_harness", "claude", "chatgpt"}:
            raise ImportWorkflowError(
                "IMPORT_SOURCE_UNSUPPORTED",
                "This import source is not supported.",
                status_code=422,
            )
        if len(payload) > self.max_file_bytes:
            raise ImportWorkflowError(
                "IMPORT_SECURITY_REJECTED",
                "File exceeds the configured "
                f"{self.max_file_bytes // (1024 * 1024)} MiB limit.",
                status_code=413,
            )
        clean_name = self.safe_filename(filename)
        extension = Path(clean_name).suffix.lower()
        if source_type == "deepseek_harness" and extension not in {".jsonl", ".zip"}:
            raise ImportWorkflowError(
                "IMPORT_PARSE_FAILED",
                "DeepSeek Harness imports require a .jsonl or .zip session file.",
            )
        if source_type == "chatgpt" and extension != ".zip":
            raise ImportWorkflowError("IMPORT_PARSE_FAILED", "ChatGPT imports require a ZIP export.")
        if source_type == "generic" and extension == ".jsonl":
            raise ImportWorkflowError(
                "IMPORT_SOURCE_UNSUPPORTED",
                "Select the DeepSeek Harness source for .jsonl session files.",
            )
        if source_type in {"deepseek_harness", "claude", "chatgpt"}:
            if project_id is None:
                raise ImportWorkflowError(
                    "PROJECT_REQUIRED",
                    "Choose a target project before previewing a Harness session.",
                )
            project_id = validate_project_id(project_id)
            if not self.store.has_project(project_id):
                raise ImportWorkflowError(
                    "PROJECT_NOT_FOUND",
                    "Choose a project space that exists in this Vault.",
                    status_code=404,
                )
        else:
            project_id = validate_project_id(project_id or "personal")
            if not self.store.has_project(project_id):
                raise ImportWorkflowError(
                    "PROJECT_NOT_FOUND", "Choose a project space that exists in this Vault.", status_code=404
                )
        _validate_content_type(clean_name, content_type)
        if _package is None:
            package, parsed = self._convert_to_umif(clean_name, source_type, payload, project_id)
        else:
            from .umif_projection import project_for_vault
            package, parsed = _package, project_for_vault(_package)
        from everos.umif.validator import validate_package
        validation = validate_package(package)
        if not validation.valid:
            raise ImportWorkflowError("UMIF_VALIDATION_FAILED", ", ".join(validation.errors))
        if source_type == "claude":
            from .claude_export import PARSER_VERSION as CLAUDE_PARSER_VERSION
            parsed = replace(parsed, parser_version=CLAUDE_PARSER_VERSION)
        if source_type == "chatgpt":
            from .chatgpt_export import PARSER_VERSION as CHATGPT_PARSER_VERSION
            parsed = replace(parsed, parser_version=CHATGPT_PARSER_VERSION)
        if (
            source_type == "deepseek_harness"
            and package.manifest.source_exports[0].get("provider") == "deepseek_web"
        ):
            from .deepseek_web import PARSER_VERSION as DEEPSEEK_WEB_PARSER_VERSION
            parsed = replace(parsed, parser_version=DEEPSEEK_WEB_PARSER_VERSION)
        raw_hash = hashlib.sha256(payload).hexdigest()
        with self.store.connect() as connection:
            if source_type in {"deepseek_harness", "claude", "chatgpt"}:
                duplicate = connection.execute(
                    """SELECT id FROM import_batches
                       WHERE file_sha256 = ? AND project_id = ? AND parser_version = ?
                       ORDER BY created_at LIMIT 1""",
                    (raw_hash, project_id, parsed.parser_version),
                ).fetchone()
            else:
                duplicate = connection.execute(
                    """SELECT id FROM import_batches
                       WHERE file_sha256 = ? ORDER BY created_at LIMIT 1""",
                    (raw_hash,),
                ).fetchone()
            known_message_hashes = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT content_sha256 FROM normalized_messages"
                ).fetchall()
            }
            known_message_ids: dict[str, list[tuple[str, str]]] = {}
            known_tool_event_ids: dict[str, tuple[str, str | None]] = {}
            if source_type in {"deepseek_harness", "claude", "chatgpt"}:
                for row in connection.execute(
                    """SELECT m.source_message_id, m.content_sha256, b.parser_version
                       FROM normalized_messages m JOIN import_batches b ON b.id = m.batch_id
                       WHERE m.source_message_id IS NOT NULL AND b.project_id = ? AND b.source_type = ?""",
                    (project_id, source_type),
                ).fetchall():
                    known_message_ids.setdefault(row[0], []).append((row[1], row[2]))
                known_tool_event_ids = {
                    row[0]: (row[1], row[2])
                    for row in connection.execute(
                        "SELECT source_event_id, content_sha256, source_content_sha256 FROM normalized_tool_events "
                        "WHERE batch_id IN (SELECT id FROM import_batches WHERE project_id = ? AND source_type = ?)",
                        (project_id, source_type),
                    ).fetchall()
                }
            if duplicate:
                connection.execute(
                    """INSERT INTO import_events (id, batch_id, event_type)
                       VALUES (?, ?, 'duplicate_skipped')""",
                    (uuid.uuid4().hex, duplicate["id"]),
                )
        seen_message_hashes: set[str] = set()
        normalized_messages = []
        source_event_conflict_count = 0
        for message in parsed.messages:
            if source_type in {"deepseek_harness", "claude", "chatgpt"}:
                prior_versions = known_message_ids.get(message.source_message_id or "", [])
                prior_hashes = [content_hash for content_hash, _ in prior_versions]
                is_duplicate = any(
                    content_hash == message.content_hash
                    and version == parsed.parser_version
                    for content_hash, version in prior_versions
                )
                source_event_conflict_count += int(
                    any(content_hash != message.content_hash for content_hash in prior_hashes)
                )
            else:
                is_duplicate = (
                    message.content_hash in known_message_hashes
                    or message.content_hash in seen_message_hashes
                )
            seen_message_hashes.add(message.content_hash)
            normalized_messages.append(replace(message, duplicate=is_duplicate))
        normalized_tool_events = []
        duplicate_tool_event_count = 0
        for event in parsed.tool_events:
            old_hashes = known_tool_event_ids.get(event.source_event_id)
            old_hash = (old_hashes[1] or old_hashes[0]) if old_hashes else None
            new_hash = event.source_content_hash if old_hashes and old_hashes[1] else event.content_hash
            is_duplicate = old_hash == new_hash
            source_event_conflict_count += int(
                old_hash is not None and old_hash != new_hash
            )
            duplicate_tool_event_count += int(is_duplicate)
            normalized_tool_events.append(replace(event, duplicate=is_duplicate))
        warnings = list(parsed.warnings)
        if source_event_conflict_count:
            warnings.append(
                f"{source_event_conflict_count} source event ID(s) already exist with different content."
            )
        parsed = ParsedImport(
            messages=tuple(normalized_messages),
            conversation_count=parsed.conversation_count,
            duplicate_message_count=sum(
                message.duplicate for message in normalized_messages
            ),
            tool_call_count=parsed.tool_call_count,
            tool_result_count=parsed.tool_result_count,
            skipped_event_counts=parsed.skipped_event_counts,
            warnings=tuple(warnings),
            parser_version=parsed.parser_version,
            duplicate_tool_event_count=duplicate_tool_event_count,
            source_event_conflict_count=source_event_conflict_count,
            tool_events=tuple(normalized_tool_events),
        )
        preview_id = uuid.uuid4().hex
        created = get_utc_now().isoformat()
        preview_data = {
            "preview_id": preview_id,
            "source_type": source_type,
            "project_id": project_id,
            "filename": clean_name,
            "file_size": len(payload),
            "raw_file_hash": raw_hash,
            "created_at": created,
            "parsed": asdict(parsed),
            "umif_package_id": package.manifest.package_id,
            "umif_record_count": validation.record_count,
            "umif_warnings": list(validation.warnings),
            "duplicate_batch_id": duplicate["id"] if duplicate else None,
            "import_report": package.import_report,
            "source_job_id": _job_id,
            "source_file_hashes": {},
            "umif_snapshot_hash": canonical_content_hash(package.manifest.model_dump(mode="json")),
        }
        metadata_path = self.preview_dir / f"{preview_id}.json"
        raw_path = self.preview_dir / f"{preview_id}.raw"
        try:
            metadata_path.write_text(
                json.dumps(preview_data, ensure_ascii=False), encoding="utf-8"
            )
            raw_path.write_bytes(payload)
            if source_type == "claude":
                import shutil
                from everos.umif.package import write_package
                write_package(package, self.preview_dir / f"{preview_id}.umif")
                source_dir = self.preview_dir / f"{preview_id}.source"
                source_dir.mkdir()
                for source_file in _source_files:
                    from .export_downloader import ExportDownloader
                    name = self.safe_filename(source_file.name)
                    target = source_dir / name
                    if target.exists():
                        raise ImportWorkflowError('PREVIEW_CORRUPTED', 'Conflicting source filenames.')
                    shutil.copyfile(source_file, target)
                    preview_data['source_file_hashes'][name] = ExportDownloader.checksum(target)
                metadata_path.write_text(json.dumps(preview_data, ensure_ascii=False), encoding='utf-8')
        except OSError:
            metadata_path.unlink(missing_ok=True)
            raw_path.unlink(missing_ok=True)
            raise
        return self._preview_response(preview_data)

    @staticmethod
    def _preview_response(data: dict[str, Any]) -> dict[str, Any]:
        parsed = data["parsed"]
        messages = parsed["messages"]
        tool_events = [
            ImportService._safe_trace_preview(event)
            for event in parsed.get("tool_events", [])[:20]
        ]
        return {
            "preview_id": data["preview_id"],
            "source_type": data["source_type"],
            "filename": data["filename"],
            "file_size": data["file_size"],
            "raw_file_hash": data["raw_file_hash"],
            "umif_package_id": data.get("umif_package_id"),
            "umif_record_count": data.get("umif_record_count", 0),
            "umif_warnings": data.get("umif_warnings", []),
            "conversation_count": parsed["conversation_count"],
            "message_count": len(messages),
            "duplicate_message_count": parsed["duplicate_message_count"],
            "tool_call_count": parsed["tool_call_count"],
            "tool_result_count": parsed["tool_result_count"],
            "duplicate_tool_event_count": parsed["duplicate_tool_event_count"],
            "source_event_conflict_count": parsed["source_event_conflict_count"],
            "skipped_event_counts": parsed["skipped_event_counts"] or {},
            "warnings": parsed["warnings"],
            "messages": messages[:20],
            "tool_events": tool_events,
            "duplicate_batch_id": data["duplicate_batch_id"],
            "import_report": data.get("import_report", {}),
        }

    @staticmethod
    def _safe_trace_preview(value: Any, *, key: str = "") -> Any:
        if key and _SENSITIVE_TRACE_KEY.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): ImportService._safe_trace_preview(
                    item_value, key=str(item_key)
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [ImportService._safe_trace_preview(item) for item in value]
        if isinstance(value, str):
            redacted = _SENSITIVE_TRACE_TEXT.sub(r"\1\2[REDACTED]", value)
            if len(redacted) > _PREVIEW_TRACE_TEXT_LIMIT:
                return redacted[:_PREVIEW_TRACE_TEXT_LIMIT] + "…"
            return redacted
        return value

    def cancel_preview(self, preview_id: str) -> None:
        self._preview_path(preview_id, ".json").unlink(missing_ok=True)
        self._preview_path(preview_id, ".raw").unlink(missing_ok=True)
        import shutil
        for suffix in (".umif", ".source"):
            path = self._preview_path(preview_id, suffix)
            if path.is_dir(): shutil.rmtree(path)

    def _preview_path(self, preview_id: str, suffix: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", preview_id):
            raise ImportWorkflowError(
                "PREVIEW_NOT_FOUND", "Preview does not exist.", status_code=404
            )
        return self.preview_dir / f"{preview_id}{suffix}"

    def commit(self, preview_id: str, project_id: str) -> dict[str, Any]:
        project_id = validate_project_id(project_id)
        if not self.store.has_project(project_id):
            raise ImportWorkflowError(
                "PROJECT_NOT_FOUND",
                "Choose a project space that exists in this Vault.",
                status_code=404,
            )
        identity = self.store.get_identity()
        meta_path = self._preview_path(preview_id, ".json")
        raw_path = self._preview_path(preview_id, ".raw")
        if not meta_path.is_file() or not raw_path.is_file():
            raise ImportWorkflowError(
                "PREVIEW_NOT_FOUND",
                "Preview expired or was cancelled.",
                status_code=404,
            )
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ImportWorkflowError(
                "PREVIEW_NOT_FOUND", "Preview is unavailable.", status_code=404
            ) from exc
        if (
            get_utc_now().timestamp()
            - datetime.fromisoformat(data["created_at"]).timestamp()
            > PREVIEW_TTL_SECONDS
        ):
            self.cancel_preview(preview_id)
            raise ImportWorkflowError(
                "PREVIEW_EXPIRED",
                "Preview expired; upload the file again.",
                status_code=410,
            )
        raw = raw_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != data["raw_file_hash"]:
            self.cancel_preview(preview_id)
            raise ImportWorkflowError(
                "PREVIEW_CORRUPTED", "Preview integrity check failed.", status_code=409
            )
        if data.get("project_id") != project_id:
            raise ImportWorkflowError(
                "PREVIEW_PROJECT_MISMATCH",
                "The target project changed; upload the file again to refresh its UMIF scope and duplicate check.",
                status_code=409,
            )
        parsed = data["parsed"]
        if data["source_type"] == "claude":
            from everos.umif.package import read_package
            frozen = self.preview_dir / f"{preview_id}.umif"
            try:
                package = read_package(frozen)
            except (OSError, ValueError) as exc:
                raise ImportWorkflowError('PREVIEW_CORRUPTED', 'UMIF preview is unreadable.', status_code=409) from exc
            snapshot_hashes = {
                canonical_content_hash(package.manifest.model_dump(mode='json')),
                hashlib.sha256(package.manifest.model_dump_json().encode()).hexdigest(),
            }
            if data.get('umif_snapshot_hash') not in snapshot_hashes:
                raise ImportWorkflowError("PREVIEW_CORRUPTED", "UMIF preview snapshot changed.", status_code=409)
        else:
            package, _ = self._convert_to_umif(data["filename"], data["source_type"], raw, project_id)
        from everos.umif.package import write_package
        from everos.umif.validator import validate_package
        validation = validate_package(package)
        if not validation.valid or package.manifest.package_id != data.get("umif_package_id"):
            raise ImportWorkflowError("UMIF_VALIDATION_FAILED", "UMIF preview changed or failed validation.", status_code=409)
        if parsed.get("source_event_conflict_count", 0):
            raise ImportWorkflowError(
                "IMPORT_SOURCE_EVENT_CONFLICT",
                "A DeepSeek Harness source event ID already exists with different content.",
                status_code=409,
            )
        batch_id = uuid.uuid4().hex
        batch_dir = self.store.evidence_dir / batch_id
        batch_dir.mkdir(parents=True, exist_ok=False)
        target = batch_dir / data["filename"]
        umif_dir = batch_dir / "umif"
        try:
            temporary = batch_dir / f".{data['filename']}.tmp"
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            write_package(package, umif_dir)
            source_dir = self.preview_dir / f"{preview_id}.source"
            if data.get('source_file_hashes') and not source_dir.is_dir():
                raise ImportWorkflowError('PREVIEW_CORRUPTED', 'Original evidence is missing.', status_code=409)
            if data["source_type"] == "claude" and source_dir.is_dir():
                import shutil
                shutil.copytree(source_dir, batch_dir / "source")
                from .export_downloader import ExportDownloader
                for name, expected in data.get('source_file_hashes', {}).items():
                    actual = batch_dir / 'source' / self.safe_filename(name)
                    if not actual.is_file() or ExportDownloader.checksum(actual) != expected:
                        raise ImportWorkflowError('PREVIEW_CORRUPTED', 'Original evidence changed after preview.', status_code=409)
            disk_validation = validate_package(package, umif_dir)
            if not disk_validation.valid:
                raise ImportWorkflowError("UMIF_VALIDATION_FAILED", ", ".join(disk_validation.errors))
            with self.store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if data["source_type"] in {"deepseek_harness", "claude", "chatgpt"}:
                    duplicate = connection.execute(
                        """SELECT id FROM import_batches
                           WHERE file_sha256 = ? AND project_id = ? AND parser_version = ?
                           LIMIT 1""",
                        (data["raw_file_hash"], project_id, parsed["parser_version"]),
                    ).fetchone()
                else:
                    duplicate = connection.execute(
                        "SELECT id FROM import_batches WHERE file_sha256 = ? LIMIT 1",
                        (data["raw_file_hash"],),
                    ).fetchone()
                if duplicate:
                    raise ImportWorkflowError(
                        "IMPORT_DUPLICATE_FILE",
                        "This file has already been archived.",
                        status_code=409,
                    )
                duplicate_message_count = 0
                duplicate_tool_event_count = 0
                if data["source_type"] in {"deepseek_harness", "claude", "chatgpt"}:
                    known_message_ids: dict[str, list[tuple[str, str]]] = {}
                    for row in connection.execute(
                        """SELECT m.source_message_id, m.content_sha256, b.parser_version
                           FROM normalized_messages m JOIN import_batches b ON b.id = m.batch_id
                           WHERE m.source_message_id IS NOT NULL AND b.project_id = ? AND b.source_type = ?""",
                        (project_id, data["source_type"]),
                    ).fetchall():
                        known_message_ids.setdefault(row[0], []).append((row[1], row[2]))
                    known_tool_event_ids = {
                        row[0]: (row[1], row[2])
                        for row in connection.execute(
                            "SELECT source_event_id, content_sha256, source_content_sha256 FROM normalized_tool_events "
                            "WHERE batch_id IN (SELECT id FROM import_batches WHERE project_id = ? AND source_type = ?)",
                            (project_id, data["source_type"]),
                        ).fetchall()
                    }
                    for message in parsed["messages"]:
                        prior_versions = known_message_ids.get(message["source_message_id"] or "", [])
                        if any(content_hash != message["content_hash"] for content_hash, _ in prior_versions):
                            raise ImportWorkflowError(
                                "IMPORT_SOURCE_EVENT_CONFLICT",
                                "A DeepSeek Harness source event ID already exists with different content.",
                                status_code=409,
                            )
                        message["duplicate"] = any(
                            content_hash == message["content_hash"]
                            and version == parsed["parser_version"]
                            for content_hash, version in prior_versions
                        )
                        duplicate_message_count += int(message["duplicate"])
                    for event in parsed["tool_events"]:
                        old_hashes = known_tool_event_ids.get(event["source_event_id"])
                        old_hash = (old_hashes[1] or old_hashes[0]) if old_hashes else None
                        new_hash = event.get("source_content_hash") if old_hashes and old_hashes[1] else event["content_hash"]
                        if old_hash is not None and old_hash != new_hash:
                            raise ImportWorkflowError(
                                "IMPORT_SOURCE_EVENT_CONFLICT",
                                "A DeepSeek Harness source event ID already exists with different content.",
                                status_code=409,
                            )
                        event["duplicate"] = old_hash == new_hash
                        duplicate_tool_event_count += int(event["duplicate"])
                else:
                    known_message_hashes = {
                        row[0]
                        for row in connection.execute(
                            "SELECT DISTINCT content_sha256 FROM normalized_messages"
                        ).fetchall()
                    }
                    seen_message_hashes: set[str] = set()
                    for message in parsed["messages"]:
                        is_duplicate = (
                            message["content_hash"] in known_message_hashes
                            or message["content_hash"] in seen_message_hashes
                        )
                        message["duplicate"] = is_duplicate
                        seen_message_hashes.add(message["content_hash"])
                        duplicate_message_count += int(is_duplicate)
                parsed["duplicate_message_count"] = duplicate_message_count
                parsed["duplicate_tool_event_count"] = duplicate_tool_event_count
                archived_at = get_utc_now().isoformat()
                connection.execute(
                    """INSERT INTO import_batches
                       (id, source_name, source_type, original_filename,
                        file_sha256, file_size, parser_version, namespace,
                        app_id, project_id, user_id, agent_id,
                        status, extraction_status, conversation_count, message_count,
                        duplicate_message_count, raw_relpath, archived_at,
                        umif_package_relpath, umif_package_id, umif_record_count, umif_validation_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived',
                               'not_started', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        batch_id,
                        data["filename"],
                        data["source_type"],
                        data["filename"],
                        data["raw_file_hash"],
                        data["file_size"],
                        parsed.get("parser_version", PARSER_VERSION),
                        project_id,
                        identity["app_id"],
                        project_id,
                        identity["user_id"],
                        identity["agent_id"],
                        parsed["conversation_count"],
                        len(parsed["messages"]),
                        parsed["duplicate_message_count"],
                        str(target.relative_to(self.store.data_dir)),
                        archived_at,
                        str(umif_dir.relative_to(self.store.data_dir)),
                        package.manifest.package_id,
                        validation.record_count,
                        json.dumps({"errors": validation.errors, "warnings": validation.warnings}),
                    ),
                )
                connection.execute(
                    """INSERT INTO evidence_files
                       (id, batch_id, relative_path, sha256, media_type, original_name)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        batch_id,
                        str(target.relative_to(self.store.data_dir)),
                        data["raw_file_hash"],
                        {
                            ".json": "application/json",
                            ".jsonl": "application/x-ndjson",
                            ".zip": "application/zip",
                        }.get(target.suffix.lower(), "text/plain"),
                        data["filename"],
                    ),
                )
                for record in (
                    *package.principals,
                    *package.sessions,
                    *package.events,
                    *(r for rows in package.optional_records.values() for r in rows),
                ):
                    connection.execute(
                        """INSERT INTO umif_record_map
                           (batch_id, source_record_id, content_sha256, target_type, status)
                           VALUES (?, ?, ?, ?, 'archived')""",
                        (batch_id, record.record_id,
                         record.provenance["content_sha256"], record.record_type),
                    )
                source_records = {
                    (record.record_type, record.source.get("native_id")): record
                    for record in package.events
                }
                if data["source_type"] == "claude":
                    source_records.update({(record.record_type, record.record_id): record for record in package.events})
                generic_records = {
                    (record.scope.get("session_id"), record.sequence): record
                    for record in package.events if record.record_type == "event.message"
                }
                generic_sessions = {
                    session.payload.get("native_session_id"): session.record_id
                    for session in package.sessions
                }
                for message in parsed["messages"]:
                    target_id = uuid.uuid4().hex
                    connection.execute(
                        """INSERT INTO normalized_messages
                           (id, batch_id, conversation_id, ordinal, role, content,
                            timestamp_ms, content_sha256,
                            source_message_id, is_duplicate, memory_input)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            target_id,
                            batch_id,
                            message["conversation_id"],
                            message["ordinal"],
                            message["role"],
                            message["content"],
                            message["timestamp_ms"],
                            message["content_hash"],
                            message["source_message_id"],
                            int(message["duplicate"]),
                            int(message["memory_input"]),
                        ),
                    )
                    record = source_records.get(("event.message", message["source_message_id"]))
                    if record is None and data["source_type"] == "generic":
                        record = generic_records.get((
                            generic_sessions.get(message["conversation_id"]),
                            message["ordinal"],
                        ))
                    if record is not None:
                        connection.execute(
                            """UPDATE umif_record_map SET target_id = ?, status = ?
                               WHERE batch_id = ? AND source_record_id = ?""",
                            (target_id, "duplicate" if message["duplicate"] else "projected",
                             batch_id, record.record_id),
                        )
                for event in parsed["tool_events"]:
                    target_id = uuid.uuid4().hex
                    connection.execute(
                        """INSERT INTO normalized_tool_events
                           (id, batch_id, conversation_id, ordinal, source_event_id,
                            source_event_seq, event_type, timestamp_ms, turn, step,
                            call_id, tool_name, arguments_json, result_content_json,
                            result_text, is_error, source_event_seqs_json,
                            content_sha256, is_duplicate, source_content_sha256)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            target_id,
                            batch_id,
                            event["conversation_id"],
                            event["ordinal"],
                            event["source_event_id"],
                            event["source_event_seq"],
                            event["event_type"],
                            event["timestamp_ms"],
                            event["turn"],
                            event["step"],
                            event["call_id"],
                            event["tool_name"],
                            json.dumps(event["arguments"], ensure_ascii=False, sort_keys=True)
                            if event["arguments"] is not None
                            else None,
                            json.dumps(event["result_content"], ensure_ascii=False)
                            if event["result_content"] is not None
                            else None,
                            event["result_text"],
                            int(event["is_error"])
                            if event["is_error"] is not None
                            else None,
                            json.dumps(event["source_event_seqs"]),
                            event["content_hash"],
                            int(event["duplicate"]),
                            event.get("source_content_hash"),
                        ),
                    )
                    record = source_records.get((
                        "event.tool_call" if event["event_type"] == "tool/call" else "event.tool_result",
                        event["source_event_id"],
                    ))
                    if record is not None:
                        connection.execute(
                            """UPDATE umif_record_map SET target_id = ?, status = ?
                               WHERE batch_id = ? AND source_record_id = ?""",
                            (target_id, "duplicate" if event["duplicate"] else "projected",
                             batch_id, record.record_id),
                        )
                if data.get("source_job_id"):
                    connection.execute("UPDATE import_jobs SET status='committed',batch_id=? WHERE id=?", (batch_id, data["source_job_id"]))
                connection.execute(
                    """INSERT INTO import_events (id, batch_id, event_type)
                       VALUES (?, ?, 'archived')""",
                    (uuid.uuid4().hex, batch_id),
                )
        except Exception:
            import shutil
            shutil.rmtree(batch_dir, ignore_errors=True)
            raise
        self.cancel_preview(preview_id)
        return {
            "batch_id": batch_id,
            "source_type": data["source_type"],
            "original_filename": data["filename"],
            "app_id": identity["app_id"],
            "project_id": project_id,
            "user_id": identity["user_id"],
            "agent_id": identity["agent_id"],
            "raw_file_hash": data["raw_file_hash"],
            "umif_package_id": package.manifest.package_id,
            "umif_record_count": validation.record_count,
            "conversation_count": parsed["conversation_count"],
            "message_count": len(parsed["messages"]),
            "duplicate_message_count": parsed["duplicate_message_count"],
            "tool_call_count": parsed["tool_call_count"],
            "tool_result_count": parsed["tool_result_count"],
            "duplicate_tool_event_count": parsed["duplicate_tool_event_count"],
            "archived_at": archived_at,
            "extraction_status": "not_started",
        }


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_unpacked_bytes: int = DEFAULT_MAX_ARCHIVE_UNPACKED_BYTES
    max_files: int = DEFAULT_MAX_ARCHIVE_FILES
    max_compression_ratio: int = DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO


def read_harness_export(payload: bytes, filename: str, limits: ImportLimits) -> bytes:
    """Safely select the sole Harness JSONL member without writing to disk."""
    extension = Path(filename.replace("\\", "/")).suffix.lower()
    if len(payload) > limits.max_file_bytes:
        raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "File exceeds the upload limit.", status_code=413)
    if extension == ".jsonl":
        return payload
    if extension != ".zip":
        raise ImportWorkflowError("IMPORT_PARSE_FAILED", "Harness requires a .jsonl or .zip file.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Invalid ZIP archive.") from exc
    with archive:
        entries = archive.infolist()
        if not entries or len(entries) > limits.max_files:
            raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "ZIP entry count is invalid.")
        seen: set[str] = set()
        member = None
        total = 0
        for info in entries:
            path = _archive_member_path(info.filename)
            if path.as_posix() in seen:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Duplicate ZIP path.")
            seen.add(path.as_posix())
            mode = (info.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or (kind and kind not in {stat.S_IFREG, stat.S_IFDIR}):
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Unsafe ZIP member type.")
            if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Unsupported ZIP member encoding.")
            total += info.file_size
            if total > limits.max_unpacked_bytes:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "ZIP unpacked size exceeds limit.", status_code=413)
            if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > limits.max_compression_ratio:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Suspicious ZIP compression ratio.", status_code=413)
            if info.is_dir(): continue
            if path.suffix.lower() in _NESTED_ARCHIVE_EXTENSIONS:
                raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "Nested archive is not allowed.")
            if path.name != "session.v4.jsonl" or member is not None:
                raise ImportWorkflowError("IMPORT_PARSE_FAILED", "Harness ZIP must contain exactly one session.v4.jsonl file.")
            member = info
        if member is None:
            raise ImportWorkflowError("IMPORT_PARSE_FAILED", "Harness ZIP has no session.v4.jsonl file.")
        try:
            with archive.open(member) as stream:
                data = stream.read(limits.max_unpacked_bytes + 1)
        except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, zlib.error) as exc:
            raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "ZIP member cannot be safely decoded.") from exc
        if len(data) != member.file_size or len(data) > limits.max_unpacked_bytes:
            raise ImportWorkflowError("IMPORT_SECURITY_REJECTED", "ZIP member size mismatch.", status_code=413)
        return data


def convert_parsed_markdown(
    parsed: ParsedImport, project_id: str, *, source_identity: str | None = None
):
    """Convert the existing generic parser output through the same UMIF contract."""
    from everos.umif.canonical import content_hash, record_id
    from everos.umif.models import Manifest, Message, Principal, Session, UmifPackage
    from everos.umif.package import seal_package
    source = {"provider": "generic", "product": "memo-vault"}
    if source_identity is None:
        source_identity = content_hash([
            (message.conversation_id, message.ordinal, message.role,
             message.content, message.source_message_id)
            for message in parsed.messages
        ])
    def session_native(cid: str) -> str:
        return f"{source_identity}:{cid}"
    principal_ids = {role: record_id("generic", "principal", role) for role in ("user", "assistant")}
    def common(cls, typ, native, seq, scope, body, timestamp=None):
        from datetime import UTC, datetime
        return cls(record_id=record_id("generic", typ, native), source={**source, "native_type": typ, "native_id": native},
                   scope=scope, sequence=seq, timestamp_ms=timestamp,
                   occurred_at=datetime.fromtimestamp(timestamp / 1000, UTC).isoformat().replace("+00:00", "Z") if timestamp is not None else None,
                   provenance={"adapter": "memo-vault-generic", "adapter_version": "1.0.0", "content_sha256": content_hash(body), "time_quality": "source" if timestamp is not None else "unknown"}, payload=body)
    base_scope = {"workspace_id": "personal", "project_id": project_id, "session_id": None, "branch_id": "main"}
    principals = tuple(common(Principal, "principal", role, i, base_scope, {"principal_id": pid, "kind": "person" if role == "user" else "agent"}) for i, (role, pid) in enumerate(principal_ids.items()))
    conversation_ids = tuple(dict.fromkeys(m.conversation_id for m in parsed.messages))
    sessions = tuple(common(Session, "session", session_native(cid), i, base_scope,
                            {"session_id": record_id("generic", "session", session_native(cid)),
                             "session_kind": "conversation", "native_session_id": cid})
                     for i, cid in enumerate(conversation_ids))
    events = []
    for m in parsed.messages:
        native = f"{session_native(m.conversation_id)}:{m.source_message_id or m.ordinal}"
        body = {"message_id": native, "author_principal_id": principal_ids.get(m.role, principal_ids["assistant"]),
                "role": m.role, "content_parts": [{"type": "text", "text": m.content}]}
        scope = {**base_scope, "session_id": record_id("generic", "session", session_native(m.conversation_id))}
        events.append(common(Message, "message", native, m.ordinal, scope, body, m.timestamp_ms).model_copy(
            update={"extensions": {"memo_vault_generic": {"source_message_id": m.source_message_id}}}
        ))
    manifest = Manifest(package_id=record_id("generic", "package", source_identity + ":" + project_id),
                        generator={"name": "everos-umif", "version": "0.1.0"}, source_exports=[source], default_scope=base_scope, files=[])
    return seal_package(UmifPackage(manifest=manifest, principals=principals, sessions=sessions, events=tuple(events), import_report={"skipped_event_counts": parsed.skipped_event_counts or {}, "warnings": list(parsed.warnings)}))
