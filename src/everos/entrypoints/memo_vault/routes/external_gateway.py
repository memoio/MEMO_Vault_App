"""External-only HTTP operations; never expose owner or core API routes here."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from everos.service.memo_vault_export_contracts import GatewayError

from ..gateway_contracts import (
    ContextRequest,
    EvidenceRequest,
    GetRequest,
    SearchRequest,
)

router = APIRouter(prefix="/external", tags=["vault-gateway"])


def token_from(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer mv_") or len(auth) > 256:
        raise HTTPException(
            401, detail={"code": "ACCESS_DENIED", "message": "access denied"}
        )
    return auth[7:]


async def invoke(request: Request, operation: str, body: dict):
    token = token_from(request)
    try:
        transport = "mcp" if request.headers.get("x-vault-client") == "mcp" else "api"
        return await request.app.state.gateway_service.call(
            token, operation, body, transport=transport
        )
    except GatewayError as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc


@router.get("/capabilities")
async def capabilities(request: Request):
    async def healthy() -> bool:
        return (await request.app.state.everos_adapter.health()).reachable

    try:
        transport = "mcp" if request.headers.get("x-vault-client") == "mcp" else "api"
        return await request.app.state.gateway_service.discover(
            token_from(request), healthy, transport=transport
        )
    except GatewayError as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc


@router.post("/memories/search")
async def search(body: SearchRequest, request: Request):
    return await invoke(request, "search_memory", body.model_dump(exclude_none=True))


@router.post("/memories/get")
async def get_memory(body: GetRequest, request: Request):
    return await invoke(request, "get_memory", body.model_dump(exclude_none=True))


@router.post("/evidence/get")
async def get_evidence(body: EvidenceRequest, request: Request):
    return await invoke(request, "get_evidence", body.model_dump(exclude_none=True))


@router.post("/context/build")
async def build_context(body: ContextRequest, request: Request):
    return await invoke(request, "build_context", body.model_dump(exclude_none=True))


@router.get("/packs/{pack_id}")
async def pack_info(pack_id: str, request: Request):
    return await invoke(request, "get_pack_info", {"pack_id": pack_id})


@router.post("/packs/{pack_id}/deliver")
async def deliver(pack_id: str, request: Request):
    result = await invoke(request, "read_memory_pack", {"pack_id": pack_id})
    media_type = (
        "application/json"
        if result["data"]["format"] == "vault-memory/1"
        else "text/markdown"
    )
    return Response(
        result["artifact"],
        media_type=media_type,
        headers={
            "X-Content-SHA256": result["content_sha256"],
            "X-Vault-Receipt": result["receipt_id"],
            "X-Request-ID": result["request_id"],
            "Cache-Control": "no-store",
        },
    )
