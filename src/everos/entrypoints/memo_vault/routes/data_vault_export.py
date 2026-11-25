"""Owner-protected Data Vault (Web pages) browsing, HTML download, delete, and batch ZIP export endpoints."""

from __future__ import annotations

import io
import json
import re
import shutil
import uuid
import zipfile
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from everos.component.utils.datetime import get_utc_now

from ..contracts import ApiEnvelope
from ..storage import VaultStore

router = APIRouter(prefix="/data-vault", tags=["memo-vault-data-vault"])

MAX_ZIP_RECORDS = 100
MAX_ZIP_TOTAL_BYTES = 256 * 1024 * 1024
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class DataVaultZipRequest(BaseModel):
    record_ids: list[str] = Field(min_length=1, max_length=MAX_ZIP_RECORDS)
    include_payload: bool = False


def _resolve_under_data_dir(store: VaultStore, relpath: str) -> Any:
    root = store.data_dir.resolve()
    target = (store.data_dir / relpath).resolve()
    if not target.is_relative_to(root):
        return None
    return target


def _safe_slug(value: str, fallback: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("_", value.strip()).strip("._")
    return cleaned[:64] or fallback


def _format_record(
    store: VaultStore,
    row: Any,
    projects: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    html_path = _resolve_under_data_dir(store, row["html_relpath"])
    html_available = bool(html_path and html_path.is_file())
    proj = projects.get(row["project_id"])
    return {
        "record_id": row["record_id"],
        "request_id": row["request_id"],
        "project_id": row["project_id"],
        "project_name": proj["name"] if proj else row["project_id"],
        "project_path": proj.get("full_path", row["project_id"]) if proj else row["project_id"],
        "title": row["title"],
        "source_url": row["source_url"],
        "domain": row["domain"],
        "capture_mode": row["capture_mode"],
        "capture_scope": row["capture_scope"],
        "unit_count": row["unit_count"],
        "html_bytes": row["html_bytes"],
        "html_sha256": row["html_sha256"],
        "html_available": html_available,
        "captured_at": row["captured_at"],
        "created_at": row["created_at"],
    }


@router.get("/records")
async def list_data_vault_records(
    request: Request,
    project_id: str | None = Query(default=None),
    domain: str | None = Query(default=None),
    capture_mode: str | None = Query(default=None, pattern="^(page|selection)$"),
    query: str | None = Query(default=None),
    created_from: str | None = Query(default=None),
    created_to: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ApiEnvelope[dict[str, Any]]:
    """Paginated listing of archived Web pages in standalone Data Vault."""
    store: VaultStore = request.app.state.vault_store
    clauses: list[str] = []
    params: list[Any] = []

    if project_id and project_id != "all":
        clauses.append("project_id = ?")
        params.append(project_id)
    if domain and domain.strip():
        clauses.append("LOWER(domain) LIKE ?")
        params.append(f"%{domain.strip().lower()}%")
    if capture_mode:
        clauses.append("capture_mode = ?")
        params.append(capture_mode)
    if query and query.strip():
        like = f"%{query.strip().lower()}%"
        clauses.append("(LOWER(title) LIKE ? OR LOWER(source_url) LIKE ? OR LOWER(domain) LIKE ? OR LOWER(record_id) LIKE ?)")
        params.extend([like, like, like, like])
    if created_from and created_from.strip():
        clauses.append("created_at >= ?")
        params.append(created_from.strip())
    if created_to and created_to.strip():
        bound = created_to.strip()
        if len(bound) == 10:
            bound = f"{bound}T23:59:59.999999+00:00"
        clauses.append("created_at <= ?")
        params.append(bound)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    offset = (page - 1) * page_size

    with store.connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM data_vault_records {where}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT * FROM data_vault_records
                {where}
                ORDER BY created_at DESC, record_id DESC
                LIMIT ? OFFSET ?""",
            (*params, page_size, offset),
        ).fetchall()

    projects = {p["project_id"]: p for p in store.list_projects()}
    records = [_format_record(store, r, projects) for r in rows]
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data={
            "records": records,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/records/{record_id}")
async def get_data_vault_record(record_id: str, request: Request) -> ApiEnvelope[dict[str, Any]]:
    """Return metadata and optional structured block summary for a single Data Vault Web record."""
    store: VaultStore = request.app.state.vault_store
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM data_vault_records WHERE record_id = ? OR request_id = ?",
            (record_id, record_id),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "DATA_VAULT_RECORD_NOT_FOUND", "message": "Web archive record not found."},
        )

    projects = {p["project_id"]: p for p in store.list_projects()}
    data = _format_record(store, row, projects)

    block_counts: dict[str, int] = {}
    payload_path = _resolve_under_data_dir(store, row["payload_relpath"])
    if payload_path and payload_path.is_file():
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            for blk in payload.get("blocks") or []:
                if isinstance(blk, dict) and isinstance(blk.get("type"), str):
                    t = blk["type"]
                    block_counts[t] = block_counts.get(t, 0) + 1
        except (OSError, ValueError):
            pass
    data["block_counts"] = block_counts
    return ApiEnvelope(request_id=uuid.uuid4().hex, data=data)


@router.get("/records/{record_id}/html")
async def download_data_vault_html(
    record_id: str,
    request: Request,
    download: int = Query(default=0, ge=0, le=1),
) -> HTMLResponse:
    """Return raw standalone HTML5 snapshot (inline preview or downloadable attachment)."""
    store: VaultStore = request.app.state.vault_store
    with store.connect() as conn:
        row = conn.execute(
            "SELECT record_id, title, html_relpath FROM data_vault_records WHERE record_id = ? OR request_id = ?",
            (record_id, record_id),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "DATA_VAULT_RECORD_NOT_FOUND", "message": "Web archive record not found."},
        )
    target = _resolve_under_data_dir(store, row["html_relpath"])
    if target is None or not target.is_file():
        raise HTTPException(
            status_code=404,
            detail={"code": "DATA_VAULT_HTML_MISSING", "message": "Stored HTML file is missing."},
        )
    content = target.read_text(encoding="utf-8")
    headers: dict[str, str] = {}
    if download == 1:
        filename = f"{_safe_slug(row['record_id'], 'web-archive')}.html"
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return HTMLResponse(content=content, status_code=200, headers=headers)


@router.delete("/records/{record_id}")
async def delete_data_vault_record(record_id: str, request: Request) -> ApiEnvelope[dict[str, Any]]:
    """Delete a single Data Vault Web archive record and its standalone disk directory."""
    store: VaultStore = request.app.state.vault_store
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT record_id, request_id, html_relpath FROM data_vault_records WHERE record_id = ? OR request_id = ?",
            (record_id, record_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "DATA_VAULT_RECORD_NOT_FOUND", "message": "Web archive record not found."},
            )
        conn.execute("DELETE FROM data_vault_records WHERE record_id = ?", (row["record_id"],))

    data_vault_root = store.data_vault_dir.resolve()
    record_dir = (store.data_vault_dir / row["record_id"]).resolve()
    if record_dir.is_relative_to(data_vault_root) and record_dir != data_vault_root and record_dir.exists():
        shutil.rmtree(record_dir, ignore_errors=True)

    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data={"deleted": True, "record_id": row["record_id"]},
    )


@router.post("/records/export-zip")
async def export_data_vault_zip(body: DataVaultZipRequest, request: Request) -> Response:
    """Package selected Data Vault Web HTML snapshots into a downloadable ZIP with manifest.json."""
    store: VaultStore = request.app.state.vault_store
    unique_ids = list(dict.fromkeys(r.strip() for r in body.record_ids if r.strip()))
    if not unique_ids:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_SELECTION", "message": "At least one record_id is required."},
        )

    placeholders = ",".join("?" for _ in unique_ids)
    with store.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM data_vault_records WHERE record_id IN ({placeholders})",
            unique_ids,
        ).fetchall()

    row_by_id = {row["record_id"]: row for row in rows}
    missing_ids = [rid for rid in unique_ids if rid not in row_by_id]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "DATA_VAULT_RECORD_NOT_FOUND",
                "message": f"Records not found: {', '.join(missing_ids[:5])}",
            },
        )

    projects = {p["project_id"]: p for p in store.list_projects()}
    manifest_items: list[dict[str, Any]] = []
    total_bytes = 0
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rid in unique_ids:
            row = row_by_id[rid]
            html_path = _resolve_under_data_dir(store, row["html_relpath"])
            if html_path is None or not html_path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "DATA_VAULT_HTML_MISSING",
                        "message": f"Stored HTML file missing for {rid}.",
                    },
                )
            html_bytes = html_path.read_bytes()
            total_bytes += len(html_bytes)
            if total_bytes > MAX_ZIP_TOTAL_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "EXPORT_SIZE_LIMIT_EXCEEDED", "message": "Selected archives exceed 256 MiB limit."},
                )

            folder = _safe_slug(rid, "record")
            html_arc = f"{folder}/page.html"
            zf.writestr(html_arc, html_bytes)

            payload_arc: str | None = None
            if body.include_payload:
                payload_path = _resolve_under_data_dir(store, row["payload_relpath"])
                if payload_path and payload_path.is_file():
                    p_bytes = payload_path.read_bytes()
                    total_bytes += len(p_bytes)
                    if total_bytes > MAX_ZIP_TOTAL_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail={"code": "EXPORT_SIZE_LIMIT_EXCEEDED", "message": "Selected archives exceed 256 MiB limit."},
                        )
                    payload_arc = f"{folder}/payload.json"
                    zf.writestr(payload_arc, p_bytes)

            proj = projects.get(row["project_id"])
            manifest_items.append(
                {
                    "record_id": rid,
                    "title": row["title"],
                    "source_url": row["source_url"],
                    "domain": row["domain"],
                    "project_id": row["project_id"],
                    "project_name": proj["name"] if proj else row["project_id"],
                    "capture_mode": row["capture_mode"],
                    "unit_count": row["unit_count"],
                    "captured_at": row["captured_at"],
                    "created_at": row["created_at"],
                    "html_file": html_arc,
                    "html_sha256": row["html_sha256"],
                    "payload_file": payload_arc,
                }
            )

        manifest = {
            "schema": "memo-vault.data-vault-export/1",
            "exported_at": get_utc_now().isoformat(),
            "record_count": len(manifest_items),
            "records": manifest_items,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="data-vault-web-export.zip"'},
    )
