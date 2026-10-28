"""Metadata-only inventory of archived Claude ZIP parts and their members."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any


def source_members(store: Any, batch_id: str, package: Any, *, limit: int = 1000) -> dict[str, Any]:
    with store.connect() as connection:
        job = connection.execute(
            "SELECT id FROM import_jobs WHERE batch_id=? ORDER BY created_at DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
    if job is None:
        return {"files": [], "total": 0, "truncated": False}
    root = (store.data_dir / "import-jobs" / job["id"]).resolve()
    if not root.is_relative_to((store.data_dir / "import-jobs").resolve()):
        return {"files": [], "total": 0, "truncated": False}
    return source_members_from_job(root, package, limit=limit)


def source_members_from_job(root: Path, package: Any, *, limit: int = 1000) -> dict[str, Any]:
    recognized = {
        (record.provenance.get("archive_name"), record.provenance.get("member_path"))
        for record in (*package.principals, *package.sessions, *package.events,
                       *(r for group in package.optional_records.values() for r in group))
    }
    files: list[dict[str, Any]] = []
    total = 0
    for source in package.manifest.source_exports:
        name = source.get("archive_name")
        if not isinstance(name, str) or Path(name).name != name:
            continue
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            files.append({"name": name, "status": "missing_local_part"})
            total += 1
            continue
        files.append({"name": name, "bytes": path.stat().st_size,
                      "sha256": source.get("archive_sha256"), "status": "archived_zip"})
        total += 1
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    total += 1
                    if len(files) < limit:
                        files.append({"name": member.filename, "container": name,
                                      "bytes": member.file_size,
                                      "status": "mapped_to_umif" if (name, member.filename) in recognized
                                      else "retained_in_source"})
        except (OSError, zipfile.BadZipFile):
            files.append({"name": name, "status": "archive_unreadable"})
    return {"files": files[:limit], "total": total, "truncated": total > limit}
