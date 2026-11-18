"""Owner-only grant lifecycle, Pack approval drafts and gateway controls."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, ValidationError

from everos.infra.memo_vault_gateway import DuplicateGrantError, RepositoryDeniedError
from everos.service.memo_vault_export_contracts import GatewayError, GrantCreate

from ..contracts import PackExportRequest
from ..gateway_contracts import (
    ContextRequest,
    CreateGrantRequest,
    EvidenceRequest,
    GetRequest,
    SearchRequest,
)
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


def _credential_cipher(request: Request) -> AESGCM:
    security = request.app.state.owner_security
    if security.public_demo:
        raise HTTPException(
            403, detail="Credential storage is unavailable in public demo mode."
        )
    key = hmac.new(
        security.secret.encode(), b"memo-vault/grant-credentials/v1", hashlib.sha256
    ).digest()
    return AESGCM(key)


def _encrypt_credential(request: Request, grant_id: str, token: str) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + _credential_cipher(request).encrypt(
        nonce, token.encode(), grant_id.encode()
    )


@router.post("/grants/{grant_id}/credential")
async def read_grant_credential(grant_id: str, request: Request):
    cipher = _credential_cipher(request)
    ciphertext = request.app.state.gateway_repository.grant_credential(grant_id)
    if ciphertext is None:
        raise HTTPException(404, detail="Credential unavailable; provide or rotate it.")
    try:
        token = cipher.decrypt(
            ciphertext[:12], ciphertext[12:], grant_id.encode()
        ).decode()
    except (InvalidTag, ValueError, UnicodeError) as exc:
        raise HTTPException(
            409, detail="Credential cannot be decrypted; rotate it."
        ) from exc
    return JSONResponse(
        {"data": {"token": token}}, headers={"Cache-Control": "no-store"}
    )


class VerifyGrantTokenRequest(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=512)


@router.post("/grants/{grant_id}/verify-token")
async def verify_grant_token(
    grant_id: str, body: VerifyGrantTokenRequest, request: Request
):
    token = body.token.get_secret_value()
    repository = request.app.state.gateway_repository
    matches = repository.token_matches_grant(grant_id, token)
    if matches and not request.app.state.owner_security.public_demo:
        matches = repository.save_grant_credential(
            grant_id, token, _encrypt_credential(request, grant_id, token)
        )
    return {"data": {"matches": matches}}


class GatewayTestRequest(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=256)
    operation: Literal[
        "search_memory",
        "get_memory",
        "get_evidence",
        "build_context",
        "get_pack_info",
        "read_memory_pack",
    ]
    body: dict[str, Any]


@router.post("/gateway/test")
async def test_gateway(body: GatewayTestRequest):
    """Forward an owner-initiated test to the configured external gateway."""
    routes = {
        "search_memory": ("memories/search", SearchRequest),
        "get_memory": ("memories/get", GetRequest),
        "get_evidence": ("evidence/get", EvidenceRequest),
        "build_context": ("context/build", ContextRequest),
    }
    if body.operation in routes:
        path, schema = routes[body.operation]
        try:
            payload = schema.model_validate(body.body).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise HTTPException(422, detail="Invalid test request.") from exc
        method = "POST"
    else:
        pack_id = body.body.get("pack_id")
        if not isinstance(pack_id, str) or not 1 <= len(pack_id) <= 256:
            raise HTTPException(422, detail="Invalid Pack ID.")
        path = f"packs/{quote(pack_id, safe='')}"
        method = "GET" if body.operation == "get_pack_info" else "POST"
        if body.operation == "read_memory_pack":
            path += "/deliver"
        payload = {}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.request(
                method,
                f"{gateway_url()}/api/v1/external/{path}",
                headers={"Authorization": f"Bearer {body.token.get_secret_value().strip()}"},
                **({"json": payload} if method == "POST" else {}),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, detail="External gateway unavailable.") from exc
    try:
        result = response.json()
    except ValueError:
        result = response.text[:20000]
    return JSONResponse(
        {
            "data": {
                "status": response.status_code,
                "ok": response.is_success,
                "receipt": response.headers.get("X-Vault-Receipt"),
                "result": result,
            }
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/grants/pack-drafts")
async def pack_draft(body: PackExportRequest, request: Request):
    from .exports import expand_pack_request

    if body.format == "vault-memory/1" and body.evidence_mode not in {"none", "id"}:
        raise HTTPException(
            422, detail="External fixed Packs support none/id evidence only."
        )
    service = request.app.state.gateway_service
    try:
        body = expand_pack_request(request, body)
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
                "project_ids": sorted(set(body.namespaces)),
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
            encrypt_token=(
                None
                if request.app.state.owner_security.public_demo
                else lambda grant_id, token: _encrypt_credential(
                    request, grant_id, token
                )
            ),
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
        encrypt = (
            None
            if request.app.state.owner_security.public_demo
            else (lambda item_id, token: _encrypt_credential(request, item_id, token))
        )
        return {
            "data": request.app.state.gateway_repository.rotate(
                grant_id, encrypt_token=encrypt
            )
        }
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
