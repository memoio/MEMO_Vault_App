"""Durable Claude acquisition jobs with restart-safe export caching."""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import portalocker

from everos.umif.canonical import canonical_bytes
from everos.umif.package import read_package, write_package
from everos.umif.validator import validate_package

from .claude_export import convert_claude_export
from .claude_manifest import parse_manifest
from .export_downloader import ExportDownloader, write_private
from .import_contracts import ImportWorkflowError
from .imports import ImportService, validate_project_id
from .storage import VaultStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_jobs (
 id TEXT PRIMARY KEY, status TEXT NOT NULL, project_id TEXT NOT NULL,
 filename TEXT NOT NULL, error_code TEXT, error_message TEXT,
 batch_id TEXT,
 progress_json TEXT NOT NULL DEFAULT '{}', preview_id TEXT,
 cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

MAX_LOCAL_ZIPS = 999  # Multipart parser allows 1000 file parts, including manifest.


class ImportJobManager:
    """Persist work in SQLite and serialize each job with a process lock."""

    def __init__(
        self,
        store: VaultStore,
        imports: ImportService,
        downloader: ExportDownloader | None = None,
    ) -> None:
        self.store, self.imports = store, imports
        self.stopping = threading.Event()
        self.downloader = downloader or ExportDownloader(
            max_bytes=int(
                os.environ.get(
                    "MEMO_VAULT_CLAUDE_MAX_DOWNLOAD_BYTES", str(512 * 1024 * 1024)
                )
            )
        )
        self.max_total_bytes = int(
            os.environ.get(
                "MEMO_VAULT_CLAUDE_MAX_TOTAL_BYTES", str(2 * 1024 * 1024 * 1024)
            )
        )
        self.max_unpacked_bytes = int(
            os.environ.get(
                "MEMO_VAULT_CLAUDE_MAX_UNPACKED_BYTES", str(512 * 1024 * 1024)
            )
        )
        self.max_files = int(os.environ.get("MEMO_VAULT_CLAUDE_MAX_FILES", "10000"))
        self.root = store.data_dir / "import-jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        with store.connect() as connection:
            connection.executescript(_SCHEMA)
            if "batch_id" not in {
                r[1] for r in connection.execute("PRAGMA table_info(import_jobs)")
            }:
                connection.execute("ALTER TABLE import_jobs ADD COLUMN batch_id TEXT")

    def path(self, job_id: str) -> Path:
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ImportWorkflowError(
                "IMPORT_JOB_NOT_FOUND", "Import job not found.", status_code=404
            )
        return self.root / job_id

    def status(self, job_id: str) -> dict[str, Any]:
        self.path(job_id)
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise ImportWorkflowError(
                "IMPORT_JOB_NOT_FOUND", "Import job not found.", status_code=404
            )
        value = dict(row)
        value["progress"] = json.loads(value.pop("progress_json"))
        if value["filename"] == "local-bundle.json":
            value["input_kind"] = "local_bundle"
            value["part_count"] = len(
                [name for name in value["progress"] if name != "report"]
            )
            value["manifest_verified"] = (self.path(job_id) / "manifest.json").is_file()
        elif value["filename"] == "manifest.json":
            value["input_kind"] = "manifest"
            value["part_count"] = len(
                parse_manifest((self.path(job_id) / "manifest.json").read_bytes())
            )
        else:
            value["input_kind"] = "single_zip"
            value["part_count"] = 1
        value["retryable"] = value["error_code"] in {
            "CLAUDE_DOWNLOAD_INTERRUPTED",
            "CLAUDE_DOWNLOAD_FAILED",
            "IMPORT_JOB_INTERRUPTED",
        }
        return value

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            ids = [
                r[0]
                for r in connection.execute(
                    "SELECT id FROM import_jobs "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 100"
                )
            ]
        return [self.status(job_id) for job_id in ids]

    def _update(self, job_id: str, **fields: Any) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE import_jobs SET "
                + ", ".join(f"{key}=?" for key in fields)
                + " WHERE id=?",
                (*fields.values(), job_id),
            )

    def create(self, filename: str, payload: bytes, project_id: str) -> dict[str, Any]:
        project_id = validate_project_id(project_id)
        if not self.store.has_project(project_id):
            raise ImportWorkflowError(
                "PROJECT_NOT_FOUND", "Target project does not exist.", status_code=404
            )
        if len(payload) > self.downloader.max_bytes:
            raise ImportWorkflowError(
                "IMPORT_SECURITY_REJECTED",
                "Upload size limit exceeded.",
                status_code=413,
            )
        filename = self.imports.safe_filename(filename)
        if filename.endswith(".json"):
            if len(payload) > 2 * 1024 * 1024:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "Manifest exceeds the 2 MiB limit.",
                    status_code=413,
                )
            parse_manifest(payload)
        elif not filename.endswith(".zip"):
            raise ImportWorkflowError(
                "CLAUDE_INVALID_EXPORT", "Upload a Claude manifest JSON or ZIP."
            )
        job_id = uuid.uuid4().hex
        directory = self.path(job_id)
        directory.mkdir(mode=0o700)
        target = directory / (
            "manifest.json" if filename.endswith(".json") else filename
        )
        write_private(target, payload)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO "
                "import_jobs(id,status,project_id,filename) VALUES (?,'queued',?,?)",
                (job_id, project_id, target.name),
            )
        return self.status(job_id)

    def create_local_bundle(
        self,
        files: Sequence[tuple[str, Path]],
        project_id: str,
        *,
        manifest: bytes | None = None,
    ) -> dict[str, Any]:
        """Stage several local ZIPs as one durable, URL-free Claude import."""
        project_id = validate_project_id(project_id)
        if not self.store.has_project(project_id):
            raise ImportWorkflowError(
                "PROJECT_NOT_FOUND", "Target project does not exist.", status_code=404
            )
        if not 1 <= len(files) <= MAX_LOCAL_ZIPS:
            raise ImportWorkflowError(
                "CLAUDE_INVALID_EXPORT", "Select 1 to 999 Claude ZIP files."
            )
        parts: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        for name, path in files:
            if (
                name != self.imports.safe_filename(name)
                or not name.lower().endswith(".zip")
                or name.casefold() in seen
            ):
                raise ImportWorkflowError(
                    "CLAUDE_INVALID_EXPORT",
                    "Duplicate or unsafe ZIP filename in this selection.",
                )
            seen.add(name.casefold())
            size = path.stat().st_size
            total += size
            if size > self.downloader.max_bytes or total > self.max_total_bytes:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "Selected ZIP files exceed the import size limit.",
                    status_code=413,
                )
            if not zipfile.is_zipfile(path):
                raise ImportWorkflowError(
                    "CLAUDE_INVALID_ZIP", "A selected file is not a readable ZIP."
                )
            parts.append(
                {
                    "filename": name,
                    "size_bytes": size,
                    "sha256": self.downloader.checksum(path),
                }
            )
        if manifest is not None:
            if len(manifest) > 2 * 1024 * 1024:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "Manifest exceeds the 2 MiB limit.",
                    status_code=413,
                )
            expected = {part.filename for part in parse_manifest(manifest)}
            actual = {part["filename"] for part in parts}
            if expected != actual:
                raise ImportWorkflowError(
                    "CLAUDE_PARTS_MISMATCH",
                    "Missing ZIP or unexpected ZIP filename "
                    "compared with the manifest.",
                )
        job_id = uuid.uuid4().hex
        directory = self.path(job_id)
        directory.mkdir(mode=0o700)
        try:
            for (name, path), part in zip(files, parts, strict=True):
                target = directory / name
                shutil.copyfile(path, target)
                target.chmod(0o600)
                if (
                    target.stat().st_size != part["size_bytes"]
                    or self.downloader.checksum(target) != part["sha256"]
                ):
                    raise ImportWorkflowError(
                        "CLAUDE_CACHE_CORRUPTED", "Uploaded ZIP changed during staging."
                    )
            write_private(
                directory / "local-bundle.json",
                canonical_bytes({"version": 1, "parts": parts}),
            )
            if manifest is not None:
                write_private(directory / "manifest.json", manifest)
            progress = {
                part["filename"]: {
                    "status": "uploaded",
                    "bytes": part["size_bytes"],
                    "sha256": part["sha256"],
                }
                for part in parts
            }
            with self.store.connect() as connection:
                connection.execute(
                    "INSERT INTO import_jobs"
                    "(id,status,project_id,filename,progress_json) "
                    "VALUES (?,'queued',?,?,?)",
                    (job_id, project_id, "local-bundle.json", json.dumps(progress)),
                )
        except Exception:
            shutil.rmtree(directory)
            raise
        return self.status(job_id)

    def _local_bundle_parts(self, directory: Path) -> list[Path]:
        try:
            descriptor = json.loads((directory / "local-bundle.json").read_bytes())
            parts = descriptor["parts"]
            if descriptor["version"] != 1 or not 1 <= len(parts) <= MAX_LOCAL_ZIPS:
                raise ValueError
            names: set[str] = set()
            paths = []
            for part in parts:
                name = part["filename"]
                if (
                    not isinstance(name, str)
                    or name != self.imports.safe_filename(name)
                    or not name.lower().endswith(".zip")
                    or name.casefold() in names
                ):
                    raise ValueError
                names.add(name.casefold())
                path = directory / name
                if (
                    not path.is_file()
                    or path.stat().st_size != part["size_bytes"]
                    or self.downloader.checksum(path) != part["sha256"]
                ):
                    raise ValueError
                paths.append(path)
            return paths
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ImportWorkflowError(
                "CLAUDE_CACHE_CORRUPTED", "Local ZIP bundle is incomplete or changed."
            ) from exc

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.status(job_id)
        if job["status"] in {"ready_for_preview", "committed"}:
            raise ImportWorkflowError(
                "IMPORT_JOB_COMPLETE",
                "This job can no longer be cancelled.",
                status_code=409,
            )
        self._update(
            job_id,
            cancel_requested=1,
            **(
                {"status": "cancelled"}
                if self.status(job_id)["status"] == "queued"
                else {}
            ),
        )
        return self.status(job_id)

    def supply_part(self, job_id: str, filename: str, payload: bytes) -> dict[str, Any]:
        """Replace an unavailable manifest part with a user-downloaded ZIP."""
        job = self.status(job_id)
        if job["input_kind"] != "manifest":
            raise ImportWorkflowError(
                "CLAUDE_MANIFEST_REQUIRED",
                "Only a manifest download job can receive missing ZIP parts.",
                status_code=409,
            )
        if job["status"] in {"committed", "ready_for_preview"}:
            raise ImportWorkflowError(
                "IMPORT_JOB_COMPLETE",
                "Completed imports cannot be replaced.",
                status_code=409,
            )
        directory = self.path(job_id)
        with portalocker.Lock(directory / ".lock", timeout=0):
            manifest = directory / "manifest.json"
            if not manifest.exists():
                raise ImportWorkflowError(
                    "CLAUDE_INVALID_MANIFEST", "This job has no manifest."
                )
            entries = parse_manifest(manifest.read_bytes())
            if filename not in {e.filename for e in entries}:
                raise ImportWorkflowError(
                    "CLAUDE_UNKNOWN_PART", "Filename does not match a manifest part."
                )
            if len(payload) > self.downloader.max_bytes:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "Upload size limit exceeded.",
                    status_code=413,
                )
            target = directory / filename
            total = sum(
                p.stat().st_size for p in directory.glob("*.zip") if p.name != filename
            )
            if total + len(payload) > self.max_total_bytes:
                raise ImportWorkflowError(
                    "IMPORT_SECURITY_REJECTED",
                    "Export exceeds the total download quota.",
                    status_code=413,
                )
            write_private(target, payload)
            write_private(
                target.with_suffix(".sha256"), self.downloader.checksum(target).encode()
            )
            self._update(
                job_id,
                status="queued",
                cancel_requested=0,
                error_code=None,
                error_message=None,
                preview_id=None,
            )
        return self.status(job_id)

    def run(self, job_id: str) -> None:
        directory = self.path(job_id)
        try:
            with portalocker.Lock(directory / ".lock", timeout=0):
                self._run_locked(job_id)
        except portalocker.exceptions.LockException:
            return

    def _run_locked(self, job_id: str) -> None:
        job = self.status(job_id)
        if job["status"] in {"ready_for_preview", "committed"}:
            return
        directory = self.path(job_id)
        progress = job["progress"]
        try:
            self._update(
                job_id, status="downloading", error_code=None, error_message=None
            )
            source = directory / job["filename"]
            paths = []

            def cancelled() -> bool:
                return self.stopping.is_set() or bool(
                    self.status(job_id)["cancel_requested"]
                )

            if job["filename"] == "local-bundle.json":
                paths = self._local_bundle_parts(directory)
            elif source.suffix == ".json":
                entries = parse_manifest(source.read_bytes())
                failures: list[ImportWorkflowError] = []
                for entry in entries:
                    if cancelled():
                        raise ImportWorkflowError(
                            "CLAUDE_CANCELLED",
                            "Import cancelled; complete downloads retained.",
                        )
                    target = directory / entry.filename

                    def report(size: int, name: str = entry.filename) -> None:
                        complete_bytes = sum(
                            p.stat().st_size
                            for p in directory.glob("*.zip")
                            if p.name != name
                        )
                        if complete_bytes + size > self.max_total_bytes:
                            raise ImportWorkflowError(
                                "IMPORT_SECURITY_REJECTED",
                                "Export exceeds the total download quota.",
                            )
                        progress[name] = {"status": "downloading", "bytes": size}
                        self._update(job_id, progress_json=json.dumps(progress))

                    try:
                        digest = self.downloader.download(
                            entry.export_url,
                            target,
                            cancelled=cancelled,
                            progress=report,
                        )
                    except ImportWorkflowError as exc:
                        if exc.code not in {
                            "CLAUDE_LINK_EXPIRED",
                            "CLAUDE_DOWNLOAD_FAILED",
                            "CLAUDE_DOWNLOAD_INTERRUPTED",
                            "CLAUDE_INVALID_ZIP",
                            "CLAUDE_EXPORT_NOT_AVAILABLE",
                            "CLAUDE_REGION_UNAVAILABLE",
                        }:
                            raise
                        progress[entry.filename] = {
                            "status": "failed",
                            "error_code": exc.code,
                        }
                        self._update(job_id, progress_json=json.dumps(progress))
                        failures.append(exc)
                        continue
                    progress[entry.filename] = {
                        "status": "downloaded",
                        "bytes": target.stat().st_size,
                        "sha256": digest,
                    }
                    self._update(job_id, progress_json=json.dumps(progress))
                    paths.append(target)
                if failures:
                    raise failures[0]
            else:
                paths.append(source)
            if cancelled():
                raise ImportWorkflowError(
                    "CLAUDE_CANCELLED", "Import cancelled; complete downloads retained."
                )
            self._update(job_id, status="decoding")
            package = convert_claude_export(
                paths,
                job["project_id"],
                max_unpacked_bytes=self.max_unpacked_bytes,
                max_files=self.max_files,
            )
            if cancelled():
                raise ImportWorkflowError(
                    "CLAUDE_CANCELLED", "Import cancelled; complete files retained."
                )
            self._update(job_id, status="validating")
            report = validate_package(package)
            if not report.valid:
                raise ImportWorkflowError(
                    "UMIF_VALIDATION_FAILED", ", ".join(report.errors)
                )
            package_path = directory / "umif"
            if package_path.exists():
                import shutil

                shutil.rmtree(package_path)
            write_package(package, package_path)
            if cancelled():
                raise ImportWorkflowError(
                    "CLAUDE_CANCELLED", "Import cancelled; complete files retained."
                )
            progress["report"] = package.import_report
            self._update(
                job_id,
                status="ready_for_preview",
                progress_json=json.dumps(progress),
                preview_id=None,
            )
        except ImportWorkflowError as exc:
            if self.stopping.is_set() and not self.status(job_id)["cancel_requested"]:
                self._update(
                    job_id,
                    status="queued",
                    error_code="IMPORT_JOB_INTERRUPTED",
                    error_message="Import interrupted by service "
                    "shutdown; it will resume on restart.",
                )
                return
            self._update(
                job_id,
                status="cancelled" if exc.code == "CLAUDE_CANCELLED" else "failed",
                error_code=exc.code,
                error_message=str(exc),
            )
        except Exception:
            self._update(
                job_id,
                status="failed",
                error_code="CLAUDE_PROCESSING_FAILED",
                error_message="Export processing failed; raw files were retained.",
            )

    def retry(self, job_id: str) -> None:
        job = self.status(job_id)
        if job["status"] in {"ready_for_preview", "committed"}:
            raise ImportWorkflowError(
                "IMPORT_JOB_COMPLETE", "This job is already complete.", status_code=409
            )
        if job["status"] in {"downloading", "decoding", "validating"}:
            raise ImportWorkflowError(
                "IMPORT_JOB_RUNNING", "Import is already running.", status_code=409
            )
        self._update(
            job_id,
            status="queued",
            cancel_requested=0,
            error_code=None,
            error_message=None,
        )

    def recover(self) -> list[str]:
        """Recover unfinished jobs; run() locks each job across processes."""
        with self.store.connect() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM import_jobs "
                    "WHERE status IN ('queued','downloading','decoding','validating')"
                )
            ]

    def preview(
        self, job_id: str, *, confirm_project_merge: bool = False
    ) -> dict[str, Any]:
        job = self.status(job_id)
        if job["status"] != "ready_for_preview":
            raise ImportWorkflowError(
                "IMPORT_JOB_NOT_READY", "Import job is not ready.", status_code=409
            )
        directory = self.path(job_id)
        try:
            package = read_package(directory / "umif")
        except (OSError, ValueError) as exc:
            raise ImportWorkflowError(
                "UMIF_VALIDATION_FAILED", "Staged package is unreadable."
            ) from exc
        validation = validate_package(package, directory / "umif")
        if not validation.valid:
            raise ImportWorkflowError(
                "UMIF_VALIDATION_FAILED", ", ".join(validation.errors)
            )
        projects = package.import_report.get("source_projects", [])
        if len(projects) > 1 and not confirm_project_merge:
            raise ImportWorkflowError(
                "CLAUDE_PROJECT_MAPPING_REQUIRED",
                "Confirm mapping all source projects to the "
                "selected target, or import separate exports.",
                status_code=409,
            )
        descriptor = canonical_bytes(
            {"provider": "claude", "package_id": package.manifest.package_id}
        )
        sources = tuple(
            sorted(
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() == ".zip"
            )
        )
        if (directory / "manifest.json").is_file():
            sources += (directory / "manifest.json",)
        preview = self.imports.preview(
            "claude-export.json",
            "claude",
            descriptor,
            project_id=job["project_id"],
            _package=package,
            _source_files=sources,
            _job_id=job_id,
        )
        self._update(job_id, preview_id=preview["preview_id"])
        return preview
