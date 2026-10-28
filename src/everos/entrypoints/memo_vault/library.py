"""Project-scoped reads for Vault-confirmed canonical EverOS memories."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .adapter import EverOSAdapter, EverOSAdapterError, EverOSMemoryRecord
from .evidence import (
    archived_source_status,
    load_source_segment,
    select_evidence_excerpt,
)
from .storage import VaultStore

MemoryType = Literal["episode", "atomic_fact", "profile"]
LibraryStatus = Literal["valid", "expired", "all"]
PackKind = Literal["preference", "fact", "decision", "todo", "episode", "profile"]
ExternalUse = Literal["allow", "ask", "deny"]


@dataclass(frozen=True, slots=True)
class PackEvidenceReference:
    evidence_id: str
    source_message_id: str
    quote: str
    quote_sha256: str
    available: bool
    matched: bool


@dataclass(frozen=True, slots=True)
class PackSourceMemory:
    """Canonical EverOS content joined to one confirmed Vault registry row."""

    memory_id: str
    everos_memory_id: str
    project_id: str
    namespace: str
    kind: PackKind
    memory_type: MemoryType
    revision: int
    content: str | None
    timestamp: str | None
    recorded_at: str
    confirmed_at: str
    external_use: ExternalUse
    valid_until: str | None
    expiry_status: Literal["valid", "expired"]
    lifecycle_status: Literal["active", "withdrawn", "deleted"]
    evidence_kind: str
    content_sha256: str | None
    evidence: tuple[PackEvidenceReference, ...]
    index_available: bool


class LibraryError(RuntimeError):
    """Base error translated into the local Vault HTTP contract."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ProjectNotFound(LibraryError):
    def __init__(self, project_id: str) -> None:
        super().__init__("PROJECT_NOT_FOUND", f"Project '{project_id}' does not exist.", 404)


class LibraryMemoryNotFound(LibraryError):
    def __init__(self) -> None:
        super().__init__("MEMORY_NOT_FOUND", "Confirmed memory does not exist in this project.", 404)


class LibraryMemorySyncing(LibraryError):
    def __init__(self) -> None:
        super().__init__("MEMORY_INDEX_SYNCING", "The approved memory is not visible in the EverOS index yet.", 409)


class MemoVaultLibraryService:
    """Join confirmed Vault governance rows with canonical scoped EverOS reads."""

    def __init__(self, store: VaultStore, adapter: EverOSAdapter) -> None:
        self.store = store
        self.adapter = adapter

    async def list_memories(
        self,
        *,
        project_id: str,
        query: str = "",
        status: LibraryStatus = "valid",
    ) -> dict[str, object]:
        self._require_project(project_id)
        registry = self._confirmed_rows(project_id)
        canonical = await self._canonical_records(
            project_id, [row for row in registry if row["lifecycle_status"] == "active"]
        )
        now = datetime.now(UTC)
        query_key = query.strip().casefold()
        visible: list[dict[str, object]] = []
        missing: list[str] = []
        for row in registry:
            if row["lifecycle_status"] == "deleted":
                continue
            expiry_status = _expiry_status(row["valid_until"], now)
            lifecycle = _lifecycle_status(row["lifecycle_status"])
            if lifecycle == "active" and row["everos_memory_id"] not in canonical:
                if status == "all" or expiry_status == status:
                    missing.append(str(row["id"]))
                continue
            if status != "all" and expiry_status != status:
                continue
            record = (
                canonical[str(row["everos_memory_id"])]
                if lifecycle == "active"
                else _snapshot_record(row)
            )
            if query_key and query_key not in record.content.casefold():
                continue
            visible.append(self._list_item(row, record, project_id, expiry_status))
        visible.sort(key=lambda item: (str(item.get("timestamp") or ""), str(item["id"])), reverse=True)
        return {
            "memories": visible,
            "visible_total": len(visible),
            "missing_index_ids": missing,
        }

    async def list_pack_sources(self, project_id: str) -> list[PackSourceMemory]:
        """Return confirmed registry rows joined to canonical, user-scoped bodies.

        Candidate text is intentionally absent from this projection. Inactive
        lifecycle rows and confirmed rows missing from the EverOS index have
        no body, so downstream export code cannot accidentally fall back.
        """
        self._require_project(project_id)
        registry = [
            row for row in self._confirmed_rows(project_id)
            if row["lifecycle_status"] != "deleted"
        ]
        active = [
            row for row in registry
            if row["lifecycle_status"] == "active" and row["external_use"] != "deny"
        ]
        canonical = await self._canonical_records(project_id, active)
        now = datetime.now(UTC)
        sources: list[PackSourceMemory] = []
        for row in registry:
            lifecycle = _lifecycle_status(row["lifecycle_status"])
            record = (
                canonical.get(str(row["everos_memory_id"]))
                if lifecycle == "active" and row["external_use"] != "deny"
                else None
            )
            body = record.content if record is not None else None
            evidence = tuple(
                PackEvidenceReference(
                    evidence_id=str(item["evidence_id"]),
                    source_message_id=str(item["source_message_id"]),
                    quote=str(item["quote"]),
                    quote_sha256=str(item["quote_sha256"]),
                    available=bool(item["available"]),
                    matched=bool(item["matched"]),
                )
                for item in self._evidence(str(row["id"]), body)
            ) if body is not None else ()
            sources.append(PackSourceMemory(
                memory_id=str(row["id"]),
                everos_memory_id=str(row["everos_memory_id"]),
                project_id=project_id,
                namespace=str(row["namespace"]),
                kind=_pack_kind(row["kind"]),
                memory_type=_memory_type(row["memory_type"]),
                revision=int(row["revision"]),
                content=body,
                timestamp=record.timestamp if record is not None else None,
                recorded_at=str(row["recorded_at"] or row["confirmed_at"]),
                confirmed_at=str(row["confirmed_at"]),
                external_use=_external_use(row["external_use"]),
                valid_until=row["valid_until"],
                expiry_status=_expiry_status(row["valid_until"], now),
                lifecycle_status=lifecycle,
                evidence_kind=str(row["evidence_kind"]),
                content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest() if body is not None else None,
                evidence=evidence,
                index_available=record is not None,
            ))
        return sources

    def revalidate_pack_sources(
        self,
        project_ids: list[str],
        sources: list[PackSourceMemory],
    ) -> None:
        """Synchronously verify the Vault policy snapshot after upstream reads.

        Pack source loading awaits EverOS. A different selected project may
        take long enough for earlier rows to be withdrawn, denied, revised,
        expired, or newly confirmed. This final local read runs after those
        awaits, under one SQLite read snapshot, immediately before rendering.
        """
        namespaces = list(dict.fromkeys(project_ids))
        if not namespaces:
            raise LibraryError("PACK_SOURCE_CHANGED", "The selected project scope changed; refresh the Pack.", 409)

        placeholders = ",".join("?" for _ in namespaces)
        query = f"""SELECT m.id, m.everos_memory_id, m.namespace, m.kind, m.memory_type,
                           m.revision, m.evidence_kind, m.external_use, m.valid_until,
                           m.confirmed_at, m.lifecycle_status, c.created_at AS recorded_at
                    FROM memories m LEFT JOIN candidates c ON c.id = m.id
                    WHERE m.status = 'confirmed' AND m.lifecycle_status != 'deleted'
                      AND m.namespace IN ({placeholders})"""
        with self.store.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(query, namespaces).fetchall()

        current_by_id = {str(row["id"]): row for row in rows}
        loaded_by_id = {source.memory_id: source for source in sources}
        if len(current_by_id) != len(rows) or len(loaded_by_id) != len(sources):
            raise LibraryError("PACK_SOURCE_CHANGED", "The selected memory set changed; refresh the Pack.", 409)
        if current_by_id.keys() != loaded_by_id.keys():
            raise LibraryError("PACK_SOURCE_CHANGED", "The selected memory set changed; refresh the Pack.", 409)

        now = datetime.now(UTC)
        for memory_id, row in current_by_id.items():
            source = loaded_by_id[memory_id]
            current_state = (
                str(row["everos_memory_id"]),
                str(row["namespace"]),
                str(row["kind"]),
                str(row["memory_type"]),
                int(row["revision"]),
                str(row["evidence_kind"]),
                str(row["external_use"]),
                row["valid_until"],
                str(row["confirmed_at"]),
                str(row["lifecycle_status"]),
                str(row["recorded_at"] or row["confirmed_at"]),
            )
            loaded_state = (
                source.everos_memory_id,
                source.namespace,
                source.kind,
                source.memory_type,
                source.revision,
                source.evidence_kind,
                source.external_use,
                source.valid_until,
                source.confirmed_at,
                source.lifecycle_status,
                source.recorded_at,
            )
            if current_state != loaded_state:
                raise LibraryError("PACK_SOURCE_CHANGED", "A memory changed while loading the Pack; refresh it.", 409)
            if _expiry_status(row["valid_until"], now) != source.expiry_status:
                raise LibraryError("PACK_SOURCE_CHANGED", "A memory expired while loading the Pack; refresh it.", 409)

    async def summary(self) -> dict[str, object]:
        projects: list[dict[str, object]] = []
        for project in self.store.list_projects():
            project_id = project["project_id"]
            registry = self._confirmed_rows(project_id)
            canonical = await self._canonical_records(
                project_id, [row for row in registry if row["lifecycle_status"] == "active"]
            )
            now = datetime.now(UTC)
            visible_valid = 0
            syncing = 0
            for row in registry:
                if row["lifecycle_status"] != "active":
                    continue
                if _expiry_status(row["valid_until"], now) != "valid":
                    continue
                if row["everos_memory_id"] in canonical:
                    visible_valid += 1
                else:
                    syncing += 1
            projects.append({
                "project_id": project_id,
                "name": project["name"],
                "visible_valid_count": visible_valid,
                "syncing_count": syncing,
            })
        return {"projects": projects}

    async def get_memory(self, *, project_id: str, memory_id: str) -> dict[str, object]:
        self._require_project(project_id)
        row = self._confirmed_row(project_id, memory_id)
        if row is None:
            raise LibraryMemoryNotFound()
        lifecycle = str(row["lifecycle_status"])
        if lifecycle != "active":
            record = _snapshot_record(row)
            result = self._list_item(
                row, record, project_id,
                _expiry_status(row["valid_until"], datetime.now(UTC)),
            )
            result["content"] = "" if lifecycle == "deleted" else record.content
            result["metadata"] = {} if lifecycle == "deleted" else record.metadata
            result["evidence"] = [] if lifecycle == "deleted" else self._evidence(str(row["id"]), record.content)
            result["evidence_available"] = any(item["available"] for item in result["evidence"])
            result["source_segment"] = (
                None if lifecycle == "deleted"
                else self._source_segment(str(row["id"]), record.content)
            )
            result["lifecycle_note"] = (
                "此记忆已删除，正文与来源链接不可查看。"
                if lifecycle == "deleted"
                else "此记忆已撤销，保留只读记录。"
            )
            return result
        memory_type = _memory_type(row["memory_type"])
        identity = self.store.get_identity()
        try:
            record = await self.adapter.get_user_memory(
                app_id=identity["app_id"],
                project_id=project_id,
                user_id=identity["user_id"],
                memory_id=str(row["everos_memory_id"]),
                memory_type=memory_type,
            )
        except EverOSAdapterError as exc:
            raise _translate_adapter_error(exc) from exc
        if record is None:
            raise LibraryMemorySyncing()
        result = self._list_item(row, record, project_id, _expiry_status(row["valid_until"], datetime.now(UTC)))
        result["content"] = record.content
        result["metadata"] = record.metadata
        result["evidence"] = self._evidence(str(row["id"]), record.content)
        result["evidence_available"] = any(item["available"] for item in result["evidence"])
        result["source_segment"] = self._source_segment(str(row["id"]), record.content)
        return result

    async def apply_lifecycle(
        self,
        *,
        project_id: str,
        memory_id: str,
        action: Literal["correct", "withdraw", "delete"],
        expected_revision: int,
        content: str | None = None,
        external_use: Literal["allow", "ask", "deny"] | None = None,
        note: str | None = None,
    ) -> dict[str, object]:
        self._require_project(project_id)
        row = self._confirmed_row(project_id, memory_id)
        if row is None:
            raise LibraryMemoryNotFound()
        target_status = {"correct": "active", "withdraw": "withdrawn", "delete": "deleted"}[action]
        if row["lifecycle_status"] == target_status:
            if action != "correct" or (content == row["candidate_content"] and external_use == row["external_use"]):
                return await self.get_memory(project_id=project_id, memory_id=memory_id)
        if row["lifecycle_status"] != "active":
            raise LibraryError("MEMORY_LIFECYCLE_CONFLICT", "This memory is no longer active.", 409)
        if expected_revision != row["revision"]:
            raise LibraryError("MEMORY_REVISION_CONFLICT", "This memory changed since it was loaded. Refresh before editing.", 409)
        if action == "correct" and (not content or not content.strip()):
            raise LibraryError("EMPTY_MEMORY_CONTENT", "Memory content cannot be empty.", 422)

        corrected_content = content.strip() if action == "correct" and content else None
        candidate_content = str(row["candidate_content"] or "").strip()
        if (
            action == "correct"
            and corrected_content == candidate_content
            and external_use is not None
            and external_use != row["external_use"]
        ):
            # Export policy belongs to the local Vault registry. Do not route a
            # policy-only edit through EverOS' canonical-memory lifecycle API.
            with self.store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """SELECT lifecycle_status, revision, external_use
                       FROM memories
                       WHERE id = ? AND namespace = ? AND status = 'confirmed'""",
                    (memory_id, project_id),
                ).fetchone()
                if current is None:
                    raise LibraryMemoryNotFound()
                if current["lifecycle_status"] != "active":
                    raise LibraryError(
                        "MEMORY_LIFECYCLE_CONFLICT",
                        "This memory is no longer active.",
                        409,
                    )
                if (
                    current["revision"] != expected_revision
                    or current["external_use"] != row["external_use"]
                ):
                    raise LibraryError(
                        "MEMORY_REVISION_CONFLICT",
                        "This memory changed since it was loaded. "
                        "Refresh before editing.",
                        409,
                    )
                connection.execute(
                    """UPDATE memories SET external_use = ?
                       WHERE id = ? AND namespace = ?""",
                    (external_use, memory_id, project_id),
                )
                connection.execute(
                    """UPDATE candidates
                       SET external_use = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (external_use, memory_id),
                )
            return await self.get_memory(project_id=project_id, memory_id=memory_id)

        identity = self.store.get_identity()
        try:
            receipt = await self.adapter.apply_memory_lifecycle(
                candidate_id=str(row["id"]),
                memory_id=str(row["everos_memory_id"]),
                app_id=identity["app_id"],
                project_id=project_id,
                user_id=identity["user_id"],
                memory_type=_memory_type(row["memory_type"]),
                action=action,
                expected_revision=expected_revision,
                content=content.strip() if action == "correct" and content else None,
            )
        except EverOSAdapterError as exc:
            raise _translate_adapter_error(exc) from exc
        if receipt.get("lifecycle_status") != target_status:
            raise LibraryError("EVEROS_INVALID_LIFECYCLE_RESPONSE", "EverOS returned an unexpected lifecycle state.", 502)
        new_revision = receipt.get("revision")
        if (
            not isinstance(new_revision, int)
            or isinstance(new_revision, bool)
            or new_revision <= expected_revision
        ):
            raise LibraryError(
                "EVEROS_INVALID_LIFECYCLE_RESPONSE",
                "EverOS did not advance the memory revision.",
                502,
            )

        now = datetime.now(UTC).isoformat()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT lifecycle_status, revision FROM memories WHERE id = ? AND namespace = ?",
                (memory_id, project_id),
            ).fetchone()
            if current is None:
                raise LibraryMemoryNotFound()
            already_applied = current["revision"] == new_revision and current["lifecycle_status"] == target_status
            if current["revision"] != expected_revision and not already_applied:
                raise LibraryError("MEMORY_REVISION_CONFLICT", "This memory changed during the update. Refresh before editing.", 409)
            if not already_applied:
                connection.execute(
                    """UPDATE memories SET lifecycle_status = ?, revision = ?,
                       withdrawn_at = CASE WHEN ? = 'withdrawn' THEN ? ELSE withdrawn_at END,
                       deleted_at = CASE WHEN ? = 'deleted' THEN ? ELSE deleted_at END
                       WHERE id = ? AND namespace = ?""",
                    (target_status, new_revision, target_status, now, target_status, now, memory_id, project_id),
                )
                if action == "correct":
                    corrected = str(receipt.get("content") or content or "").strip()
                    connection.execute(
                        """UPDATE candidates SET content = ?, revision = ?, external_use = ?,
                           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                        (corrected, new_revision, external_use or row["external_use"], memory_id),
                    )
                    if row["memory_type"] == "profile":
                        metadata = json.loads(row["candidate_metadata"] or "{}")
                        if isinstance(metadata, dict) and isinstance(metadata.get("item"), dict):
                            metadata["item"] = {**metadata["item"], "description": corrected}
                            connection.execute(
                                "UPDATE candidates SET metadata_json = ? WHERE id = ?",
                                (json.dumps(metadata, ensure_ascii=False), memory_id),
                            )
                    connection.execute(
                        "UPDATE memories SET external_use = ? WHERE id = ?",
                        (external_use or row["external_use"], memory_id),
                    )
                elif action == "delete":
                    connection.execute(
                        """UPDATE candidates
                           SET content = '', metadata_json = NULL, source_segment_json = NULL
                           WHERE id = ?""",
                        (memory_id,),
                    )
                    connection.execute("DELETE FROM candidate_evidence WHERE candidate_id = ?", (memory_id,))
                    connection.execute("UPDATE memory_lifecycle_events SET note = NULL WHERE memory_id = ?", (memory_id,))
                connection.execute(
                    """INSERT OR IGNORE INTO memory_lifecycle_events
                       (id, memory_id, action, from_revision, to_revision, note)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (uuid.uuid4().hex, memory_id, action, expected_revision, new_revision, note if action == "correct" else None),
                )
        return await self.get_memory(project_id=project_id, memory_id=memory_id)

    def _require_project(self, project_id: str) -> None:
        if not self.store.has_project(project_id):
            raise ProjectNotFound(project_id)

    def _confirmed_rows(self, project_id: str) -> list[sqlite3.Row]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT m.id, m.everos_memory_id, m.namespace, m.kind, m.memory_type, m.revision,
                          m.evidence_kind, m.external_use, m.valid_until, m.confirmed_at,
                          m.lifecycle_status, m.withdrawn_at, m.deleted_at,
                          c.content AS candidate_content, c.metadata_json AS candidate_metadata,
                          c.created_at AS recorded_at
                   FROM memories m LEFT JOIN candidates c ON c.id = m.id
                   WHERE m.status = 'confirmed' AND m.namespace = ?
                   ORDER BY m.confirmed_at DESC, m.id""",
                (project_id,),
            ).fetchall()
        return rows

    def _confirmed_row(self, project_id: str, memory_id: str) -> sqlite3.Row | None:
        with self.store.connect() as connection:
            return connection.execute(
                """SELECT m.id, m.everos_memory_id, m.namespace, m.kind, m.memory_type, m.revision,
                          m.evidence_kind, m.external_use, m.valid_until, m.confirmed_at,
                          m.lifecycle_status, m.withdrawn_at, m.deleted_at,
                          c.content AS candidate_content, c.metadata_json AS candidate_metadata,
                          c.created_at AS recorded_at
                   FROM memories m LEFT JOIN candidates c ON c.id = m.id
                   WHERE m.id = ? AND m.namespace = ? AND m.status = 'confirmed'""",
                (memory_id, project_id),
            ).fetchone()

    async def _canonical_records(
        self, project_id: str, registry: list[sqlite3.Row]
    ) -> dict[str, EverOSMemoryRecord]:
        identity = self.store.get_identity()
        records: dict[str, EverOSMemoryRecord] = {}
        memory_types = {_memory_type(row["memory_type"]) for row in registry}
        for memory_type in sorted(memory_types):
            try:
                values = await self.adapter.list_user_memories(
                    app_id=identity["app_id"],
                    project_id=project_id,
                    user_id=identity["user_id"],
                    memory_type=memory_type,
                )
            except EverOSAdapterError as exc:
                raise _translate_adapter_error(exc) from exc
            for record in values:
                if record.memory_type != memory_type:
                    raise LibraryError(
                        "EVEROS_INVALID_MEMORY_RESPONSE",
                        "EverOS returned a memory with an unexpected type.",
                        502,
                    )
                if record.memory_id in records:
                    raise LibraryError(
                        "EVEROS_INVALID_MEMORY_RESPONSE",
                        "EverOS returned duplicate memory IDs across types.",
                        502,
                    )
                records[record.memory_id] = record
        return records

    @staticmethod
    def _list_item(
        row: sqlite3.Row,
        record: EverOSMemoryRecord,
        project_id: str,
        status: str,
    ) -> dict[str, object]:
        preview = (
            "此记忆已删除，正文不可查看。"
            if row["lifecycle_status"] == "deleted"
            else record.content.strip()
        )
        if len(preview) > 600:
            preview = preview[:600].rstrip() + "…"
        return {
            "id": row["id"],
            "everos_memory_id": row["everos_memory_id"],
            "project_id": project_id,
            "namespace": row["namespace"],
            "kind": row["kind"],
            "memory_type": row["memory_type"],
            "content_preview": preview,
            "timestamp": record.timestamp or row["confirmed_at"],
            "external_use": row["external_use"],
            "status": status,
            "lifecycle_status": row["lifecycle_status"],
            "revision": row["revision"],
            "evidence_kind": row["evidence_kind"],
            "valid_until": row["valid_until"],
        }

    def _evidence(self, candidate_id: str, memory_content: str) -> list[dict[str, object]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT e.message_id, e.quote, e.char_start, e.char_end, e.quote_sha256,
                          m.role, m.timestamp_ms, m.content,
                          b.original_filename, b.raw_relpath, b.file_sha256
                   FROM candidate_evidence e
                   JOIN candidates c ON c.id = e.candidate_id
                   JOIN normalized_messages m ON m.id = e.message_id AND m.batch_id = c.batch_id
                   JOIN import_batches b ON b.id = c.batch_id
                   WHERE e.candidate_id = ?
                   ORDER BY m.timestamp_ms, e.message_id""",
                (candidate_id,),
            ).fetchall()
        evidence: list[dict[str, object]] = []
        archive_status_by_file: dict[tuple[str, str], str] = {}
        for row in rows:
            archive_key = (str(row["raw_relpath"] or ""), str(row["file_sha256"] or ""))
            if archive_key not in archive_status_by_file:
                archive_status_by_file[archive_key] = archived_source_status(
                    self.store.data_dir,
                    self.store.evidence_dir,
                    row["raw_relpath"],
                    row["file_sha256"],
                )
            archive_status = archive_status_by_file[archive_key]
            archive_available = archive_status == "available"
            quote = str(row["quote"] or "")
            quote_sha256 = str(row["quote_sha256"] or "")
            source_content = str(row["content"] or "")
            # Re-select against the current canonical memory so a corrected
            # claim does not keep pointing at an obsolete excerpt. This also
            # compacts the legacy full-message quotes without exposing them.
            excerpt = select_evidence_excerpt(memory_content, source_content)
            if excerpt.matched:
                quote = excerpt.quote
            else:
                quote = ""
            quote_matches = excerpt.matched and bool(quote)
            evidence.append({
                "filename": row["original_filename"],
                "message_id": row["message_id"],
                "source_message_id": row["message_id"],
                "evidence_id": _evidence_id(
                    candidate_id,
                    str(row["message_id"]),
                    row["char_start"],
                    row["char_end"],
                    quote_sha256,
                ),
                "quote_sha256": quote_sha256,
                "role": row["role"],
                "timestamp_ms": row["timestamp_ms"],
                "quote": quote,
                "available": archive_available,
                "archive_status": archive_status,
                "excerpt_matched": quote_matches,
                "matched": quote_matches,
            })
        return evidence

    def _source_segment(self, candidate_id: str, memory_content: str) -> dict[str, object] | None:
        with self.store.connect() as connection:
            return load_source_segment(
                connection,
                candidate_id,
                memory_content,
                data_dir=self.store.data_dir,
                evidence_dir=self.store.evidence_dir,
            )


def _snapshot_record(row: sqlite3.Row) -> EverOSMemoryRecord:
    """Shape the Vault-approved snapshot for withdrawn/deleted entries."""
    content = str(row["candidate_content"] or "")
    try:
        metadata = json.loads(row["candidate_metadata"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return EverOSMemoryRecord(
        memory_id=str(row["everos_memory_id"]),
        memory_type=_memory_type(row["memory_type"]),
        content=content,
        timestamp=str(row["confirmed_at"]),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _memory_type(value: object) -> MemoryType:
    if value not in {"episode", "atomic_fact", "profile"}:
        raise LibraryError(
            "INVALID_MEMORY_TYPE",
            "Confirmed memory has an unsupported EverOS memory type.",
            502,
        )
    return value  # type: ignore[return-value]


def _pack_kind(value: object) -> PackKind:
    if value not in {"preference", "fact", "decision", "todo", "episode", "profile"}:
        raise LibraryError("INVALID_MEMORY_KIND", "Confirmed memory has an unsupported kind.", 502)
    return value  # type: ignore[return-value]


def _external_use(value: object) -> ExternalUse:
    if value not in {"allow", "ask", "deny"}:
        raise LibraryError("INVALID_EXTERNAL_USE", "Confirmed memory has an unsupported export policy.", 502)
    return value  # type: ignore[return-value]


def _lifecycle_status(value: object) -> Literal["active", "withdrawn", "deleted"]:
    if value not in {"active", "withdrawn", "deleted"}:
        raise LibraryError("INVALID_MEMORY_LIFECYCLE", "Confirmed memory has an unsupported lifecycle state.", 502)
    return value  # type: ignore[return-value]


def _evidence_id(
    candidate_id: str,
    message_id: str,
    char_start: object,
    char_end: object,
    quote_sha256: str,
) -> str:
    parts = (
        candidate_id,
        message_id,
        "" if char_start is None else str(char_start),
        "" if char_end is None else str(char_end),
        quote_sha256,
    )
    return "ev_" + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]


def _expiry_status(value: object, now: datetime) -> Literal["valid", "expired"]:
    if not isinstance(value, str) or not value.strip():
        return "valid"
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LibraryError("INVALID_EXPIRY", "A confirmed memory has an invalid expiry value.", 502) from exc
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return "expired" if now >= expires.astimezone(UTC) else "valid"


def _translate_adapter_error(exc: EverOSAdapterError) -> LibraryError:
    # Unexpected upstream 4xx responses indicate a broken API contract or
    # rejected scope shape; transport/server failures remain retryable 503s.
    status = 502 if exc.status_code == 502 or 400 <= exc.status_code < 500 else 503
    return LibraryError(exc.code, str(exc), status)
