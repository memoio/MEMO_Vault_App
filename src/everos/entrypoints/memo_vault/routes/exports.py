"""Memory Pack and ADP export API contract placeholders."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..adapter import EverOSHttpAdapter
from ..adp_converter import json_bytes
from ..adp_export import list_traces, prepare, set_consent
from ..config import get_settings
from ..contracts import (
    AdpConsentRequest,
    AdpExportRequest,
    AdpValidateRequest,
    ApiEnvelope,
    PackExportData,
    PackExportRequest,
    PackPreviewData,
    PackPreviewRequest,
)
from ..library import LibraryError, MemoVaultLibraryService
from ..memory_pack import MemoryPackError, MemoryPackService

router = APIRouter(tags=["memo-vault-exports"])


def _library_service(request: Request) -> MemoVaultLibraryService:
    settings = get_settings()
    adapter = getattr(request.app.state, "everos_adapter", None) or EverOSHttpAdapter(
        settings.everos_base_url, timeout_seconds=settings.everos_timeout_seconds
    )
    return MemoVaultLibraryService(request.app.state.vault_store, adapter)


def _raise_http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


async def _load_sources(request: Request, namespaces: list[str]):
    store = request.app.state.vault_store
    # Validate every requested project before the first call to EverOS.
    for project_id in dict.fromkeys(namespaces):
        if not store.has_project(project_id):
            raise _raise_http(
                "PROJECT_NOT_FOUND", "One or more selected projects do not exist.", 404
            )
    service = _library_service(request)
    sources = []
    for project_id in dict.fromkeys(namespaces):
        sources.extend(await service.list_pack_sources(project_id))
    service.revalidate_pack_sources(namespaces, sources)
    return sources


@router.post("/packs/preview", response_model=ApiEnvelope[PackPreviewData])
async def preview_pack(
    body: PackPreviewRequest, request: Request
) -> ApiEnvelope[PackPreviewData]:
    try:
        sources = await _load_sources(request, body.namespaces)
        data = MemoryPackService().preview(body, sources)
    except HTTPException:
        raise
    except LibraryError as exc:
        raise _raise_http(exc.code, str(exc), exc.status_code) from exc
    except MemoryPackError as exc:
        raise _raise_http(exc.code, str(exc), exc.status_code) from exc
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)


@router.post("/packs/export", response_model=ApiEnvelope[PackExportData])
async def export_pack(
    body: PackExportRequest, request: Request
) -> ApiEnvelope[PackExportData]:
    try:
        sources = await _load_sources(request, body.namespaces)
        data = MemoryPackService().export(body, sources)
    except HTTPException:
        raise
    except LibraryError as exc:
        if exc.code == "PACK_SOURCE_CHANGED":
            raise _raise_http(
                "PACK_PREVIEW_STALE",
                "The Pack changed while loading. "
                "Refresh it before copying or downloading.",
                409,
            ) from exc
        raise _raise_http(exc.code, str(exc), exc.status_code) from exc
    except MemoryPackError as exc:
        raise _raise_http(exc.code, str(exc), exc.status_code) from exc

    request.app.state.vault_store.record_export(
        export_type="memory_pack",
        format=data.format,
        item_revisions=data.included_revisions,
        content_sha256=data.content_sha256,
        created_at=datetime.now(UTC).isoformat(),
        namespace_scope=body.namespaces,
        delivery_action=body.delivery_action,
        confirmed_ask_ids=body.confirmed_ask_ids,
        preview_sha256=data.preview_sha256,
    )
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)


@router.get("/adp/traces")
async def list_adp_traces(request: Request):
    data = await run_in_threadpool(list_traces, request.app.state.vault_store)
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)


@router.post("/adp/consent")
async def update_adp_consent(body: AdpConsentRequest, request: Request):
    try:
        await run_in_threadpool(
            set_consent, request.app.state.vault_store, body.trace_ids, body.consent
        )
    except ValueError as exc:
        raise _raise_http("ADP_INVALID_SELECTION", str(exc), 422) from exc
    return ApiEnvelope(
        request_id=uuid.uuid4().hex, data={"updated": len(body.trace_ids)}
    )


@router.post("/adp/validate")
async def validate_adp(body: AdpValidateRequest, request: Request):
    try:
        data = await run_in_threadpool(
            prepare, request.app.state.vault_store, body.trace_ids, body.format
        )
    except ValueError as exc:
        raise _raise_http("ADP_INVALID_SELECTION", str(exc), 422) from exc
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)


@router.post("/adp/export")
async def export_adp(body: AdpExportRequest, request: Request):
    try:
        data = await run_in_threadpool(
            prepare, request.app.state.vault_store, body.trace_ids, body.format
        )
    except ValueError as exc:
        raise _raise_http("ADP_INVALID_SELECTION", str(exc), 422) from exc
    if not data["valid"] or data["preview_sha256"] != body.preview_sha256:
        raise _raise_http("ADP_PREVIEW_STALE", "轨迹或授权已变化，请重新校验。", 409)
    content = b"".join(json_bytes(item["trajectory"]) for item in data["traces"])
    request.app.state.vault_store.record_export(
        export_type=body.format,
        format="atif-v1.7-jsonl" if body.format == "atif" else "paper-adp-jsonl",
        item_revisions={i: 1 for i in body.trace_ids},
        content_sha256=data["preview_sha256"],
        created_at=datetime.now(UTC).isoformat(),
        namespace_scope=[],
        delivery_action="download",
        confirmed_ask_ids=[],
        preview_sha256=body.preview_sha256,
    )
    return Response(
        content,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{body.format}-candidates.jsonl"'
            ),
            "Cache-Control": "no-store",
        },
    )
