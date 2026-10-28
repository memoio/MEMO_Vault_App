"""Owner-only grant lifecycle, Pack approval drafts and gateway controls."""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from everos.infra.memo_vault_gateway import DuplicateGrantError, RepositoryDeniedError
from everos.service.memo_vault_export_contracts import GatewayError, GrantCreate

from ..contracts import PackExportRequest
from ..gateway_contracts import CreateGrantRequest
from ..gateway_runtime import gateway_url, to_pack_sources
from ..memory_pack import MemoryPackError, MemoryPackService


def require_owner(request: Request) -> None:
    security = getattr(request.app.state, "owner_security", None)
    status = security.check(request) if security is not None else 503
    if status:
        raise HTTPException(
            status,
            detail={
                "code": "OWNER_AUTH_REQUIRED",
                "message": "Owner authentication required.",
            },
        )


router = APIRouter(dependencies=[Depends(require_owner)], tags=["vault-authorization"])


@router.get("/grants")
async def list_grants(
    request: Request,
    bucket: Literal["all", "active", "history"] = "all",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    items, total = request.app.state.gateway_repository.list_grant_page(
        bucket=bucket, limit=limit, offset=offset
    )
    return {"data": {"items": items, "total": total, "limit": limit, "offset": offset}}


@router.post("/grants/pack-drafts")
async def pack_draft(body: PackExportRequest, request: Request):
    if body.format == "vault-memory/1" and body.evidence_mode not in {"none", "id"}:
        raise HTTPException(
            422, detail="External fixed Packs support none/id evidence only."
        )
    service = request.app.state.gateway_service
    try:
        sources = await service.load_sources(body.namespaces)
        exported = MemoryPackService().export(body, to_pack_sources(sources))
        selected = [s for s in sources if s.memory_id in exported.included_ids]
        config = {
            "format": body.format,
            "evidence_mode": body.evidence_mode,
            "token_budget": body.token_budget,
        }
        manifest = service.manifest(selected, config, exported.generated_at)
        draft_id = request.app.state.gateway_repository.save_draft(
            manifest,
            {
                "project_ids": sorted(set(body.namespaces)),
                "kinds": sorted(set(body.kinds)),
            },
        )
        return {
            "data": {
                "draft_id": draft_id,
                "included_count": exported.included_count,
                "content_sha256": exported.content_sha256,
            }
        }
    except (GatewayError, MemoryPackError) as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc


@router.post("/grants")
async def create_grant(
    body: CreateGrantRequest,
    request: Request,
    idempotency_key: Annotated[
        str | None, Header(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    ] = None,
):
    config = GrantCreate.model_validate(body.model_dump(exclude={"draft_id"}))
    try:
        grant = await request.app.state.grant_service.create(
            config,
            draft_id=body.draft_id,
            request_key=idempotency_key,
        )
    except DuplicateGrantError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "GRANT_ALREADY_CREATED",
                "grant_id": exc.grant_id,
                "message": str(exc),
            },
        ) from exc
    except GatewayError as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return {"data": grant}


@router.delete("/grants/{grant_id}")
async def revoke(grant_id: str, request: Request):
    request.app.state.gateway_repository.revoke(grant_id)
    return {"data": {"revoked": True}}


@router.delete("/grants/{grant_id}/permanent")
async def delete_grant(grant_id: str, request: Request):
    try:
        request.app.state.gateway_repository.delete_grant(grant_id)
    except RepositoryDeniedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"data": {"deleted": True}}


@router.post("/grants/{grant_id}/rotate")
async def rotate(grant_id: str, request: Request):
    try:
        return {"data": request.app.state.gateway_repository.rotate(grant_id)}
    except RepositoryDeniedError as exc:
        raise HTTPException(
            409, detail="Only active, unused credentials can be rotated."
        ) from exc


@router.get("/grants/{grant_id}/audit")
async def audit(grant_id: str, request: Request):
    return {"data": request.app.state.gateway_repository.audit(grant_id)}


@router.get("/gateway/status")
async def status(request: Request):
    repository = request.app.state.gateway_repository
    health = await request.app.state.everos_adapter.health()
    try:
        async with httpx.AsyncClient(timeout=0.8) as client:
            gateway_response = await client.get(f"{gateway_url()}/health")
        gateway_online = gateway_response.is_success
    except httpx.HTTPError:
        gateway_online = False
    return {
        "data": repository.status()
        | {
            "gateway_url": gateway_url(),
            "everos_online": health.reachable,
            "gateway_online": gateway_online,
        }
    }


@router.post("/gateway/pause")
async def pause(request: Request):
    return {"data": request.app.state.gateway_repository.set_paused(True)}


@router.post("/gateway/resume")
async def resume(request: Request):
    return {"data": request.app.state.gateway_repository.set_paused(False)}
