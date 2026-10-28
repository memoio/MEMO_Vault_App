"""Import preview and archive endpoints."""

from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from everos.umif.package import read_package

from ..adapter import EverOSAdapterError, EverOSHttpAdapter
from ..config import get_settings
from ..contracts import ApiEnvelope, ImportCommitRequest
from ..evidence import select_evidence_excerpt, validate_source_segment
from ..imports import ImportWorkflowError
from ..source_inventory import source_members
from ..storage import VaultStore

router = APIRouter(prefix="/imports", tags=["memo-vault-imports"])


@router.get("/{batch_id}/content-inventory")
async def content_inventory(batch_id: str, request: Request, page: int = 1, page_size: int = 50):
    """List archived UMIF tracks and source members without returning binary payloads."""
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="Invalid page")
    store = request.app.state.vault_store
    with store.connect() as c:
        batch = c.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise HTTPException(status_code=404, detail="Import batch not found")
        mapped = {row["source_record_id"]: dict(row) for row in c.execute(
            "SELECT source_record_id,target_type,target_id,status FROM umif_record_map WHERE batch_id=?", (batch_id,))}
        reviews = {row["source_record_id"]: dict(row) for row in c.execute(
            "SELECT source_record_id,decision,decided_at FROM import_content_reviews WHERE batch_id=?", (batch_id,))}
        evidence = [dict(row) for row in c.execute(
            "SELECT original_name,relative_path,sha256,media_type FROM evidence_files WHERE batch_id=?", (batch_id,))]
        tool_count = c.execute("SELECT count(*) FROM normalized_tool_events WHERE batch_id=?", (batch_id,)).fetchone()[0]
    root = store.data_dir.resolve()
    relpath = batch["umif_package_relpath"]
    if not relpath:
        raise HTTPException(status_code=404, detail="UMIF package unavailable")
    directory = (root / relpath).resolve()
    if not directory.is_relative_to(root):
        raise HTTPException(status_code=422, detail="Invalid package path")
    try:
        package = await run_in_threadpool(read_package, directory)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="UMIF package unreadable") from exc
    records = [*package.principals, *package.sessions, *package.events,
               *(record for group in package.optional_records.values() for record in group)]
    tracks = {"conversation": 0, "agent": 0, "knowledge": 0, "assertion": 0, "other": 0}
    items = []
    for record in records:
        kind = record.record_type
        track = ("agent" if kind in {"event.tool_call", "event.tool_result"}
                 else "conversation" if kind in {"session", "event.message"}
                 else "knowledge" if kind in {"asset", "document"}
                 else "assertion" if kind == "memory.assertion" else "other")
        tracks[track] += 1
        state = mapped.get(record.record_id, {})
        item = {"record_id": record.record_id, "record_type": kind, "track": track,
                "status": state.get("status", "archived"), "target_id": state.get("target_id"),
                "review_decision": reviews.get(record.record_id, {}).get("decision"),
                "reviewed_at": reviews.get(record.record_id, {}).get("decided_at"),
                "content_sha256": record.provenance.get("content_sha256"),
                "source_file": record.provenance.get("archive_name"),
                "source_member": record.provenance.get("member_path")}
        if kind in {"asset", "document"}:
            item["name"] = record.payload.get("filename") or record.payload.get("title")
            item["bytes"] = record.payload.get("size_bytes")
        items.append(item)
    source_files = []
    for file in evidence:
        source_files.append({"name": file["original_name"], "sha256": file["sha256"],
                             "media_type": file["media_type"], "status": "archived"})
        source = (root / file["relative_path"]).resolve()
        if source.is_relative_to(root) and source.is_file() and zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                for member in archive.infolist()[:1000]:
                    if not member.is_dir():
                        source_files.append({"name": member.filename, "bytes": member.file_size,
                                             "status": "retained_in_source", "container": file["original_name"]})
    source_info = (await run_in_threadpool(source_members, store, batch_id, package)
                   if batch["source_type"] == "claude" else
                   {"files": source_files, "total": len(source_files), "truncated": False})
    return ApiEnvelope(request_id=uuid.uuid4().hex,
                       data={"batch_id": batch_id, "package_id": batch["umif_package_id"],
                             "tracks": tracks, "agent_events_stored": tool_count,
                             "total": len(items), "page": page, "page_size": page_size,
                             "items": items[(page-1)*page_size:page*page_size],
                             "source_files": source_info["files"],
                             "source_file_total": source_info["total"],
                             "source_files_truncated": source_info["truncated"],
                             "report": package.import_report})


def _raise_api_error(error: ImportWorkflowError) -> None:
    stage = "umif_validate" if error.code == "UMIF_VALIDATION_FAILED" else (
        "vault_commit" if error.code in {"IMPORT_SOURCE_EVENT_CONFLICT", "PREVIEW_CORRUPTED"} else "decode"
    )
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error), "stage": stage},
    ) from error


def _persist_candidates(
    store: VaultStore,
    *,
    batch_id: str,
    batch: Mapping[str, object],
    candidates: object,
) -> int:
    """Persist a whole extraction result with its evidence in one transaction."""
    if not isinstance(candidates, list):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "EVEROS_INVALID_CANDIDATE_RESPONSE",
                "message": "EverOS returned an invalid candidate list.",
            },
        )
    valid_kinds = {"preference", "fact", "decision", "todo", "episode", "profile"}
    valid_memory_types = {"atomic_fact", "episode", "profile"}
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "EVEROS_INVALID_CANDIDATE_RESPONSE",
                        "message": "EverOS returned an invalid candidate record.",
                    },
                )
            candidate_id = candidate.get("candidate_id")
            kind = candidate.get("kind")
            memory_type = candidate.get("memory_type", "atomic_fact")
            metadata = candidate.get("metadata")
            content = candidate.get("content")
            source_ids = candidate.get("source_message_ids")
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or not isinstance(kind, str)
                or kind not in valid_kinds
                or memory_type not in valid_memory_types
                or (metadata is not None and not isinstance(metadata, dict))
                or (memory_type == "episode" and kind != "episode")
                or (memory_type == "profile" and kind not in {"profile", "preference"})
                or (memory_type == "atomic_fact" and kind in {"episode", "profile"})
                or not isinstance(content, str)
                or not content.strip()
                or len(content) > 4000
                or not isinstance(source_ids, list)
                or not source_ids
                or not all(isinstance(source_id, str) for source_id in source_ids)
            ):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "EVEROS_INVALID_CANDIDATE_RESPONSE",
                        "message": (
                            "EverOS returned a candidate without valid content or "
                            "source evidence."
                        ),
                    },
                )
            unique_source_ids = list(dict.fromkeys(source_ids))
            placeholders = ",".join("?" for _ in unique_source_ids)
            source_rows = connection.execute(
                f"""SELECT id, conversation_id, content, timestamp_ms FROM normalized_messages
                    WHERE batch_id = ? AND memory_input = 1 AND is_duplicate = 0
                      AND id IN ({placeholders})""",
                (batch_id, *unique_source_ids),
            ).fetchall()
            if any(row['timestamp_ms'] is None for row in source_rows):
                metadata = {**(metadata or {}), 'source_time_quality': 'inferred_processing_time',
                            'source_timestamps_missing': True}
            if len(source_rows) != len(unique_source_ids):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "EVEROS_INVALID_CANDIDATE_RESPONSE",
                        "message": (
                            "EverOS returned a candidate linked to messages outside "
                            "this batch."
                        ),
                    },
                )
            source_segment_value = candidate.get("source_segment")
            source_segment = None
            if source_segment_value is not None:
                source_segment = validate_source_segment(source_segment_value, source_rows)
                if source_segment is None:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "code": "EVEROS_INVALID_CANDIDATE_SOURCE_SEGMENT",
                            "message": "EverOS returned source offsets that do not match this batch's archived messages.",
                        },
                    )
            scope = (
                f"{batch['app_id']}/{batch['project_id']}/users/{batch['user_id']}"
            )
            if memory_type == "profile":
                from everos.service.memo_vault_candidates import profile_contents_duplicate

                previous_profiles = connection.execute(
                    "SELECT content FROM candidates WHERE memory_type = 'profile' "
                    "AND everos_scope = ? AND id != ? AND status IN ('pending', 'confirmed')",
                    (scope, candidate_id),
                ).fetchall()
                if any(profile_contents_duplicate(content, row["content"]) for row in previous_profiles):
                    continue
            existing = connection.execute(
                "SELECT batch_id FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if existing is not None and existing["batch_id"] != batch_id:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "EVEROS_INVALID_CANDIDATE_RESPONSE",
                        "message": (
                            "EverOS returned a candidate ID already owned by another "
                            "batch."
                        ),
                    },
                )
            connection.execute(
                """INSERT OR IGNORE INTO candidates
                   (id, batch_id, everos_scope, kind, memory_type, metadata_json,
                    source_segment_json, content, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (candidate_id, batch_id, scope, kind, memory_type,
                 json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
                 json.dumps(source_segment, ensure_ascii=False) if source_segment is not None else None,
                 content.strip()),
            )
            if source_segment is not None:
                connection.execute(
                    """UPDATE candidates SET source_segment_json = COALESCE(source_segment_json, ?)
                       WHERE id = ? AND batch_id = ?""",
                    (json.dumps(source_segment, ensure_ascii=False), candidate_id, batch_id),
                )
            for source_row in source_rows:
                excerpt = select_evidence_excerpt(content, source_row["content"])
                quote = excerpt.quote
                connection.execute(
                    """INSERT OR IGNORE INTO candidate_evidence
                       (candidate_id, message_id, char_start, char_end, quote, quote_sha256)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        candidate_id,
                        source_row["id"],
                        excerpt.char_start,
                        excerpt.char_end,
                        quote,
                        hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                    ),
                )
        connection.execute(
            """UPDATE import_batches SET extraction_status = 'extracted',
               extraction_method = 'local', error_stage = NULL,
               error_code = NULL, error_message = NULL WHERE id = ?""",
            (batch_id,),
        )
        connection.execute(
            """INSERT INTO import_events (id, batch_id, event_type)
               VALUES (?, ?, 'extraction_succeeded')""",
            (uuid.uuid4().hex, batch_id),
        )
    return len(candidates)


@router.post("/preview", response_model=ApiEnvelope[dict[str, object]])
async def preview_import(
    request: Request,
    file: Annotated[UploadFile, File()],
    source_type: Annotated[str, Form()] = "generic",
    project_id: Annotated[str | None, Form()] = None,
) -> ApiEnvelope[dict[str, object]]:
    service = request.app.state.import_service
    try:
        payload = await file.read(service.max_file_bytes + 1)
        if len(payload) > service.max_file_bytes:
            raise ImportWorkflowError(
                "IMPORT_SECURITY_REJECTED",
                "File exceeds the configured "
                f"{service.max_file_bytes // (1024 * 1024)} MiB limit.",
                status_code=413,
            )
        data = service.preview(
            file.filename or "upload.txt",
            source_type,
            payload,
            content_type=file.content_type,
            project_id=project_id,
        )
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
    finally:
        await file.close()
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)


@router.post("/commit", response_model=ApiEnvelope[dict[str, object]])
async def commit_import(
    body: ImportCommitRequest, request: Request
) -> ApiEnvelope[dict[str, object]]:
    try:
        data = request.app.state.import_service.commit(body.preview_id, body.project_id)
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)


@router.delete("/previews/{preview_id}", response_model=ApiEnvelope[dict[str, bool]])
async def cancel_preview(
    preview_id: str, request: Request
) -> ApiEnvelope[dict[str, bool]]:
    try:
        request.app.state.import_service.cancel_preview(preview_id)
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
    return ApiEnvelope(request_id=uuid.uuid4().hex, data={"cancelled": True})


@router.get("/{batch_id}/status", response_model=ApiEnvelope[dict[str, object]])
async def import_status(batch_id: str, request: Request) -> ApiEnvelope[dict[str, object]]:
    """Return safe retry diagnostics and the durable UMIF package reference."""
    store = request.app.state.vault_store
    with store.connect() as connection:
        row = connection.execute(
            """SELECT id, status, extraction_status, error_stage, error_code,
                      error_message, umif_package_relpath, umif_package_id,
                      umif_record_count, umif_validation_json
               FROM import_batches WHERE id = ?""", (batch_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "IMPORT_BATCH_NOT_FOUND", "message": "Import batch does not exist."})
    data = dict(row)
    data["umif_validation"] = json.loads(data.pop("umif_validation_json") or "{}")
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)


@router.post("/{batch_id}/extract", response_model=ApiEnvelope[dict[str, object]])
async def extract_import(
    batch_id: str, request: Request
) -> ApiEnvelope[dict[str, object]]:
    store = request.app.state.vault_store
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        batch = connection.execute(
            """SELECT id, extraction_status, app_id, project_id, user_id, agent_id
               FROM import_batches WHERE id = ?""",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "IMPORT_BATCH_NOT_FOUND",
                    "message": "Import batch does not exist.",
                },
            )
        if batch["extraction_status"] == "extracted":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EXTRACTION_ALREADY_COMPLETED",
                    "message": "This batch has already been extracted.",
                },
            )
        if batch["extraction_status"] == "running":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "EXTRACTION_ALREADY_RUNNING",
                    "message": "Extraction is already running.",
                },
            )
        messages = [
            dict(row)
            for row in connection.execute(
                """SELECT id, conversation_id, ordinal, role, content, timestamp_ms
                   FROM normalized_messages
                   WHERE batch_id = ? AND is_duplicate = 0 AND memory_input = 1
                   ORDER BY conversation_id, ordinal""",
                (batch_id,),
            ).fetchall()
        ]
        if not messages:
            connection.execute(
                """UPDATE import_batches SET extraction_status = 'extracted',
                   extraction_method = 'none', error_stage = NULL,
                   error_code = NULL, error_message = NULL
                   WHERE id = ?""",
                (batch_id,),
            )
            connection.execute(
                """INSERT INTO import_events (id, batch_id, event_type)
                   VALUES (?, ?, 'extraction_skipped_duplicate_messages')""",
                (uuid.uuid4().hex, batch_id),
            )
            no_new_messages = True
            deferred_tool_messages = False
        elif not any(
            message["role"] in {"user", "assistant"} for message in messages
        ):
            connection.execute(
                """UPDATE import_batches SET extraction_status = 'not_started',
                   extraction_method = NULL WHERE id = ?""",
                (batch_id,),
            )
            deferred_tool_messages = any(
                message["role"] == "tool" for message in messages
            )
            skip_event = (
                "extraction_deferred_tool_messages"
                if deferred_tool_messages
                else "extraction_skipped_unsupported_roles"
            )
            connection.execute(
                """INSERT INTO import_events (id, batch_id, event_type)
                   VALUES (?, ?, ?)""",
                (uuid.uuid4().hex, batch_id, skip_event),
            )
            no_new_messages = False
        else:
            no_new_messages = False
            deferred_tool_messages = False
        if not no_new_messages and not deferred_tool_messages:
            claimed = connection.execute(
                """UPDATE import_batches
                   SET extraction_status = 'running',
                       error_stage = NULL, error_code = NULL, error_message = NULL
                   WHERE id = ?
                     AND extraction_status IN ('not_started', 'failed', 'cancelled')""",
                (batch_id,),
            )
            if claimed.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "EXTRACTION_ALREADY_RUNNING",
                        "message": "Extraction is already running.",
                    },
                )
            connection.execute(
                """INSERT INTO import_events (id, batch_id, event_type)
                   VALUES (?, ?, 'extraction_started')""",
                (uuid.uuid4().hex, batch_id),
            )

    if no_new_messages:
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data={
                "status": "no_new_messages",
                "method": "none",
                "candidate_count": 0,
                "candidate_scope": None,
                "deferred_tool_message_count": sum(
                    message["role"] == "tool" for message in messages
                ),
                "unsupported_role_message_count": 0,
            },
        )
    if deferred_tool_messages:
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data={
                "status": "deferred_tool_messages",
                "method": "none",
                "candidate_count": 0,
                "candidate_scope": None,
                "deferred_tool_message_count": sum(
                    message["role"] == "tool" for message in messages
                ),
                "unsupported_role_message_count": sum(
                    message["role"] not in {"user", "assistant", "tool"}
                    for message in messages
                ),
            },
        )
    if not any(message["role"] in {"user", "assistant"} for message in messages):
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data={
                "status": "unsupported_role_messages",
                "method": "none",
                "candidate_count": 0,
                "candidate_scope": None,
                "deferred_tool_message_count": 0,
                "unsupported_role_message_count": len(messages),
            },
        )

    settings = get_settings()
    adapter = EverOSHttpAdapter(
        settings.everos_base_url,
        timeout_seconds=settings.everos_timeout_seconds,
    )
    try:
        result = await adapter.extract_import(
            batch_id=batch_id,
            app_id=batch["app_id"],
            project_id=batch["project_id"],
            user_id=batch["user_id"],
            agent_id=batch["agent_id"],
            messages=messages,
        )
    except EverOSAdapterError as exc:
        with store.connect() as connection:
            connection.execute(
                """UPDATE import_batches SET extraction_status = 'failed',
                   error_stage = ?, error_code = ?, error_message = ? WHERE id = ?""",
                (exc.stage or "everos_contract", exc.code, str(exc), batch_id),
            )
            connection.execute(
                """INSERT INTO import_events (id, batch_id, event_type)
                   VALUES (?, ?, 'extraction_failed')""",
                (uuid.uuid4().hex, batch_id),
            )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc), "stage": exc.stage or "everos_contract"},
        ) from exc

    try:
        result["candidate_count"] = _persist_candidates(
            store,
            batch_id=batch_id,
            batch=batch,
            candidates=result.get("candidates", []),
        )
    except Exception as exc:
        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            error_code = str(
                exc.detail.get("code", "CANDIDATE_PERSISTENCE_FAILED")
            )
            error_message = str(
                exc.detail.get("message", "Candidate persistence failed.")
            )
            error_status = exc.status_code
        else:
            error_code = "CANDIDATE_PERSISTENCE_FAILED"
            error_message = "Candidate results could not be saved to the Vault."
            error_status = 500
        with store.connect() as connection:
            connection.execute(
                """UPDATE import_batches SET extraction_status = 'failed',
                   error_stage = 'candidate_persist',
                   error_code = ?, error_message = ? WHERE id = ?""",
                (
                    error_code,
                    error_message,
                    batch_id,
                ),
            )
            connection.execute(
                """INSERT INTO import_events (id, batch_id, event_type)
                   VALUES (?, ?, 'extraction_failed')""",
                (uuid.uuid4().hex, batch_id),
            )
        if isinstance(exc, HTTPException):
            raise HTTPException(
                status_code=error_status,
                detail={"code": error_code, "message": error_message, "stage": "candidate_persist"},
            ) from exc
        raise HTTPException(
            status_code=error_status,
            detail={"code": error_code, "message": error_message, "stage": "candidate_persist"},
        ) from exc
    result.pop("candidates", None)
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=result)
