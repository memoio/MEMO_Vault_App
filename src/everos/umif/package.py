"""Write and read immutable UMIF JSON/JSONL package directories."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .canonical import canonical_bytes
from .models import (
    FileEntry,
    Manifest,
    Message,
    Principal,
    Session,
    ToolCall,
    ToolResult,
    UmifPackage,
    UmifRecord,
)

_REQUIRED = ("principals.jsonl", "sessions.jsonl", "events.jsonl")
_OPTIONAL = ("assets.jsonl", "documents.jsonl", "memories.jsonl", "feedback.jsonl")


def _records(package: UmifPackage, name: str) -> tuple[UmifRecord, ...]:
    if name == "principals.jsonl":
        return package.principals
    if name == "sessions.jsonl":
        return package.sessions
    if name == "events.jsonl":
        return package.events
    return package.optional_records.get(name, ())


def _file_bytes(records: tuple[UmifRecord, ...]) -> bytes:
    return b"".join(
        canonical_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )


def seal_package(package: UmifPackage) -> UmifPackage:
    names = (
        *_REQUIRED,
        *(name for name in _OPTIONAL if name in package.optional_records),
    )
    entries = [
        FileEntry(
            path=name,
            sha256="sha256:"
            + hashlib.sha256(_file_bytes(_records(package, name))).hexdigest(),
            record_count=len(_records(package, name)),
        )
        for name in names
    ]
    return package.model_copy(
        update={"manifest": package.manifest.model_copy(update={"files": entries})}
    )


def write_package(package: UmifPackage, directory: Path) -> None:
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("UMIF package directory is not empty")
    directory.mkdir(parents=True, exist_ok=True)
    package = seal_package(package)
    for entry in package.manifest.files:
        (directory / entry.path).write_bytes(_file_bytes(_records(package, entry.path)))
    for relative, data in package.blobs.items():
        import re

        if not re.fullmatch(r"blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}", relative):
            raise ValueError("unsafe blob path")
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (directory / "manifest.json").write_bytes(
        canonical_bytes(package.manifest.model_dump(mode="json")) + b"\n"
    )
    (directory / "import-report.json").write_bytes(
        canonical_bytes(package.import_report) + b"\n"
    )


def read_package(directory: Path) -> UmifPackage:
    manifest = Manifest.model_validate_json((directory / "manifest.json").read_bytes())

    def rows(name: str, model: type[UmifRecord]) -> tuple[UmifRecord, ...]:
        path = directory / name
        if not path.is_file():
            return ()
        return tuple(
            model.model_validate_json(line)
            for line in path.read_bytes().splitlines()
            if line
        )

    events = []
    for line in (directory / "events.jsonl").read_bytes().splitlines():
        data = json.loads(line)
        model = {
            "event.message": Message,
            "event.tool_call": ToolCall,
            "event.tool_result": ToolResult,
        }.get(data.get("record_type"))
        if model is None:
            raise ValueError("unsupported UMIF event type")
        events.append(model.model_validate(data))
    optional = {
        name: rows(name, UmifRecord)
        for name in _OPTIONAL
        if (directory / name).is_file()
    }
    report_path = directory / "import-report.json"
    report = json.loads(report_path.read_bytes()) if report_path.is_file() else {}
    blobs = {}
    for record in optional.get("assets.jsonl", ()):
        relative = record.payload.get("blob_path")
        if relative:
            import re

            if not isinstance(relative, str) or not re.fullmatch(
                r"blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}", relative
            ):
                raise ValueError("unsafe blob path")
            target = directory / relative
            if not target.resolve().is_relative_to(directory.resolve()):
                raise ValueError("unsafe blob path")
            if target.is_file():
                blobs[relative] = target.read_bytes()
    return UmifPackage(
        blobs=blobs,
        manifest=manifest,
        principals=rows("principals.jsonl", Principal),
        sessions=rows("sessions.jsonl", Session),
        events=tuple(events),
        optional_records=optional,
        import_report=report,
    )
