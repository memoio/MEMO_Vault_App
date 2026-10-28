"""Candidate review, confirmation, and rejection endpoints."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from ..evidence import archived_source_status, evidence_status, load_source_segment

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..adapter import EverOSAdapterError, EverOSHttpAdapter
from ..config import get_settings
from ..contracts import (
    ApiEnvelope,
    ConfirmCandidateRequest,
    ManualCandidateRequest,
    RejectCandidateRequest,
)

router = APIRouter(prefix="/candidates", tags=["memo-vault-candidates"])


class CandidateSourceRequest(BaseModel):
    content: str | None = Field(default=None, max_length=4000)


@router.post("", status_code=201, response_model=ApiEnvelope[dict[str, object]])
async def create_manual_candidate(
    body: ManualCandidateRequest, request: Request
) -> ApiEnvelope[dict[str, object]]:
    """Add a human-authored candidate to the normal review queue."""
    store = request.app.state.vault_store
    content = body.content.strip()
    namespace = body.namespace.strip()
    if not content:
        raise HTTPException(status_code=422, detail={
            "code": "EMPTY_CANDIDATE_CONTENT",
            "message": "Memory content cannot be empty.",
        })
    if not store.has_project(namespace):
        raise HTTPException(status_code=422, detail={
            "code": "PROJECT_NOT_FOUND",
            "message": "Choose an existing project space.",
        })

    identity = store.get_identity()
    candidate_id = uuid.uuid4().hex
    batch_id = f"manual_{candidate_id}"
    created_at = datetime.now(timezone.utc)
    memory_type = "episode" if body.kind == "episode" else "profile"
    metadata = (
        {
            "title": "人工新增",
            "timestamp_ms": int(created_at.timestamp() * 1000),
        }
        if memory_type == "episode"
        else {
            "section": "explicit_info",
            "item": {"description": content, "evidence": "人工新增"},
            "timestamp_ms": int(created_at.timestamp() * 1000),
        }
    )
    scope = f"{identity['app_id']}/{namespace}/users/{identity['user_id']}"

    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        # candidates.batch_id is required by the existing schema and supplies
        # EverOS identity during confirmation. This internal anchor is not an
        # imported file and intentionally has no archived path or messages.
        connection.execute(
            """INSERT INTO import_batches
               (id, source_name, file_sha256, namespace, status, extraction_status,
                source_type, original_filename, app_id, project_id, user_id,
                agent_id, raw_relpath)
               VALUES (?, 'manual', '', ?, 'manual', 'not_applicable',
                       'manual', '人工新增', ?, ?, ?, ?, '')""",
            (
                batch_id,
                namespace,
                identity["app_id"],
                namespace,
                identity["user_id"],
                identity["agent_id"],
            ),
        )
        connection.execute(
            """INSERT INTO candidates
               (id, batch_id, everos_scope, kind, memory_type, metadata_json,
                content, status, evidence_kind, external_use, origin,
                target_project_id, validity_days, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'human_authored', ?,
                       'manual', ?, ?, ?, ?)""",
            (
                candidate_id,
                batch_id,
                scope,
                body.kind,
                memory_type,
                json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
                content,
                body.external_use,
                namespace,
                body.validity_days,
                created_at.isoformat(),
                created_at.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO import_events (id, batch_id, event_type) VALUES (?, ?, 'manual_candidate_created')",
            (uuid.uuid4().hex, batch_id),
        )

    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data={
            "id": candidate_id,
            "batch_id": batch_id,
            "filename": "人工新增",
            "project_id": namespace,
            "namespace": namespace,
            "kind": body.kind,
            "memory_type": memory_type,
            "content": content,
            "status": "pending",
            "revision": 1,
            "evidence_kind": "human_authored",
            "external_use": body.external_use,
            "origin": "manual",
            "evidence_status": "not_required",
            "validity_days": body.validity_days,
            "valid_until": None,
            "created_at": created_at.isoformat(),
            "evidence": [],
        },
    )


@router.get("", response_model=ApiEnvelope[dict[str, object]])
async def list_candidates(
    request: Request,
    status: str = Query(default="all", pattern="^(all|pending|confirmed|rejected)$"),
    kind: str | None = Query(default=None, pattern="^(preference|fact|decision|todo|episode|profile)$"),
    origin: str | None = Query(default=None, pattern="^(import|agent|manual)$"),
    evidence: str | None = Query(default=None, pattern="^(valid|invalid|not_required)$"),
) -> ApiEnvelope[dict[str, object]]:
    store = request.app.state.vault_store
    clauses: list[str] = []
    params: list[object] = []
    if status != "all":
        clauses.append("c.status = ?")
        params.append(status)
    if kind:
        clauses.append("c.kind = ?")
        params.append(kind)
    if origin:
        clauses.append("c.origin = ?")
        params.append(origin)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with store.connect() as connection:
        rows = connection.execute(
            f"""SELECT c.id, c.batch_id, c.kind, c.memory_type, c.content, c.status, c.revision,
                       c.evidence_kind, c.external_use, c.created_at, c.metadata_json,
                       c.origin, c.target_project_id, c.validity_days, c.valid_until,
                       b.original_filename AS filename, b.project_id, b.raw_relpath,
                       b.file_sha256,
                       e.message_id, e.quote, m.role, m.timestamp_ms,
                       m.content AS source_content, m.batch_id AS source_batch_id
                FROM candidates c
                JOIN import_batches b ON b.id = c.batch_id
                LEFT JOIN candidate_evidence e ON e.candidate_id = c.id
                LEFT JOIN normalized_messages m ON m.id = e.message_id
                {where}
                ORDER BY CASE c.status WHEN 'pending' THEN 0 ELSE 1 END,
                         c.created_at DESC, c.id, e.message_id""",
            params,
        ).fetchall()
    candidates: dict[str, dict[str, object]] = {}
    archive_status_by_batch: dict[str, str] = {}
    for row in rows:
        item = candidates.setdefault(
            row["id"],
            {
                "id": row["id"],
                "batch_id": row["batch_id"],
                "filename": row["filename"],
                "project_id": row["project_id"],
                "namespace": row["target_project_id"] or row["project_id"],
                "kind": row["kind"],
                "memory_type": row["memory_type"],
                "content": row["content"],
                "status": row["status"],
                "revision": row["revision"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "evidence_kind": row["evidence_kind"],
                "external_use": row["external_use"],
                "origin": row["origin"],
                "validity_days": row["validity_days"],
                "valid_until": row["valid_until"],
                "created_at": row["created_at"],
                "evidence": [],
                "evidence_status": "missing",
                "_source_texts": [],
                "_archive_status": "missing",
                "_has_linked_source": False,
            },
        )
        batch_id = str(row["batch_id"])
        if item["origin"] == "manual":
            archive_status_by_batch[batch_id] = "not_required"
        elif batch_id not in archive_status_by_batch:
            archive_status_by_batch[batch_id] = archived_source_status(
                store.data_dir,
                store.evidence_dir,
                row["raw_relpath"],
                row["file_sha256"],
            )
        archive_status = archive_status_by_batch[batch_id]
        item["_archive_status"] = archive_status
        if row["message_id"] and row["source_batch_id"] == row["batch_id"]:
            item["_has_linked_source"] = True
            if row["quote"] and row["quote"] in (row["source_content"] or ""):
                item["_source_texts"].append(row["source_content"])
            item["evidence"].append(
                {
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "timestamp_ms": row["timestamp_ms"],
                }
            )
    values = list(candidates.values())
    for item in values:
        archive_status = str(item.pop("_archive_status"))
        has_linked_source = bool(item.pop("_has_linked_source"))
        source_texts = item.pop("_source_texts")
        if item["origin"] == "manual":
            item["evidence_status"] = "not_required"
        elif item["origin"] == "import":
            item["evidence_status"] = (
                "valid"
                if archive_status == "available" and has_linked_source
                else "mismatch"
                if archive_status == "hash_mismatch"
                else "missing"
            )
        else:
            item["evidence_status"] = evidence_status(str(item["content"]), source_texts)
    if evidence == "valid":
        values = [item for item in values if item["evidence_status"] == "valid"]
    elif evidence == "invalid":
        values = [item for item in values if item["evidence_status"] in {"missing", "mismatch"}]
    elif evidence == "not_required":
        values = [item for item in values if item["evidence_status"] == "not_required"]
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data={"candidates": values, "total": len(values)},
    )


@router.get("/{candidate_id}/source/{message_id}", response_model=ApiEnvelope[dict[str, object]])
async def get_candidate_source(
    candidate_id: str, message_id: str, request: Request
) -> ApiEnvelope[dict[str, object]]:
    """Read the archived conversation containing a linked evidence message."""
    return await _read_candidate_source(candidate_id, message_id, request)


@router.post("/{candidate_id}/source/{message_id}", response_model=ApiEnvelope[dict[str, object]])
async def read_candidate_source(
    candidate_id: str,
    message_id: str,
    body: CandidateSourceRequest,
    request: Request,
) -> ApiEnvelope[dict[str, object]]:
    """Read evidence using the current candidate draft for key-position search."""
    return await _read_candidate_source(
        candidate_id, message_id, request, content_override=body.content
    )


async def _read_candidate_source(
    candidate_id: str,
    message_id: str,
    request: Request,
    *,
    content_override: str | None = None,
) -> ApiEnvelope[dict[str, object]]:
    store = request.app.state.vault_store
    with store.connect() as connection:
        source = connection.execute(
            """SELECT m.batch_id, m.conversation_id, e.message_id,
                      c.content AS candidate_content, b.original_filename,
                      b.raw_relpath, b.file_sha256
               FROM candidate_evidence e
               JOIN candidates c ON c.id = e.candidate_id
               JOIN normalized_messages m ON m.id = e.message_id AND m.batch_id = c.batch_id
               JOIN import_batches b ON b.id = c.batch_id
               WHERE e.candidate_id = ? AND e.message_id = ?""",
            (candidate_id, message_id),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail={
                "code": "CANDIDATE_SOURCE_NOT_FOUND",
                "message": "This source message is not linked to the candidate.",
            })
        archive_status = archived_source_status(
            store.data_dir,
            store.evidence_dir,
            source["raw_relpath"],
            source["file_sha256"],
        )
        if archive_status != "available":
            code, message = {
                "missing": (
                    "ARCHIVED_SOURCE_UNAVAILABLE",
                    "The original import file is not available in the secure archive.",
                ),
                "hash_mismatch": (
                    "ARCHIVED_SOURCE_HASH_MISMATCH",
                    "The archived file no longer matches the file recorded at import time.",
                ),
                "hash_unavailable": (
                    "ARCHIVED_SOURCE_HASH_UNAVAILABLE",
                    "The original file cannot be integrity-checked because its recorded hash is missing.",
                ),
            }.get(archive_status, ("ARCHIVED_SOURCE_UNAVAILABLE", "The original import file is not available."))
            raise HTTPException(status_code=409 if archive_status != "missing" else 404, detail={
                "code": code,
                "message": message,
            })
        source_segment = load_source_segment(
            connection,
            candidate_id,
            (content_override if content_override is not None else str(source["candidate_content"] or "")),
            data_dir=store.data_dir,
            evidence_dir=store.evidence_dir,
            selected_message_id=message_id,
        )
        messages = [dict(row) for row in connection.execute(
            """SELECT id, ordinal, role, content, timestamp_ms
               FROM normalized_messages
               WHERE batch_id = ? AND conversation_id = ?
               ORDER BY ordinal""",
            (source["batch_id"], source["conversation_id"]),
        ).fetchall()]
    return ApiEnvelope(request_id=uuid.uuid4().hex, data={
        "batch_id": source["batch_id"],
        "filename": source["original_filename"],
        "conversation_id": source["conversation_id"],
        "selected_message_id": message_id,
        "source_segment": source_segment,
        "messages": messages,
    })


@router.post("/{candidate_id}/confirm", response_model=ApiEnvelope[dict[str, object]])
async def confirm_candidate(
    candidate_id: str, body: ConfirmCandidateRequest, request: Request
) -> ApiEnvelope[dict[str, object]]:
    store = request.app.state.vault_store
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT c.*, b.app_id, b.project_id, b.user_id, b.raw_relpath,
                      b.file_sha256
               FROM candidates c JOIN import_batches b ON b.id = c.batch_id
               WHERE c.id = ?""",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "CANDIDATE_NOT_FOUND",
                    "message": "Candidate does not exist.",
                },
            )
        if row["status"] == "confirmed":
            return ApiEnvelope(
                request_id=uuid.uuid4().hex,
                data={
                    "id": candidate_id,
                    "status": "confirmed",
                    "everos_memory_id": row["everos_memory_id"],
                    "revision": row["revision"],
                },
            )
        if row["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CANDIDATE_NOT_REVIEWABLE",
                    "message": f"Candidate status is {row['status']}.",
                },
            )
        if body.revision != row["revision"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CANDIDATE_REVISION_CONFLICT",
                    "message": (
                        "Candidate changed since it was loaded. Refresh before reviewing."
                    ),
                },
            )
        if not store.has_project(body.namespace):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PROJECT_NOT_FOUND",
                    "message": "Choose an existing project space.",
                },
            )
        if (row["memory_type"] == "episode" and body.kind != "episode") or (
            row["memory_type"] == "profile" and body.kind != row["kind"]
        ):
            raise HTTPException(status_code=422, detail={
                "code": "CANDIDATE_TYPE_MISMATCH",
                "message": "Episode and Profile candidates must keep their EverOS memory type.",
            })
        if row["origin"] == "manual" and body.kind != row["kind"]:
            raise HTTPException(status_code=422, detail={
                "code": "CANDIDATE_TYPE_MISMATCH",
                "message": "Manually added candidates must keep the selected memory type.",
            })
        source_rows = connection.execute(
            """SELECT m.id AS message_id, m.batch_id, m.content, e.quote
               FROM candidate_evidence e
               JOIN normalized_messages m ON m.id = e.message_id
               WHERE e.candidate_id = ? AND m.batch_id = ?""",
            (candidate_id, row["batch_id"]),
        ).fetchall()
        archive_status = (
            "not_required"
            if row["origin"] == "manual"
            else archived_source_status(
                store.data_dir,
                store.evidence_dir,
                row["raw_relpath"],
                row["file_sha256"],
            )
        )
        source_texts = [source["content"] for source in source_rows if source["quote"] and source["quote"] in source["content"]] if archive_status == "available" else []
        approved_content = (body.content or row["content"]).strip()
        candidate_evidence_status = (
            "not_required"
            if row["origin"] == "manual"
            else (
                "valid"
                if archive_status == "available" and bool(source_rows)
                else "mismatch"
                if archive_status == "hash_mismatch"
                else "missing"
            )
            if row["origin"] == "import"
            else evidence_status(approved_content, source_texts)
        )
        if candidate_evidence_status not in {"valid", "not_required"}:
            raise HTTPException(status_code=422, detail={
                "code": "CANDIDATE_EVIDENCE_INVALID",
                "message": "A linked message and an unchanged archived source file are required before publication. Reject this candidate or restore its source before publication.",
            })
        if not approved_content:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "EMPTY_APPROVED_MEMORY",
                    "message": "Approved memory content cannot be empty.",
                },
            )
        publish_metadata: dict[str, object] | None = None
        if row["memory_type"] != "atomic_fact":
            try:
                publish_metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail={
                    "code": "INVALID_CANDIDATE_METADATA",
                    "message": "Candidate extraction metadata is invalid.",
                }) from exc
            if not isinstance(publish_metadata, dict):
                raise HTTPException(status_code=422, detail={
                    "code": "INVALID_CANDIDATE_METADATA",
                    "message": "Candidate extraction metadata is invalid.",
                })
            if row["memory_type"] == "profile":
                item = publish_metadata.get("item")
                if not isinstance(item, dict):
                    raise HTTPException(status_code=422, detail={
                        "code": "INVALID_CANDIDATE_METADATA",
                        "message": "Profile candidate item is invalid.",
                    })
                publish_metadata["item"] = {**item, "description": approved_content}
        revision = row["revision"] + (
            approved_content != row["content"] or body.kind != row["kind"]
        )
        evidence_kind = "human_authored" if row["origin"] == "manual" else body.evidence_kind
        valid_until = (datetime.now(timezone.utc) + timedelta(days=body.validity_days)).isoformat() if body.validity_days else None
        connection.execute(
            """UPDATE candidates SET content = ?, kind = ?, revision = ?,
               status = 'publishing', evidence_kind = ?, external_use = ?,
               target_project_id = ?, validity_days = ?, valid_until = ?,
               updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'pending' AND revision = ?""",
            (
                approved_content,
                body.kind,
                revision,
                evidence_kind,
                body.external_use,
                body.namespace,
                body.validity_days,
                valid_until,
                candidate_id,
                body.revision,
            ),
        )
        batch = dict(row)

    settings = get_settings()
    adapter = getattr(request.app.state, "everos_adapter", None) or EverOSHttpAdapter(
        settings.everos_base_url, timeout_seconds=settings.everos_timeout_seconds
    )
    scope = f"{batch['app_id']}/{body.namespace}/users/{batch['user_id']}"
    publish_kwargs: dict[str, object] = {
        "memory_id": candidate_id, "scope": scope, "content": approved_content,
    }
    if row["memory_type"] != "atomic_fact":
        publish_kwargs.update(memory_type=row["memory_type"], metadata=publish_metadata)
    try:
        memory = await adapter.publish_approved(**publish_kwargs)
    except EverOSAdapterError as exc:
        with store.connect() as connection:
            connection.execute(
                "UPDATE candidates SET status = 'pending', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'publishing'",
                (candidate_id,),
            )
            connection.execute(
                "INSERT INTO import_events (id, batch_id, event_type) "
                "VALUES (?, ?, 'candidate_publish_failed')",
                (uuid.uuid4().hex, batch["batch_id"]),
            )
        raise HTTPException(
            status_code=503, detail={"code": exc.code, "message": str(exc)}
        ) from exc

    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE candidates SET status = 'confirmed', everos_memory_id = ?,
               updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'publishing'""",
            (memory.memory_id, candidate_id),
        )
        connection.execute(
            """INSERT OR IGNORE INTO memories
               (id, everos_memory_id, namespace, kind, memory_type, status, revision,
                evidence_kind, external_use, origin, valid_until)
               VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?)""",
            (
                candidate_id,
                memory.memory_id,
                body.namespace,
                body.kind,
                row["memory_type"],
                memory.revision,
                evidence_kind,
                body.external_use,
                row["origin"],
                valid_until,
            ),
        )
        connection.execute(
            "INSERT INTO import_events (id, batch_id, event_type) "
            "VALUES (?, ?, 'candidate_confirmed')",
            (uuid.uuid4().hex, batch["batch_id"]),
        )
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data={
            "id": candidate_id,
            "status": "confirmed",
            "everos_memory_id": memory.memory_id,
            "revision": revision,
        },
    )


@router.post("/{candidate_id}/reject", response_model=ApiEnvelope[dict[str, object]])
async def reject_candidate(
    candidate_id: str, body: RejectCandidateRequest, request: Request
) -> ApiEnvelope[dict[str, object]]:
    store = request.app.state.vault_store
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT batch_id, revision, status FROM candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "CANDIDATE_NOT_FOUND",
                    "message": "Candidate does not exist.",
                },
            )
        if row["revision"] != body.revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CANDIDATE_REVISION_CONFLICT",
                    "message": (
                        "Candidate changed since it was loaded. Refresh before reviewing."
                    ),
                },
            )
        if row["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CANDIDATE_NOT_REVIEWABLE",
                    "message": f"Candidate status is {row['status']}.",
                },
            )
        connection.execute(
            "UPDATE candidates SET status = 'rejected', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (candidate_id,),
        )
        connection.execute(
            "INSERT INTO import_events (id, batch_id, event_type) "
            "VALUES (?, ?, 'candidate_rejected')",
            (uuid.uuid4().hex, row["batch_id"]),
        )
    return ApiEnvelope(
        request_id=uuid.uuid4().hex, data={"id": candidate_id, "status": "rejected"}
    )
