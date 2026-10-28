"""Owner-scoped Knowledge export gateway for the Vault console."""

from __future__ import annotations

import re
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..contracts import ApiEnvelope

router = APIRouter(prefix="/knowledge-export", tags=["memo-vault-knowledge-export"])
_DOC_ID = re.compile(r"^d_[a-f0-9]{12,32}$")


def _scope(request: Request, project_id: str) -> dict[str, str]:
    store = request.app.state.vault_store
    if not store.has_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"app_id": store.get_identity()["app_id"], "project_id": project_id}


def _adapter(request: Request):
    return request.app.state.everos_adapter


async def _stream(request: Request, method: str, path: str, *, params=None, json=None):
    adapter = _adapter(request)
    client = httpx.AsyncClient(timeout=adapter.timeout_seconds, transport=adapter.transport)
    try:
        outgoing = client.build_request(method, f"{adapter.base_url}/api/v2/knowledge/{path}",
                                        params=params, json=json)
        response = await client.send(outgoing, stream=True)
        if response.status_code >= 400:
            status = response.status_code
            await response.aclose()
            await client.aclose()
            raise HTTPException(status_code=status, detail="Knowledge export unavailable")

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        headers = {}
        if "content-disposition" in response.headers:
            headers["content-disposition"] = response.headers["content-disposition"]
        return StreamingResponse(body(), media_type=response.headers.get("content-type", "application/octet-stream"), headers=headers)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Knowledge service unavailable") from exc


@router.get("/documents")
async def list_documents(request: Request, project_id: str, page: int = Query(1, ge=1),
                         page_size: int = Query(20, ge=1, le=100), category_id: str | None = None,
                         title_query: str | None = None, source_type: str | None = None,
                         file_type: str | None = None, created_from: str | None = None,
                         created_to: str | None = None):
    scope = _scope(request, project_id)
    adapter = _adapter(request)
    try:
        async with httpx.AsyncClient(timeout=adapter.timeout_seconds, transport=adapter.transport) as client:
            result = await client.get(f"{adapter.base_url}/api/v2/knowledge/documents",
                                      params={**scope, "page": page, "page_size": page_size,
                                              **({"category_id": category_id} if category_id else {}),
                                              **({"title_query": title_query} if title_query else {}),
                                              **({"source_type": source_type} if source_type else {}),
                                              **({"file_type": file_type} if file_type else {}),
                                              **({"created_from": created_from} if created_from else {}),
                                              **({"created_to": created_to} if created_to else {})})
            result.raise_for_status()
        data = result.json()["data"]
        documents = data.get("documents", [])
        ids = [item.get("doc_id") for item in documents if isinstance(item.get("doc_id"), str)]
        reviews = {}
        if ids:
            markers = ",".join("?" for _ in ids)
            with request.app.state.vault_store.connect() as connection:
                rows = connection.execute(
                    "SELECT m.target_id,m.status,r.decision FROM umif_record_map m "
                    "JOIN import_batches b ON b.id=m.batch_id "
                    "LEFT JOIN import_content_reviews r ON r.batch_id=m.batch_id "
                    "AND r.source_record_id=m.source_record_id "
                    f"WHERE b.app_id=? AND b.project_id=? AND m.target_id IN ({markers})",
                    (scope["app_id"], project_id, *ids),
                ).fetchall()
            reviews = {row["target_id"]: {"import_status": row["status"],
                                          "review_status": row["decision"]} for row in rows}
        for item in documents:
            item.update(reviews.get(item["doc_id"], {"import_status": "knowledge",
                                                     "review_status": "external_or_existing"}))
        return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Knowledge list unavailable") from exc


@router.get("/documents/{doc_id}")
async def document_detail(request: Request, project_id: str, doc_id: str):
    if not _DOC_ID.fullmatch(doc_id):
        raise HTTPException(status_code=422, detail="Invalid document ID")
    scope = _scope(request, project_id)
    adapter = _adapter(request)
    try:
        async with httpx.AsyncClient(timeout=adapter.timeout_seconds, transport=adapter.transport) as client:
            result = await client.get(f"{adapter.base_url}/api/v2/knowledge/documents/{doc_id}", params=scope)
            result.raise_for_status()
        data = result.json()["data"]
        available = bool(data.get("original_file_path"))
        data.pop("original_file_path", None)
        data["original_available"] = available
        return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Knowledge detail unavailable") from exc


@router.get("/documents/{doc_id}/original")
async def original(request: Request, project_id: str, doc_id: str):
    if not _DOC_ID.fullmatch(doc_id):
        raise HTTPException(status_code=422, detail="Invalid document ID")
    return await _stream(request, "GET", f"documents/{doc_id}/original",
                         params=_scope(request, project_id))


class ExportSelection(BaseModel):
    project_id: str
    doc_ids: list[str] = Field(min_length=1, max_length=100)
    include_derived: bool = False


@router.post("/zip")
async def archive(request: Request, body: ExportSelection):
    if any(not _DOC_ID.fullmatch(doc_id) for doc_id in body.doc_ids):
        raise HTTPException(status_code=422, detail="Invalid document ID")
    return await _stream(request, "POST", "exports",
                         json={**_scope(request, body.project_id), "doc_ids": body.doc_ids,
                               "include_derived": body.include_derived})
