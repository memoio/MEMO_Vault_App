"""Project-scoped downloads of Knowledge source files and derived Markdown."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from everos.core.errors import DocumentNotFoundError, InvalidInputError
from everos.core.persistence import MemoryRoot
from everos.infra.persistence.sqlite import knowledge_document_repo

_DOC_ID = re.compile(r"^d_[a-f0-9]{12,32}$")
_MAX_FILES = 100
_MAX_BYTES = 512 * 1024 * 1024


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _filename(name: str | None) -> str:
    safe = (name or "").replace("\\", "/").split("/")[-1]
    if safe in {"", ".", ".."} or "\x00" in safe:
        raise DocumentNotFoundError("Knowledge original is unavailable")
    return safe


async def _source(doc_id: str, app_id: str, project_id: str):
    if not _DOC_ID.fullmatch(doc_id):
        raise InvalidInputError("Invalid Knowledge document ID")
    row = await knowledge_document_repo.get_by_doc_id(doc_id)
    if row is None or row.app_id != app_id or row.project_id != project_id:
        raise DocumentNotFoundError("Knowledge document not found in this project")
    root = MemoryRoot.resolve()
    knowledge_dir = root.knowledge_dir(app_id, project_id).resolve()
    md_path = Path(row.md_path)
    if not md_path.is_absolute():
        md_path = root.root / md_path
    doc_dir = md_path.parent.resolve()
    if not _inside(doc_dir, knowledge_dir) or not (
        doc_dir.name == doc_id or doc_dir.name.endswith("_" + doc_id)
    ):
        raise DocumentNotFoundError("Knowledge original is unavailable")
    return row, doc_dir


async def resolve_original(doc_id: str, app_id: str, project_id: str) -> tuple[Path, str]:
    """Find the original file after validating the database and filesystem scope."""
    row, directory = await _source(doc_id, app_id, project_id)
    name = _filename(row.source_name)
    originals = directory / "_original"
    if not _inside(originals.resolve(), directory):
        raise DocumentNotFoundError("Knowledge original is unavailable")
    path = (originals / name).resolve()
    if not _inside(path, originals.resolve()) or not path.is_file():
        raise DocumentNotFoundError("Knowledge original is unavailable")
    return path, name


async def prepare_zip(
    doc_ids: list[str], app_id: str, project_id: str, *, include_derived: bool = False
) -> Path:
    """Build a bounded archive of selected source files, with optional Markdown."""
    if not doc_ids or len(doc_ids) > _MAX_FILES or len(set(doc_ids)) != len(doc_ids):
        raise InvalidInputError("Select between 1 and 100 distinct documents")
    files: list[tuple[Path, str, dict]] = []
    manifest: dict = {"format": "everos-knowledge-export-v1", "app_id": app_id,
                      "project_id": project_id, "documents": []}
    total = 0
    for doc_id in doc_ids:
        row, directory = await _source(doc_id, app_id, project_id)
        original, name = await resolve_original(doc_id, app_id, project_id)
        sources = [(original, f"originals/{doc_id}/{name}")]
        if include_derived:
            sources.extend(
                (file, f"processed/{doc_id}/{file.name}")
                for file in sorted(directory.glob("*.md"))
                if file.is_file() and _inside(file.resolve(), directory)
            )
        entry = {"doc_id": doc_id, "title": row.title, "category_id": row.category_id,
                 "source_name": name, "files": []}
        for file, archive_name in sources:
            size = file.stat().st_size
            total += size
            if total > _MAX_BYTES:
                raise InvalidInputError("Knowledge export exceeds the 512 MiB limit")
            files.append((file, archive_name, entry))
        manifest["documents"].append(entry)
    fd, name = tempfile.mkstemp(prefix="everos-knowledge-", suffix=".zip")
    os.close(fd)
    output = Path(name)
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            written = 0
            knowledge_root = MemoryRoot.resolve().knowledge_dir(app_id, project_id).resolve()
            for file, archive_name, entry in files:
                if not _inside(file.resolve(), knowledge_root):
                    raise DocumentNotFoundError("Knowledge original is unavailable")
                digest = hashlib.sha256()
                size = 0
                with file.open("rb") as source, archive.open(archive_name, "w") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        written += len(chunk)
                        size += len(chunk)
                        if written > _MAX_BYTES:
                            raise InvalidInputError("Knowledge export exceeds the 512 MiB limit")
                        digest.update(chunk)
                        target.write(chunk)
                entry["files"].append({"path": archive_name, "bytes": size,
                                       "sha256": digest.hexdigest()})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return output
    except BaseException:
        output.unlink(missing_ok=True)
        raise
