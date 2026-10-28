"""Review platform summaries and explicitly deliver archived project documents."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from pathlib import PurePosixPath
from typing import Any

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from everos.component.utils.datetime import get_utc_now, to_timestamp_ms
from everos.umif.canonical import content_hash
from everos.umif.package import read_package
from everos.umif.validator import validate_package

from ..contracts import ApiEnvelope
from ..import_contracts import ImportWorkflowError
from ..imports import ImportService
from ..source_inventory import source_members
from .imports import _raise_api_error

router = APIRouter(prefix="/imports", tags=["memo-vault-claude-content"])


def _load(request: Request, batch_id: str) -> tuple[Any, Any]:
    store = request.app.state.vault_store
    with store.connect() as c:
        batch = c.execute(
            "SELECT * FROM import_batches WHERE id=? AND source_type IN ('claude','chatgpt')",
            (batch_id,),
        ).fetchone()
    if batch is None:
        raise ImportWorkflowError(
            "IMPORT_BATCH_NOT_FOUND", "Claude import not found.", status_code=404
        )
    directory = store.data_dir / batch["umif_package_relpath"]
    if not directory.resolve().is_relative_to(store.data_dir.resolve()):
        raise ImportWorkflowError("UMIF_VALIDATION_FAILED", "Invalid package path.")
    try:
        package = read_package(directory)
    except (OSError, ValueError) as exc:
        raise ImportWorkflowError(
            "UMIF_VALIDATION_FAILED", "Archived package is unreadable."
        ) from exc
    report = validate_package(package, directory)
    if not report.valid:
        raise ImportWorkflowError("UMIF_VALIDATION_FAILED", ", ".join(report.errors))
    return batch, package


def _content(request: Request, batch_id: str) -> dict[str, Any]:
    batch, package = _load(request, batch_id)
    with request.app.state.vault_store.connect() as c:
        states = {
            r["source_record_id"]: dict(r)
            for r in c.execute(
                "SELECT * FROM umif_record_map WHERE batch_id=?", (batch_id,)
            )
        }
        reviews = {
            r["source_record_id"]: dict(r)
            for r in c.execute(
                "SELECT * FROM import_content_reviews WHERE batch_id=?", (batch_id,)
            )
        }
        evidence = [dict(r) for r in c.execute(
            "SELECT original_name,sha256,media_type FROM evidence_files WHERE batch_id=?",
            (batch_id,),
        )]

    def values(name: str) -> list[dict[str, Any]]:
        results = []
        for r in package.optional_records.get(name, ()):
            payload = dict(r.payload)
            if name == "documents.jsonl" and isinstance(payload.get("text"), str):
                full_text = payload["text"]
                payload["text"] = full_text[:4000]
                payload["text_length"] = len(full_text)
                payload["text_truncated"] = len(full_text) > 4000
            results.append({
                "record_id": r.record_id,
                **payload,
                "status": states.get(r.record_id, {}).get("status", "archived"),
                "target_id": states.get(r.record_id, {}).get("target_id"),
                "review_decision": reviews.get(r.record_id, {}).get("decision"),
                "reviewed_at": reviews.get(r.record_id, {}).get("decided_at"),
            })
        return results

    source_inventory = (source_members(request.app.state.vault_store, batch_id, package)
                        if batch["source_type"] == "claude" else
                        {"files": evidence, "total": len(evidence), "truncated": False})
    agent_events = [
        record for record in package.events
        if record.record_type in {"event.tool_call", "event.tool_result"}
    ]

    def trace_preview(record: Any) -> str:
        payload = record.payload
        value = (payload.get("arguments") if record.record_type == "event.tool_call"
                 else payload.get("content_parts"))
        safe = ImportService._safe_trace_preview(value)
        rendered = json.dumps(safe, ensure_ascii=False, indent=2)
        return rendered[:2000] + ("…" if len(rendered) > 2000 else "")

    return {
        "documents": values("documents.jsonl"),
        "assertions": values("memories.jsonl"),
        "agent_event_count": len(agent_events),
        "agent_events": [
            {"record_id": r.record_id, "record_type": r.record_type,
             "call_id": r.payload.get("call_id"),
             "tool_name": r.payload.get("tool_name"),
             "sequence": r.sequence, "timestamp_ms": r.timestamp_ms,
             "is_error": bool(r.payload.get("is_error")),
             "preview": trace_preview(r)}
            for r in agent_events[:20]
        ],
        "assets": [
            {"record_id": r.record_id, "filename": r.payload.get("filename"),
             "media_type": r.payload.get("media_type"),
             "bytes": len(package.blobs.get(r.payload.get("blob_path"), b"")),
             "status": states.get(r.record_id, {}).get("status", "archived"),
             "target_id": states.get(r.record_id, {}).get("target_id"),
             "review_decision": reviews.get(r.record_id, {}).get("decision"),
             "linked_document_ids": [d.record_id for d in package.optional_records.get("documents.jsonl", ())
                                     if d.payload.get("asset_id") == r.record_id]}
            for r in package.optional_records.get("assets.jsonl", ())
        ],
        "source_files": source_inventory["files"] or evidence,
        "source_file_total": source_inventory["total"],
        "source_files_truncated": source_inventory["truncated"],
        "scope": {"app_id": batch["app_id"], "project_id": batch["project_id"]},
        "report": package.import_report,
    }


@router.get("/{batch_id}/claude-content")
async def get_content(batch_id: str, request: Request) -> ApiEnvelope[dict[str, Any]]:
    try:
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data=await run_in_threadpool(_content, request, batch_id),
        )
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


@router.get("/{batch_id}/documents/{record_id}/text")
async def document_text(batch_id: str, record_id: str, request: Request,
                        offset: int = 0, limit: int = 4000) -> ApiEnvelope[dict[str, Any]]:
    try:
        if offset < 0 or limit < 1 or limit > 10000:
            raise ImportWorkflowError("CLAUDE_INVALID_RANGE", "Invalid text range.")
        _, package = await run_in_threadpool(_load, request, batch_id)
        record = next((r for r in package.optional_records.get("documents.jsonl", ())
                       if r.record_id == record_id), None)
        if record is None:
            raise ImportWorkflowError("CLAUDE_RECORD_NOT_FOUND", "Document not found.", status_code=404)
        value = record.payload.get("text") or ""
        end = min(len(value), offset + limit)
        return ApiEnvelope(request_id=uuid.uuid4().hex,
                           data={"text": value[offset:end], "next_offset": end,
                                 "has_more": end < len(value), "text_length": len(value)})
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


def _review(
    request: Request, batch_id: str, record_id: str, edited_content: str | None = None
) -> dict[str, Any]:
    batch, package = _load(request, batch_id)
    record = next(
        (
            r
            for r in package.optional_records.get("memories.jsonl", ())
            if r.record_id == record_id
        ),
        None,
    )
    if record is None:
        raise ImportWorkflowError(
            "CLAUDE_RECORD_NOT_FOUND", "Saved memory not found.", status_code=404
        )
    text = record.payload["text"]
    candidate_text = edited_content.strip() if edited_content is not None else text
    if not candidate_text.strip() or len(candidate_text) > 4000:
        raise ImportWorkflowError(
            "CLAUDE_ASSERTION_REVIEW_REQUIRED",
            "Edit this saved summary to at most 4000 characters "
            "before creating a candidate.",
        )
    # Candidate identity includes target scope and source content revision.
    key = content_hash(
        [batch["app_id"], batch["project_id"], record_id, text, candidate_text]
    ).split(":")[1]
    candidate_id, message_id = "claude_" + key, "claude_source_" + key
    scope = f"{batch['app_id']}/{batch['project_id']}/users/{batch['user_id']}"
    store = request.app.state.vault_store
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT id FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if existing is None:
            c.execute(
                """INSERT INTO normalized_messages
                (id,batch_id,conversation_id,ordinal,role,content,
                 timestamp_ms,content_sha256,source_message_id,memory_input)
                VALUES (?,?,?,?,?,?,?,?,?,0)""",
                (
                    message_id,
                    batch_id,
                    "saved:" + record_id,
                    0,
                    "platform_summary",
                    text,
                    record.timestamp_ms,
                    hashlib.sha256(text.encode()).hexdigest(),
                    record_id,
                ),
            )
            c.execute(
                """INSERT INTO candidates
                (id,batch_id,everos_scope,kind,memory_type,metadata_json,content,
                 status,origin,evidence_kind,target_project_id)
                VALUES (?,?,?,'preference','profile',?,?,
                        'pending','import','source_claim',?)""",
                (
                    candidate_id,
                    batch_id,
                    scope,
                    json.dumps(
                        {
                            "origin_kind": "platform_summary",
                            "source_record_id": record_id,
                            "section": "explicit_info",
                            "timestamp_ms": to_timestamp_ms(get_utc_now()),
                            "timestamp_basis": "review_created_at",
                            "source_timestamp_ms": record.timestamp_ms,
                            "item": {
                                "description": candidate_text,
                                "evidence": "Claude platform summary",
                            },
                        }
                    ),
                    candidate_text,
                    batch["project_id"],
                ),
            )
            c.execute(
                """INSERT INTO candidate_evidence
                (candidate_id,message_id,char_start,char_end,quote,quote_sha256)
                VALUES (?,?,0,?,?,?)""",
                (
                    candidate_id,
                    message_id,
                    len(text),
                    text,
                    hashlib.sha256(text.encode()).hexdigest(),
                ),
            )
        c.execute(
            "UPDATE umif_record_map SET "
            "status='review_pending',target_id=? "
            "WHERE batch_id=? AND source_record_id=?",
            (candidate_id, batch_id, record_id),
        )
    return {
        "candidate_id": candidate_id,
        "status": "review_pending",
        "origin_kind": "platform_summary",
    }


class AssertionReview(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)


@router.post("/{batch_id}/assertions/{record_id}/review")
async def review_assertion(
    batch_id: str, record_id: str, request: Request, body: AssertionReview | None = None
) -> ApiEnvelope[dict[str, Any]]:
    try:
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data=await run_in_threadpool(
                _review, request, batch_id, record_id, body.content if body else None
            ),
        )
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


def _document_upload(package: Any, record: Any) -> tuple[str, bytes, str]:
    """Compile UMIF asset references into a stable Knowledge upload."""
    asset = next(
        (
            a
            for a in package.optional_records.get("assets.jsonl", ())
            if a.record_id == record.payload.get("asset_id")
        ),
        None,
    )
    if asset is None or asset.payload.get("blob_path") not in package.blobs:
        raise ImportWorkflowError(
            "UMIF_VALIDATION_FAILED", "Document asset is unavailable."
        )
    name = asset.payload.get("filename", "document.txt")
    suffix = PurePosixPath(name).suffix.lower() or ".txt"
    filename = (
        record.record_id
        + "-"
        + record.provenance["content_sha256"].split(":")[1][:16]
        + suffix
    )
    return (
        filename,
        package.blobs[asset.payload["blob_path"]],
        mimetypes.guess_type(name)[0] or "application/octet-stream",
    )


def _asset_upload(package: Any, asset: Any) -> tuple[str, bytes, str]:
    blob_path = asset.payload.get("blob_path")
    if blob_path not in package.blobs:
        raise ImportWorkflowError("UMIF_VALIDATION_FAILED", "Attachment asset is unavailable.")
    name = asset.payload.get("filename") or "attachment.bin"
    suffix = PurePosixPath(name).suffix.lower() or ".bin"
    filename = asset.record_id + "-" + asset.provenance["content_sha256"].split(":")[1][:16] + suffix
    return filename, package.blobs[blob_path], asset.payload.get("media_type") or "application/octet-stream"


class DocumentDelivery(BaseModel):
    confirmed: bool = False


class DocumentReview(BaseModel):
    decision: str = Field(pattern="^(knowledge|vault_only|exclude)$")


def _review_document(request: Request, batch_id: str, record_id: str, decision: str,
                     record_file: str = "documents.jsonl") -> dict[str, Any]:
    batch, package = _load(request, batch_id)
    if not any(r.record_id == record_id for r in package.optional_records.get(record_file, ())):
        raise ImportWorkflowError("CLAUDE_RECORD_NOT_FOUND", "File not found.", status_code=404)
    if record_file == "assets.jsonl" and any(
        d.payload.get("asset_id") == record_id for d in package.optional_records.get("documents.jsonl", ())
    ):
        raise ImportWorkflowError("CLAUDE_ASSET_LINKED_DOCUMENT",
                                  "Review the linked document instead.", status_code=409)
    with request.app.state.vault_store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        mapped = c.execute(
            "SELECT status FROM umif_record_map WHERE batch_id=? AND source_record_id=?",
            (batch_id, record_id),
        ).fetchone()
        if mapped and mapped["status"] in ("delivered", "submitting", "delivery_unknown"):
            raise ImportWorkflowError("CLAUDE_REVIEW_LOCKED", "Delivery has already started.", status_code=409)
        c.execute(
            """INSERT INTO import_content_reviews
            (batch_id,source_record_id,decision,reviewer_id,app_id,project_id)
            VALUES (?,?,?,?,?,?) ON CONFLICT(batch_id,source_record_id) DO UPDATE SET
            decision=excluded.decision,reviewer_id=excluded.reviewer_id,
            decided_at=CURRENT_TIMESTAMP""",
            (batch_id, record_id, decision, batch["user_id"], batch["app_id"], batch["project_id"]),
        )
        c.execute(
            "UPDATE umif_record_map SET status=? WHERE batch_id=? AND source_record_id=?",
            ({"knowledge": "approved", "vault_only": "vault_only", "exclude": "excluded"}[decision], batch_id, record_id),
        )
        if record_file == "documents.jsonl":
            document = next(r for r in package.optional_records["documents.jsonl"] if r.record_id == record_id)
            asset_id = document.payload.get("asset_id")
            linked = [r for r in package.optional_records.get("documents.jsonl", ())
                      if r.payload.get("asset_id") == asset_id]
            if asset_id and len(linked) == 1:
                c.execute(
                    """INSERT INTO import_content_reviews
                    (batch_id,source_record_id,decision,reviewer_id,app_id,project_id)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(batch_id,source_record_id) DO UPDATE SET
                    decision=excluded.decision,reviewer_id=excluded.reviewer_id,
                    decided_at=CURRENT_TIMESTAMP""",
                    (batch_id, asset_id, decision, batch["user_id"], batch["app_id"], batch["project_id"]),
                )
                c.execute(
                    "UPDATE umif_record_map SET status=? WHERE batch_id=? AND source_record_id=?",
                    ({"knowledge": "approved", "vault_only": "vault_only", "exclude": "excluded"}[decision], batch_id, asset_id),
                )
    return {"decision": decision, "status": {"knowledge": "approved", "vault_only": "vault_only", "exclude": "excluded"}[decision]}


@router.post("/{batch_id}/documents/{record_id}/review")
async def review_document(batch_id: str, record_id: str, body: DocumentReview, request: Request) -> ApiEnvelope[dict[str, Any]]:
    try:
        return ApiEnvelope(request_id=uuid.uuid4().hex,
                           data=await run_in_threadpool(_review_document, request, batch_id, record_id, body.decision))
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


@router.post("/{batch_id}/assets/{record_id}/review")
async def review_asset(batch_id: str, record_id: str, body: DocumentReview, request: Request) -> ApiEnvelope[dict[str, Any]]:
    try:
        return ApiEnvelope(request_id=uuid.uuid4().hex,
                           data=await run_in_threadpool(_review_document, request, batch_id,
                                                        record_id, body.decision, "assets.jsonl"))
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


@router.post("/{batch_id}/documents/{record_id}/deliver")
async def deliver_document(
    batch_id: str, record_id: str, body: DocumentDelivery, request: Request
) -> ApiEnvelope[dict[str, Any]]:
    return await _deliver_record(batch_id, record_id, body, request, "documents.jsonl")


@router.post("/{batch_id}/assets/{record_id}/deliver")
async def deliver_asset(
    batch_id: str, record_id: str, body: DocumentDelivery, request: Request
) -> ApiEnvelope[dict[str, Any]]:
    return await _deliver_record(batch_id, record_id, body, request, "assets.jsonl")


async def _deliver_record(
    batch_id: str, record_id: str, body: DocumentDelivery, request: Request,
    record_file: str,
) -> ApiEnvelope[dict[str, Any]]:
    """Submit once; reconcile unknown outcomes before another submission."""
    store = request.app.state.vault_store
    try:
        if not body.confirmed:
            raise ImportWorkflowError(
                "CLAUDE_DOCUMENT_CONFIRMATION_REQUIRED",
                "Confirm this document and target project before Knowledge delivery.",
            )
        batch, package = await run_in_threadpool(_load, request, batch_id)
        record = next(
            (
                r
                for r in package.optional_records.get(record_file, ())
                if r.record_id == record_id
            ),
            None,
        )
        if record is None:
            raise ImportWorkflowError(
                "CLAUDE_RECORD_NOT_FOUND", "File not found.", status_code=404
            )
        if record_file == "assets.jsonl" and any(
            d.payload.get("asset_id") == record_id
            for d in package.optional_records.get("documents.jsonl", ())
        ):
            raise ImportWorkflowError("CLAUDE_ASSET_LINKED_DOCUMENT",
                                      "Deliver the linked document instead.", status_code=409)

        def claim() -> dict[str, Any]:
            with store.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                review = c.execute(
                    "SELECT decision FROM import_content_reviews WHERE batch_id=? AND source_record_id=?",
                    (batch_id, record_id),
                ).fetchone()
                if review is None and record_file == "documents.jsonl":
                    # The legacy explicit confirmation button is itself a review
                    # decision; persist it before claiming the delivery.
                    c.execute(
                        """INSERT INTO import_content_reviews
                        (batch_id,source_record_id,decision,reviewer_id,app_id,project_id)
                        VALUES (?,?,'knowledge',?,?,?)""",
                        (batch_id, record_id, batch["user_id"], batch["app_id"], batch["project_id"]),
                    )
                    asset_id = record.payload.get("asset_id")
                    linked = [r for r in package.optional_records.get("documents.jsonl", ())
                              if r.payload.get("asset_id") == asset_id]
                    if asset_id and len(linked) == 1:
                        c.execute(
                            """INSERT OR IGNORE INTO import_content_reviews
                            (batch_id,source_record_id,decision,reviewer_id,app_id,project_id)
                            VALUES (?,?,'knowledge',?,?,?)""",
                            (batch_id, asset_id, batch["user_id"], batch["app_id"], batch["project_id"]),
                        )
                elif review is None or review["decision"] != "knowledge":
                    raise ImportWorkflowError(
                        "CLAUDE_DOCUMENT_REVIEW_REQUIRED",
                        "Approve this document for Knowledge before delivery.", status_code=409,
                    )
                previous = c.execute(
                    """SELECT m.status,m.target_id FROM umif_record_map m
                    JOIN import_batches b ON b.id=m.batch_id
                    WHERE b.app_id=? AND b.project_id=?
                    AND m.source_record_id=? AND m.content_sha256=?
                    AND m.status IN ('delivered','submitting','delivery_unknown')
                    ORDER BY CASE m.status WHEN 'delivered' THEN 0 ELSE 1 END
                    LIMIT 1""",
                    (
                        batch["app_id"],
                        batch["project_id"],
                        record_id,
                        record.provenance["content_sha256"],
                    ),
                ).fetchone()
                if previous:
                    if previous["status"] == "delivered":
                        c.execute(
                            "UPDATE umif_record_map SET status='delivered',target_id=? "
                            "WHERE batch_id=? AND source_record_id=?",
                            (previous["target_id"], batch_id, record_id),
                        )
                        if record_file == "documents.jsonl":
                            asset_id = record.payload.get("asset_id")
                            linked = [r for r in package.optional_records.get("documents.jsonl", ())
                                      if r.payload.get("asset_id") == asset_id]
                            if asset_id and len(linked) == 1:
                                c.execute(
                                    "UPDATE umif_record_map SET status='linked_to_document',target_id=? "
                                    "WHERE batch_id=? AND source_record_id=?",
                                    (previous["target_id"], batch_id, asset_id),
                                )
                    return dict(previous)
                c.execute(
                    "UPDATE umif_record_map SET "
                    "status='submitting' WHERE batch_id=? AND source_record_id=?",
                    (batch_id, record_id),
                )
            return {"status": "claimed"}

        claimed = await run_in_threadpool(claim)
        if claimed["status"] == "delivered":
            return ApiEnvelope(request_id=uuid.uuid4().hex, data=claimed)
        if claimed["status"] != "claimed":
            raise ImportWorkflowError(
                "CLAUDE_DELIVERY_OUTCOME_UNKNOWN",
                "A previous delivery is pending or its outcome is "
                "unknown. Reconcile it before resubmitting.",
                status_code=409,
            )

        def outcome(status: str, target_id: str | None = None) -> None:
            with store.connect() as c:
                c.execute(
                    "UPDATE umif_record_map SET "
                    "status=?,target_id=? WHERE batch_id=? AND source_record_id=?",
                    (status, target_id, batch_id, record_id),
                )
                if status == "delivered" and record_file == "documents.jsonl":
                    asset_id = record.payload.get("asset_id")
                    linked = [r for r in package.optional_records.get("documents.jsonl", ())
                              if r.payload.get("asset_id") == asset_id]
                    if asset_id and len(linked) == 1:
                        c.execute(
                            "UPDATE umif_record_map SET status='linked_to_document',target_id=? "
                            "WHERE batch_id=? AND source_record_id=?",
                            (target_id, batch_id, asset_id),
                        )

        adapter = request.app.state.everos_adapter
        try:
            async with httpx.AsyncClient(
                timeout=adapter.timeout_seconds, transport=adapter.transport
            ) as client:
                response = await client.post(
                    f"{adapter.base_url}/api/v2/knowledge/documents",
                    data={
                        "title": record.payload.get("title") or record.payload.get("filename") or "Attachment",
                        "source_type": "claude",
                        "app_id": batch["app_id"],
                        "project_id": batch["project_id"],
                    },
                    files={"file": _document_upload(package, record) if record_file == "documents.jsonl"
                           else _asset_upload(package, record)},
                )
            if response.status_code >= 400:
                await run_in_threadpool(
                    outcome,
                    "delivery_rejected"
                    if 400 <= response.status_code < 500
                    else "delivery_unknown",
                )
                code = "CLAUDE_KNOWLEDGE_FAILED"
                message = (
                    f"Knowledge returned HTTP {response.status_code}; "
                    "archived document was retained."
                )
                try:
                    error = response.json().get("error", {})
                except (ValueError, AttributeError):
                    error = {}
                if (
                    isinstance(error, dict)
                    and error.get("code") == "PROVIDER_NOT_CONFIGURED"
                ):
                    for provider in ("rerank", "embedding"):
                        if f"Provider '{provider}'" in str(error.get("message", "")):
                            code = f"EVEROS_{provider.upper()}_NOT_CONFIGURED"
                            message = (
                                f"Knowledge requires a configured {provider} provider; "
                                "the archived document was retained."
                            )
                            break
                raise ImportWorkflowError(code, message, status_code=502)
            target_id = response.json()["data"]["doc_id"]
            await run_in_threadpool(outcome, "delivered", target_id)
        except ImportWorkflowError:
            raise
        except (httpx.HTTPError, KeyError, ValueError):
            await run_in_threadpool(outcome, "delivery_unknown")
            raise ImportWorkflowError(
                "CLAUDE_DELIVERY_OUTCOME_UNKNOWN",
                "Knowledge delivery outcome is unknown; do not blindly resubmit.",
                status_code=502,
            ) from None
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data={"status": "delivered", "target_id": target_id},
        )
    except ImportWorkflowError as exc:
        _raise_api_error(exc)


@router.post("/{batch_id}/documents/{record_id}/reconcile")
async def reconcile_document(
    batch_id: str, record_id: str, request: Request
) -> ApiEnvelope[dict[str, Any]]:
    return await _reconcile_record(batch_id, record_id, request, "documents.jsonl")


@router.post("/{batch_id}/assets/{record_id}/reconcile")
async def reconcile_asset(
    batch_id: str, record_id: str, request: Request
) -> ApiEnvelope[dict[str, Any]]:
    return await _reconcile_record(batch_id, record_id, request, "assets.jsonl")


async def _reconcile_record(
    batch_id: str, record_id: str, request: Request, record_file: str,
) -> ApiEnvelope[dict[str, Any]]:
    """Find a previous upload by its content-addressed source filename and scope."""
    try:
        batch, package = await run_in_threadpool(_load, request, batch_id)
        record = next(
            (
                r
                for r in package.optional_records.get(record_file, ())
                if r.record_id == record_id
            ),
            None,
        )
        if record is None:
            raise ImportWorkflowError(
                "CLAUDE_RECORD_NOT_FOUND", "Document not found.", status_code=404
            )
        source_name = (_document_upload(package, record) if record_file == "documents.jsonl"
                       else _asset_upload(package, record))[0]
        title = record.payload.get("title") or record.payload.get("filename") or "Attachment"
        adapter = request.app.state.everos_adapter
        params = {
            "app_id": batch["app_id"],
            "project_id": batch["project_id"],
            "page_size": 100,
        }
        async with httpx.AsyncClient(
            timeout=adapter.timeout_seconds, transport=adapter.transport
        ) as client:
            for page in range(1, 101):
                response = await client.get(
                    f"{adapter.base_url}/api/v2/knowledge/documents",
                    params={**params, "page": page},
                )
                response.raise_for_status()
                data = response.json()["data"]
                for item in data["documents"]:
                    if item.get("title") != title:
                        continue
                    detail = await client.get(
                        f"{adapter.base_url}/api/v2/knowledge/documents/{item['doc_id']}",
                        params=params,
                    )
                    detail.raise_for_status()
                    if detail.json()["data"].get("source_name") == source_name:

                        def save(target_id: str) -> None:
                            with request.app.state.vault_store.connect() as c:
                                c.execute(
                                    "UPDATE umif_record_map SET "
                                    "status='delivered',target_id=? "
                                    "WHERE batch_id=? AND source_record_id=?",
                                    (target_id, batch_id, record_id),
                                )
                                if record_file == "documents.jsonl":
                                    asset_id = record.payload.get("asset_id")
                                    linked = [r for r in package.optional_records.get("documents.jsonl", ())
                                              if r.payload.get("asset_id") == asset_id]
                                    if asset_id and len(linked) == 1:
                                        c.execute(
                                            "UPDATE umif_record_map SET status='linked_to_document',target_id=? "
                                            "WHERE batch_id=? AND source_record_id=?",
                                            (target_id, batch_id, asset_id),
                                        )

                        await run_in_threadpool(save, item["doc_id"])
                        return ApiEnvelope(
                            request_id=uuid.uuid4().hex,
                            data={"status": "delivered", "target_id": item["doc_id"]},
                        )
                if page * 100 >= data["total"]:
                    break
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data={
                "status": "not_found",
                "message": "No matching document found. The "
                "previous request may still be processing; "
                "archived content is retained.",
            },
        )
    except ImportWorkflowError as exc:
        _raise_api_error(exc)
    except (httpx.HTTPError, KeyError, ValueError):
        _raise_api_error(
            ImportWorkflowError(
                "CLAUDE_RECONCILE_FAILED",
                "Could not reconcile Knowledge delivery.",
                status_code=502,
            )
        )
