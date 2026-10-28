"""Pre-projection integrity validation for UMIF packages."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .canonical import content_hash
from .models import ToolCall, ToolResult, UmifPackage
from .package import _file_bytes, _records


@dataclass(frozen=True)
class ValidationReport:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    record_count: int

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_package(
    package: UmifPackage, directory: Path | None = None
) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    records = (
        *package.principals,
        *package.sessions,
        *package.events,
        *(r for group in package.optional_records.values() for r in group),
    )
    ids: set[str] = set()
    seqs: set[tuple[str, str, str, str, int]] = set()
    principals = {r.record_id for r in package.principals}
    sessions = {r.record_id for r in package.sessions}
    calls = {
        (r.scope.get("session_id"), r.payload.get("call_id"))
        for r in package.events
        if isinstance(r, ToolCall)
    }
    for record in records:
        if record.record_id in ids:
            errors.append("DUPLICATE_RECORD_ID")
        ids.add(record.record_id)
        group = (
            "event" if record.record_type.startswith("event.") else record.record_type
        )
        key = (
            str(record.scope.get("workspace_id")),
            str(record.scope.get("project_id")),
            str(record.scope.get("session_id")),
            group,
            record.sequence,
        )
        if key in seqs:
            errors.append("DUPLICATE_SEQUENCE")
        seqs.add(key)
        if record.provenance.get("content_sha256") != content_hash(record.payload):
            errors.append("CONTENT_HASH_MISMATCH")
        if (
            record.record_type.startswith("event.")
            and record.scope.get("session_id") not in sessions
        ):
            errors.append("MISSING_SESSION")
        principal_id = record.payload.get("author_principal_id") or record.payload.get(
            "tool_principal_id"
        )
        if principal_id and principal_id not in principals:
            errors.append("MISSING_PRINCIPAL")
        if (
            isinstance(record, ToolResult)
            and (record.scope.get("session_id"), record.payload.get("call_id"))
            not in calls
        ):
            warnings.append("ORPHAN_TOOL_RESULT")
        if isinstance(record, ToolCall) and record.payload.get("call_id") is None:
            errors.append("MISSING_TOOL_CALL_ID")
        if (
            record.record_type == "asset"
            and record.payload.get("blob_path") is not None
        ):
            blob_path = record.payload["blob_path"]
            if (
                not isinstance(blob_path, str)
                or not re.fullmatch(r"blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}", blob_path)
                or (
                    directory is not None
                    and not (directory / blob_path)
                    .resolve()
                    .is_relative_to(directory.resolve())
                )
            ):
                errors.append("UNSAFE_BLOB_PATH")
    assets = {r.record_id for r in package.optional_records.get("assets.jsonl", ())}
    for record in records:
        if record.record_type == "asset" and record.payload.get("blob_path"):
            relative = record.payload["blob_path"]
            data = package.blobs.get(relative)
            if data is None:
                errors.append("MISSING_BLOB")
            elif record.payload.get("sha256") != "sha256:" + hashlib.sha256(
                data
            ).hexdigest() or record.payload.get("size_bytes") != len(data):
                errors.append("BLOB_HASH_MISMATCH")
            if directory is not None and "UNSAFE_BLOB_PATH" not in errors:
                target = directory / relative
                if not target.is_file() or target.read_bytes() != data:
                    errors.append("BLOB_FILE_MISMATCH")
        if record.record_type == "document":
            asset = record.payload.get("asset_id")
            if asset and asset not in assets:
                errors.append("MISSING_DOCUMENT_ASSET")
    listed: set[str] = set()
    for entry in package.manifest.files:
        path = PurePosixPath(entry.path)
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
            errors.append("UNSAFE_FILE_PATH")
            continue
        listed.add(entry.path)
        actual = _file_bytes(_records(package, entry.path))
        if entry.sha256 != "sha256:" + hashlib.sha256(actual).hexdigest():
            errors.append("FILE_HASH_MISMATCH")
        if entry.record_count != len(_records(package, entry.path)):
            errors.append("FILE_COUNT_MISMATCH")
        if directory is not None:
            disk_path = directory / entry.path
            if not disk_path.is_file() or disk_path.read_bytes() != actual:
                errors.append("PACKAGE_FILE_MISMATCH")
    if not {"principals.jsonl", "sessions.jsonl", "events.jsonl"}.issubset(listed):
        errors.append("MISSING_REQUIRED_FILE")
    if set(package.optional_records) - listed:
        errors.append("UNLISTED_OPTIONAL_FILE")
    if set(package.optional_records) - {
        "assets.jsonl",
        "documents.jsonl",
        "memories.jsonl",
        "feedback.jsonl",
    }:
        errors.append("UNSUPPORTED_OPTIONAL_FILE")
    return ValidationReport(
        tuple(sorted(set(errors))), tuple(sorted(set(warnings))), len(records)
    )
