"""Owner-scoped Knowledge export gateway for the Vault console."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..contracts import ApiEnvelope

router = APIRouter(prefix="/knowledge-export", tags=["memo-vault-knowledge-export"])
logger = logging.getLogger(__name__)
_JOB_ID = re.compile(r"^[a-f0-9]{32}$")
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


@router.post("/zip/start", status_code=202)
async def start_archive(request: Request, body: ExportSelection):
    if len(set(body.doc_ids)) != len(body.doc_ids) or any(not _DOC_ID.fullmatch(value) for value in body.doc_ids):
        raise HTTPException(status_code=422, detail="Invalid document IDs")
    scope = _scope(request, body.project_id)
    store = request.app.state.vault_store
    job_id = uuid.uuid4().hex
    with store.connect() as c:
        expired = c.execute("SELECT id FROM knowledge_export_jobs WHERE status IN ('completed','failed') AND created_at < datetime('now','-1 day')").fetchall()
        c.execute("DELETE FROM knowledge_export_jobs WHERE status IN ('completed','failed') AND created_at < datetime('now','-1 day')")
    for row in expired:
        for suffix in (".zip", ".part"):
            (store.data_dir / "knowledge-export-jobs" / (row["id"] + suffix)).unlink(missing_ok=True)
    with store.connect() as c:
        c.execute("INSERT INTO knowledge_export_jobs (id,project_id,status) VALUES (?,?,'queued')", (job_id,body.project_id))
    async def run():
        directory = store.data_dir / "knowledge-export-jobs"
        temp = directory / (job_id + ".part")
        final = directory / (job_id + ".zip")
        try:
            async with request.app.state.knowledge_export_semaphore:
                with store.connect() as c:
                    c.execute("UPDATE knowledge_export_jobs SET status='running' WHERE id=?", (job_id,))
                await anyio.Path(directory).mkdir(parents=True, exist_ok=True)
                adapter = request.app.state.everos_adapter
                async with httpx.AsyncClient(timeout=float(os.environ.get("MEMO_VAULT_KNOWLEDGE_EXPORT_TIMEOUT_SECONDS", "7200")), transport=adapter.transport) as client:
                    async with client.stream("POST", f"{adapter.base_url}/api/v2/knowledge/exports", json={**scope, "doc_ids": body.doc_ids, "include_derived": body.include_derived}) as response:
                        response.raise_for_status()
                        size = 0
                        async with await anyio.open_file(temp, "wb") as output:
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > 600 * 1024 * 1024:
                                    raise ValueError("Archive exceeded the size limit")
                                await output.write(chunk)
                await anyio.Path(temp).replace(final)
                with store.connect() as c:
                    c.execute("UPDATE knowledge_export_jobs SET status='completed' WHERE id=?", (job_id,))
        except asyncio.CancelledError:
            await anyio.Path(temp).unlink(missing_ok=True)
            with store.connect() as c:
                c.execute("UPDATE knowledge_export_jobs SET status='failed',error_message='Export interrupted; start again.' WHERE id=?", (job_id,))
            raise
        except Exception:
            logger.exception("knowledge_export_failed job_id=%s", job_id)
            await anyio.Path(temp).unlink(missing_ok=True)
            with store.connect() as c:
                c.execute("UPDATE knowledge_export_jobs SET status='failed',error_message='Knowledge export failed; inspect service logs.' WHERE id=?", (job_id,))
    task = asyncio.create_task(run())
    request.app.state.knowledge_export_tasks.add(task)
    task.add_done_callback(request.app.state.knowledge_export_tasks.discard)
    return ApiEnvelope(request_id=uuid.uuid4().hex, data={"job_id":job_id,"status":"queued"})


@router.get("/zip/jobs/{job_id}")
async def archive_status(request: Request, job_id: str):
    if not _JOB_ID.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="Export job not found")
    with request.app.state.vault_store.connect() as c:
        row = c.execute("SELECT id,status,error_message FROM knowledge_export_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=dict(row))


@router.get("/zip/jobs/{job_id}/download")
async def download_archive(request: Request, job_id: str):
    if not _JOB_ID.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="Export job not found")
    with request.app.state.vault_store.connect() as c:
        row = c.execute("SELECT status FROM knowledge_export_jobs WHERE id=?", (job_id,)).fetchone()
    path = request.app.state.vault_store.data_dir / "knowledge-export-jobs" / (job_id + ".zip")
    if row is None or row["status"] != "completed" or not path.is_file():
        raise HTTPException(status_code=404, detail="Completed export not found")
    return FileResponse(path, filename="knowledge-export.zip", media_type="application/zip")
