"""MEMO Plugins endpoints:
- Data Vault: standalone HTML storage (does NOT enter UMIF, candidate extraction, or Knowledge).
- Agent Vault: reuses EverOS UMIF + import_batches + normalized_messages + candidate extraction/review.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from everos.component.utils.datetime import get_utc_now
from everos.umif.package import write_package
from everos.umif.validator import validate_package

from ..contracts import ApiEnvelope
from ..import_contracts import ParsedImport, ParsedMessage
from ..imports import (
    _content_hash,
    _timestamp_ms,
    convert_parsed_markdown,
    validate_project_id,
)
from ..storage import VaultStore
from .imports import start_extraction

router = APIRouter(prefix="/plugin", tags=["memo-vault-plugin"])

AGENT_PLUGIN_PARSER_VERSION = "memo-plugin-agent-v1"


class PluginIngestRequest(BaseModel):
    project_id: str = Field(default="personal", min_length=1, max_length=64)
    payload: dict[str, Any]


def _canonical_snapshot_hash(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k not in {"request_id", "html"}}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _make_record_id(seed: str) -> str:
    num = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % 9_000_000 + 1_000_000
    return f"MEMO-{num}"


def _esc(val: Any) -> str:
    return html.escape(str(val or ""), quote=True)


def _render_block_html(block: dict[str, Any]) -> str:
    btype = block.get("type")
    bid = _esc(block.get("id") or "")
    order = _esc(block.get("order") or "")
    sec = " / ".join(str(s) for s in (block.get("section_path") or []) if s)
    sec_attr = f' data-memo-section="{_esc(sec)}"' if sec else ""
    base_attr = f'id="{bid}" data-memo-order="{order}"{sec_attr}'

    if btype == "heading":
        lvl = min(max(int(block.get("level") or 1), 1), 6)
        return f"<h{lvl} {base_attr}>{_esc(block.get('text'))}</h{lvl}>"
    if btype == "paragraph":
        links = block.get("links") or []
        links_html = ""
        if links:
            items = "".join(
                f'<li><a href="{_esc(l.get("href"))}">{_esc(l.get("text"))}</a></li>'
                for l in links
                if isinstance(l, dict) and l.get("href")
            )
            if items:
                links_html = f'<ul class="memo-links">{items}</ul>'
        return f"<p {base_attr}>{_esc(block.get('text'))}</p>{links_html}"
    if btype == "quote":
        return f"<blockquote {base_attr}>{_esc(block.get('text'))}</blockquote>"
    if btype == "code":
        lang = _esc(block.get("language") or "")
        cls = f' class="language-{lang}"' if lang else ""
        return f"<pre {base_attr}><code{cls}>{_esc(block.get('text'))}</code></pre>"
    if btype == "list":
        tag = "ol" if block.get("ordered") else "ul"
        items = "".join(
            f'<li id="{_esc(it.get("id"))}" data-depth="{int(it.get("depth") or 0)}">{_esc(it.get("text"))}</li>'
            for it in (block.get("items") or [])
            if isinstance(it, dict)
        )
        return f"<{tag} {base_attr}>{items}</{tag}>"
    if btype == "table":
        cap = f"<caption>{_esc(block['caption'])}</caption>" if block.get("caption") else ""
        cols = block.get("columns") or []
        thead = (
            "<thead><tr>" + "".join(f"<th>{_esc(c)}</th>" for c in cols) + "</tr></thead>"
            if cols
            else ""
        )
        rows = "".join(
            f'<tr id="{_esc(r.get("id"))}">'
            + "".join(f"<td>{_esc(c)}</td>" for c in (r.get("cells") or []))
            + "</tr>"
            for r in (block.get("rows") or [])
            if isinstance(r, dict)
        )
        return f"<table {base_attr}>{cap}{thead}<tbody>{rows}</tbody></table>"
    if btype == "card":
        img_obj = block.get("image") if isinstance(block.get("image"), dict) else None
        img_html = (
            f'<img class="memo-card-img" src="{_esc(img_obj.get("src"))}" alt="{_esc(img_obj.get("alt"))}" loading="lazy" referrerpolicy="no-referrer">'
            if img_obj and img_obj.get("src")
            else ""
        )
        title = block.get("title")
        href = block.get("href")
        head = ""
        if title and href:
            head = f'<h3><a href="{_esc(href)}">{_esc(title)}</a></h3>'
        elif title:
            head = f"<h3>{_esc(title)}</h3>"
        fields = "".join(
            f"<dt>{_esc(f.get('label'))}</dt><dd>{_esc(f.get('value'))}</dd>"
            for f in (block.get("fields") or [])
            if isinstance(f, dict) and f.get("label")
        )
        dl = f"<dl>{fields}</dl>" if fields else ""
        txt = f"<p>{_esc(block['text'])}</p>" if block.get("text") else ""
        gid = _esc(block.get("group_id") or "")
        return f'<article {base_attr} class="memo-card" data-memo-group="{gid}">{img_html}{head}{dl}{txt}</article>'
    if btype == "image":
        src = str(block.get("src") or "").strip()
        if not src:
            return ""
        w = f' width="{int(block["width"])}"' if isinstance(block.get("width"), int) and block["width"] > 0 else ""
        h = f' height="{int(block["height"])}"' if isinstance(block.get("height"), int) and block["height"] > 0 else ""
        cap = f"<figcaption>{_esc(block.get('caption'))}</figcaption>" if block.get("caption") else ""
        return (
            f'<figure {base_attr} class="memo-image">'
            f'<img src="{_esc(src)}" alt="{_esc(block.get("alt"))}"{w}{h} loading="lazy" referrerpolicy="no-referrer">'
            f"{cap}</figure>"
        )
    if btype == "raw":
        return f'<pre {base_attr} class="memo-raw">{_esc(block.get("text"))}</pre>'
    return ""


def _ensure_complete_html(payload: dict[str, Any]) -> str:
    provided = payload.get("html")
    if isinstance(provided, str) and provided.strip().lower().startswith("<!doctype html>"):
        return provided
    source = payload.get("source") or {}
    capture = payload.get("capture") or {}
    lang = _esc(source.get("lang") or "zh-CN")
    title = _esc(source.get("title") or source.get("domain") or source.get("url") or "Captured Web Page")
    url = _esc(source.get("url") or "")
    domain = _esc(source.get("domain") or "")
    captured_at = _esc(payload.get("captured_at") or "")
    mode = _esc(capture.get("mode") or "page")
    scope = _esc(capture.get("scope") or "")
    body_blocks = "\n    ".join(
        _render_block_html(b) for b in (payload.get("blocks") or []) if isinstance(b, dict)
    )
    return (
        f"<!DOCTYPE html>\n"
        f'<html lang="{lang}">\n'
        f"<head>\n"
        f'  <meta charset="utf-8">\n'
        f"  <title>{title}</title>\n"
        f'  <base href="{url}">\n'
        f'  <meta name="memo:schema" content="memo-plugins.capture/1">\n'
        f'  <meta name="memo:source-url" content="{url}">\n'
        f'  <meta name="memo:domain" content="{domain}">\n'
        f'  <meta name="memo:captured-at" content="{captured_at}">\n'
        f'  <meta name="memo:capture-mode" content="{mode}">\n'
        f'  <meta name="memo:capture-scope" content="{scope}">\n'
        f"</head>\n"
        f"<body>\n"
        f'  <main data-memo-record-type="web_page">\n'
        f"    {body_blocks}\n"
        f"  </main>\n"
        f"</body>\n"
        f"</html>\n"
    )


def _validate_common(payload: dict[str, Any], expected_type: str) -> tuple[str, dict[str, Any], str, str]:
    if payload.get("schema_version") != "memo-plugins.capture/1":
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SCHEMA_VERSION", "message": "Unsupported schema_version."},
        )
    record_type = payload.get("record_type")
    if record_type != expected_type:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECORD_TYPE_MISMATCH",
                "message": f"Endpoint only accepts record_type={expected_type!r}, got {record_type!r}.",
            },
        )
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id or len(request_id) > 128:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_REQUEST_ID", "message": "A valid request_id is required."},
        )
    source = payload.get("source")
    if not isinstance(source, dict) or not str(source.get("url") or "").strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SOURCE", "message": "source.url is required."},
        )
    source_url = str(source["url"]).strip()
    title = str(
        source.get("title") or source.get("domain") or source.get("platform") or source_url
    ).strip()
    return request_id, source, source_url, title


def _format_data_vault_receipt(
    store: VaultStore,
    row: Any,
    *,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    projects = {p["project_id"]: p["name"] for p in store.list_projects()}
    return {
        "request_id": row["request_id"],
        "record_id": row["record_id"],
        "batch_id": None,
        "target_vault": "data_vault",
        "destination_label": "Data Vault",
        "record_type": "web_page",
        "storage_format": "html",
        "supports_memory_extraction": False,
        "project_id": row["project_id"],
        "project_name": projects.get(row["project_id"], row["project_id"]),
        "title": row["title"],
        "source_url": row["source_url"],
        "unit_count": row["unit_count"],
        "status": "archived",
        "extraction_status": "not_applicable",
        "candidate_count": 0,
        "html_url": f"/api/v1/plugin/data-vault/records/{row['record_id']}/html",
        "idempotent_replay": idempotent_replay,
        "archived_at": row["created_at"],
        "error_code": None,
        "error_message": None,
    }


def _format_agent_vault_receipt(
    store: VaultStore,
    row: Any,
    *,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    projects = {p["project_id"]: p["name"] for p in store.list_projects()}
    with store.connect() as conn:
        batch = conn.execute(
            "SELECT status, extraction_status, archived_at, error_code, error_message FROM import_batches WHERE id = ?",
            (row["batch_id"],),
        ).fetchone()
        cand_count = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE batch_id = ?",
            (row["batch_id"],),
        ).fetchone()[0]
    return {
        "request_id": row["request_id"],
        "record_id": row["record_id"],
        "batch_id": row["batch_id"],
        "target_vault": "agent_vault",
        "destination_label": "Agent Vault",
        "record_type": "agent_conversation",
        "storage_format": "umif",
        "supports_memory_extraction": True,
        "project_id": row["project_id"],
        "project_name": projects.get(row["project_id"], row["project_id"]),
        "title": row["title"],
        "source_url": row["source_url"],
        "unit_count": row["unit_count"],
        "status": batch["status"] if batch else "archived",
        "extraction_status": batch["extraction_status"] if batch else "not_started",
        "candidate_count": cand_count,
        "idempotent_replay": idempotent_replay,
        "archived_at": (batch["archived_at"] if batch and batch["archived_at"] else row["created_at"]),
        "error_code": batch["error_code"] if batch else None,
        "error_message": batch["error_message"] if batch else None,
    }


@router.get("/projects")
async def list_plugin_projects(request: Request) -> ApiEnvelope[dict[str, Any]]:
    """Return available Vault project spaces for the plugin authorization modal."""
    store: VaultStore = request.app.state.vault_store
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data={"projects": store.list_projects()},
    )


@router.post("/data-vault/records", status_code=201)
async def ingest_data_vault(
    body: PluginIngestRequest, request: Request, response: Response
) -> ApiEnvelope[dict[str, Any]]:
    """Persist web_page directly into standalone Data Vault as complete HTML."""
    store: VaultStore = request.app.state.vault_store
    try:
        project_id = validate_project_id(body.project_id)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail={"code": "INVALID_PROJECT_ID", "message": str(exc)}
        ) from exc
    if not store.has_project(project_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "PROJECT_NOT_FOUND", "message": "Target project does not exist."},
        )

    payload = body.payload
    request_id, source, source_url, title = _validate_common(payload, "web_page")
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_PAYLOAD", "message": "blocks must be a non-empty list."},
        )
    snapshot_sha256 = _canonical_snapshot_hash(payload)

    with store.connect() as conn:
        existing_agent = conn.execute(
            "SELECT 1 FROM agent_vault_deliveries WHERE request_id = ?", (request_id,)
        ).fetchone()
        if existing_agent is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "REQUEST_ID_CONFLICT",
                    "message": "request_id was already used in Agent Vault.",
                },
            )
        existing = conn.execute(
            "SELECT * FROM data_vault_records WHERE request_id = ?", (request_id,)
        ).fetchone()
    if existing is not None:
        if existing["project_id"] != project_id or existing["snapshot_sha256"] != snapshot_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "REQUEST_ID_CONFLICT",
                    "message": "request_id was already used for a different snapshot or project.",
                },
            )
        response.status_code = 200
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data=_format_data_vault_receipt(store, existing, idempotent_replay=True),
        )

    html_doc = _ensure_complete_html(payload)
    html_bytes = html_doc.encode("utf-8")
    html_sha256 = hashlib.sha256(html_bytes).hexdigest()
    payload_bytes = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    seed = f"{request_id}:{uuid.uuid4().hex}"
    record_id = _make_record_id(seed)
    record_dir = store.data_vault_dir / record_id
    record_dir.mkdir(parents=True, exist_ok=False)
    html_file = record_dir / "page.html"
    payload_file = record_dir / "payload.json"
    created_at = get_utc_now().isoformat()
    capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
    unit_count = int(capture.get("selected_units") or len(raw_blocks))

    try:
        tmp_html = record_dir / ".page.html.tmp"
        with tmp_html.open("xb") as h:
            h.write(html_bytes)
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp_html, html_file)

        tmp_json = record_dir / ".payload.json.tmp"
        with tmp_json.open("xb") as h:
            h.write(payload_bytes)
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp_json, payload_file)

        rel_html = str(html_file.relative_to(store.data_dir))
        rel_payload = str(payload_file.relative_to(store.data_dir))

        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO data_vault_records
                   (request_id, record_id, project_id, snapshot_sha256, title, source_url,
                    domain, capture_mode, capture_scope, unit_count,
                    html_relpath, html_sha256, html_bytes, payload_relpath, captured_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    record_id,
                    project_id,
                    snapshot_sha256,
                    title,
                    source_url,
                    str(source.get("domain") or ""),
                    str(capture.get("mode") or "page"),
                    str(capture.get("scope") or ""),
                    unit_count,
                    rel_html,
                    html_sha256,
                    len(html_bytes),
                    rel_payload,
                    str(payload.get("captured_at") or created_at),
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM data_vault_records WHERE request_id = ?", (request_id,)
            ).fetchone()
    except Exception:
        import shutil
        shutil.rmtree(record_dir, ignore_errors=True)
        raise

    response.status_code = 201
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data=_format_data_vault_receipt(store, row, idempotent_replay=False),
    )


@router.get("/data-vault/records/{record_id}/html")
async def get_data_vault_html(record_id: str, request: Request) -> HTMLResponse:
    """Return the raw standalone HTML document stored in Data Vault."""
    store: VaultStore = request.app.state.vault_store
    with store.connect() as conn:
        row = conn.execute(
            "SELECT html_relpath, html_sha256 FROM data_vault_records WHERE record_id = ? OR request_id = ?",
            (record_id, record_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Data Vault record not found.")
    target = (store.data_dir / row["html_relpath"]).resolve()
    if not target.is_relative_to(store.data_dir.resolve()) or not target.is_file():
        raise HTTPException(status_code=404, detail="Stored HTML file is missing.")
    content = target.read_text(encoding="utf-8")
    return HTMLResponse(content=content, status_code=200)


@router.post("/agent-vault/records", status_code=201)
async def ingest_agent_vault(
    body: PluginIngestRequest, request: Request, response: Response
) -> ApiEnvelope[dict[str, Any]]:
    """Persist agent_conversation through EverOS's UMIF + import_batches + normalized_messages flow."""
    store: VaultStore = request.app.state.vault_store
    try:
        project_id = validate_project_id(body.project_id)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail={"code": "INVALID_PROJECT_ID", "message": str(exc)}
        ) from exc
    if not store.has_project(project_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "PROJECT_NOT_FOUND", "message": "Target project does not exist."},
        )

    payload = body.payload
    request_id, source, source_url, title = _validate_common(payload, "agent_conversation")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_PAYLOAD", "message": "messages must be a non-empty list."},
        )
    snapshot_sha256 = _canonical_snapshot_hash(payload)

    with store.connect() as conn:
        existing_data = conn.execute(
            "SELECT 1 FROM data_vault_records WHERE request_id = ?", (request_id,)
        ).fetchone()
        if existing_data is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "REQUEST_ID_CONFLICT",
                    "message": "request_id was already used in Data Vault.",
                },
            )
        existing = conn.execute(
            "SELECT * FROM agent_vault_deliveries WHERE request_id = ?", (request_id,)
        ).fetchone()
    if existing is not None:
        if existing["project_id"] != project_id or existing["snapshot_sha256"] != snapshot_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "REQUEST_ID_CONFLICT",
                    "message": "request_id was already used for a different snapshot or project.",
                },
            )
        response.status_code = 200
        return ApiEnvelope(
            request_id=uuid.uuid4().hex,
            data=_format_agent_vault_receipt(store, existing, idempotent_replay=True),
        )

    batch_id = uuid.uuid4().hex
    try:
        ts_ms = _timestamp_ms(payload.get("captured_at"))
    except ValueError:
        ts_ms = None

    conv_id = str(source.get("conversation_id") or f"plugin-conv-{batch_id[:8]}")
    parsed_messages: list[ParsedMessage] = []
    seen_hashes: set[str] = set()
    dup_count = 0
    for idx, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        raw_role = str(item.get("role") or "user").lower()
        role = raw_role if raw_role in {"user", "assistant"} else "user"
        chash = _content_hash(content)
        is_dup = chash in seen_hashes
        seen_hashes.add(chash)
        dup_count += int(is_dup)
        src_id = str(item.get("source_message_id") or item.get("id") or f"m{idx + 1}")
        parsed_messages.append(
            ParsedMessage(
                conversation_id=conv_id,
                ordinal=len(parsed_messages),
                role=role,
                content=content,
                timestamp_ms=ts_ms,
                source_message_id=src_id,
                content_hash=chash,
                duplicate=is_dup,
                memory_input=True,
            )
        )
    if not parsed_messages:
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_PAYLOAD", "message": "No non-empty messages in payload."},
        )

    parsed = ParsedImport(
        messages=tuple(parsed_messages),
        conversation_count=1,
        duplicate_message_count=dup_count,
        parser_version=AGENT_PLUGIN_PARSER_VERSION,
    )
    unit_count = int(
        (payload.get("capture") or {}).get("selected_units") or len(parsed_messages)
    )
    identity = store.get_identity()
    record_id = _make_record_id(batch_id)
    filename = "plugin-conversation.json"
    raw_bytes = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    raw_file_hash = hashlib.sha256(raw_bytes).hexdigest()

    package = convert_parsed_markdown(parsed, project_id, source_identity=batch_id)
    validation = validate_package(package)
    if not validation.valid:
        raise HTTPException(
            status_code=422,
            detail={"code": "UMIF_VALIDATION_FAILED", "message": ", ".join(validation.errors)},
        )

    batch_dir = store.evidence_dir / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    target_file = batch_dir / filename
    umif_dir = batch_dir / "umif"
    archived_at = get_utc_now().isoformat()

    try:
        tmp_file = batch_dir / f".{filename}.tmp"
        with tmp_file.open("xb") as handle:
            handle.write(raw_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_file, target_file)
        write_package(package, umif_dir)

        rel_raw = str(target_file.relative_to(store.data_dir))
        rel_umif = str(umif_dir.relative_to(store.data_dir))

        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO import_batches
                   (id, source_name, source_type, original_filename,
                    file_sha256, file_size, parser_version, namespace,
                    app_id, project_id, user_id, agent_id,
                    status, extraction_status, conversation_count, message_count,
                    duplicate_message_count, raw_relpath, archived_at,
                    umif_package_relpath, umif_package_id, umif_record_count, umif_validation_json)
                   VALUES (?, ?, 'plugin_agent', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived',
                           'not_started', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    title[:120],
                    title[:120],
                    raw_file_hash,
                    len(raw_bytes),
                    AGENT_PLUGIN_PARSER_VERSION,
                    project_id,
                    identity["app_id"],
                    project_id,
                    identity["user_id"],
                    identity["agent_id"],
                    parsed.conversation_count,
                    len(parsed.messages),
                    parsed.duplicate_message_count,
                    rel_raw,
                    archived_at,
                    rel_umif,
                    package.manifest.package_id,
                    validation.record_count,
                    json.dumps({"errors": validation.errors, "warnings": validation.warnings}),
                ),
            )
            conn.execute(
                """INSERT INTO evidence_files
                   (id, batch_id, relative_path, sha256, media_type, original_name)
                   VALUES (?, ?, ?, ?, 'application/json', ?)""",
                (uuid.uuid4().hex, batch_id, rel_raw, raw_file_hash, filename),
            )
            for rec in (*package.principals, *package.sessions, *package.events):
                conn.execute(
                    """INSERT INTO umif_record_map
                       (batch_id, source_record_id, content_sha256, target_type, status)
                       VALUES (?, ?, ?, ?, 'archived')""",
                    (batch_id, rec.record_id, rec.provenance["content_sha256"], rec.record_type),
                )
            for msg in parsed.messages:
                conn.execute(
                    """INSERT INTO normalized_messages
                       (id, batch_id, conversation_id, ordinal, role, content,
                        timestamp_ms, content_sha256, source_message_id, is_duplicate, memory_input)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        uuid.uuid4().hex,
                        batch_id,
                        msg.conversation_id,
                        msg.ordinal,
                        msg.role,
                        msg.content,
                        msg.timestamp_ms,
                        msg.content_hash,
                        msg.source_message_id,
                        int(msg.duplicate),
                    ),
                )
            conn.execute(
                """INSERT INTO agent_vault_deliveries
                   (request_id, record_id, batch_id, project_id, snapshot_sha256,
                    platform, title, source_url, unit_count, payload_relpath, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    record_id,
                    batch_id,
                    project_id,
                    snapshot_sha256,
                    str(source.get("platform") or "chatgpt"),
                    title,
                    source_url,
                    unit_count,
                    rel_raw,
                    archived_at,
                ),
            )
            conn.execute(
                "INSERT INTO import_events (id, batch_id, event_type) VALUES (?, ?, 'archived')",
                (uuid.uuid4().hex, batch_id),
            )
            row = conn.execute(
                "SELECT * FROM agent_vault_deliveries WHERE request_id = ?", (request_id,)
            ).fetchone()
    except Exception:
        import shutil
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    response.status_code = 201
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data=_format_agent_vault_receipt(store, row, idempotent_replay=False),
    )


@router.get("/deliveries")
async def list_deliveries(
    request: Request,
    project_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> ApiEnvelope[dict[str, Any]]:
    """Return combined deliveries from Data Vault and Agent Vault, ordered by created_at DESC."""
    store: VaultStore = request.app.state.vault_store
    with store.connect() as conn:
        if project_id:
            data_rows = conn.execute(
                "SELECT * FROM data_vault_records WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
            agent_rows = conn.execute(
                "SELECT * FROM agent_vault_deliveries WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            data_rows = conn.execute(
                "SELECT * FROM data_vault_records ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            agent_rows = conn.execute(
                "SELECT * FROM agent_vault_deliveries ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    items = [
        *(_format_data_vault_receipt(store, r) for r in data_rows),
        *(_format_agent_vault_receipt(store, r) for r in agent_rows),
    ]
    items.sort(key=lambda x: str(x.get("archived_at") or ""), reverse=True)
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data={"deliveries": items[:limit]},
    )


@router.get("/deliveries/{request_id}")
async def get_delivery(
    request_id: str, request: Request
) -> ApiEnvelope[dict[str, Any]]:
    store: VaultStore = request.app.state.vault_store
    with store.connect() as conn:
        d_row = conn.execute(
            "SELECT * FROM data_vault_records WHERE request_id = ? OR record_id = ?",
            (request_id, request_id),
        ).fetchone()
        if d_row is not None:
            return ApiEnvelope(
                request_id=uuid.uuid4().hex,
                data=_format_data_vault_receipt(store, d_row),
            )
        a_row = conn.execute(
            "SELECT * FROM agent_vault_deliveries WHERE request_id = ? OR record_id = ? OR batch_id = ?",
            (request_id, request_id, request_id),
        ).fetchone()
        if a_row is not None:
            return ApiEnvelope(
                request_id=uuid.uuid4().hex,
                data=_format_agent_vault_receipt(store, a_row),
            )
    raise HTTPException(
        status_code=404,
        detail={"code": "DELIVERY_NOT_FOUND", "message": "Delivery record not found."},
    )


@router.post("/deliveries/{request_id}/extract", status_code=202)
async def extract_delivery(
    request_id: str, request: Request
) -> ApiEnvelope[dict[str, Any]]:
    """Trigger candidate memory extraction for an Agent Vault record only."""
    store: VaultStore = request.app.state.vault_store
    with store.connect() as conn:
        d_row = conn.execute(
            "SELECT 1 FROM data_vault_records WHERE request_id = ? OR record_id = ?",
            (request_id, request_id),
        ).fetchone()
        if d_row is not None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "EXTRACTION_NOT_SUPPORTED_FOR_DATA_VAULT",
                    "message": "Data Vault stores standalone HTML and does not run memory extraction.",
                },
            )
        row = conn.execute(
            "SELECT * FROM agent_vault_deliveries WHERE request_id = ? OR record_id = ? OR batch_id = ?",
            (request_id, request_id, request_id),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "DELIVERY_NOT_FOUND", "message": "Agent Vault record not found."},
        )
    batch_id = row["batch_id"]
    with store.connect() as conn:
        batch = conn.execute(
            "SELECT extraction_status FROM import_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "IMPORT_BATCH_NOT_FOUND", "message": "Linked import batch was not found."},
        )
    if batch["extraction_status"] not in ("running", "queued", "extracted"):
        await start_extraction(batch_id, request)
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data=_format_agent_vault_receipt(store, row),
    )
