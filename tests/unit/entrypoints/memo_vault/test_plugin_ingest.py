"""Unit tests for MEMO Plugins Data Vault (standalone HTML) and Agent Vault (EverOS UMIF) ingestion."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from everos.entrypoints.memo_vault.app import app


def _web_payload(request_id: str = "req-web-1", title: str = "Understanding Verifiable Data Ownership") -> dict:
    return {
        "schema_version": "memo-plugins.capture/1",
        "request_id": request_id,
        "record_type": "web_page",
        "captured_at": "2026-11-23T02:10:00.000Z",
        "client": {"name": "memo-plugins", "version": "0.1.0"},
        "source": {
            "kind": "web",
            "url": "https://example.com/research/data-ownership",
            "domain": "example.com",
            "title": title,
            "lang": "en",
        },
        "capture": {
            "mode": "page",
            "scope": "当前页已加载内容：1 个标题 · 1 个表格（2 行）",
            "status": "complete",
            "truncated": false if False else False,
            "extraction": "structured",
            "total_units": 3,
            "selected_units": 3,
        },
        "blocks": [
            {"id": "b1", "order": 1, "type": "heading", "level": 1, "text": title},
            {
                "id": "b2",
                "order": 2,
                "type": "table",
                "caption": "Access policy",
                "columns": ["Resource", "Permission"],
                "rows": [
                    {"id": "b2.r1", "cells": ["Profile", "Read"]},
                    {"id": "b2.r2", "cells": ["Documents", "View"]},
                ],
            },
        ],
    }


def _agent_payload(request_id: str = "req-agent-1") -> dict:
    return {
        "schema_version": "memo-plugins.capture/1",
        "request_id": request_id,
        "record_type": "agent_conversation",
        "captured_at": "2026-11-23T02:15:00.000Z",
        "client": {"name": "memo-plugins", "version": "0.1.0"},
        "source": {
            "kind": "ai_platform",
            "platform": "chatgpt",
            "url": "https://chatgpt.com/c/6f1c2d9e-1234-4abc-9def-0123456789ab",
            "title": "Agent memory and ownership",
            "conversation_id": "6f1c2d9e-1234-4abc-9def-0123456789ab",
        },
        "capture": {
            "mode": "conversation",
            "scope": "已自动遍历当前可加载范围，共 2 条",
            "status": "complete",
            "truncated": False,
            "total_units": 2,
            "selected_units": 2,
        },
        "messages": [
            {"id": "m1", "source_message_id": "s1", "order": 1, "role": "user", "content": "Who owns agent memory?"},
            {"id": "m2", "source_message_id": "s2", "order": 2, "role": "assistant", "content": "The user owns and controls it."},
        ],
    }


def test_data_vault_stores_standalone_html_and_isolates_from_umif(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/plugin/data-vault/records",
            headers={"Origin": "chrome-extension://abcdefghijklmnop"},
            json={"project_id": "personal", "payload": _web_payload()},
        )
        assert res.status_code == 201, res.text
        data = res.json()["data"]
        assert data["target_vault"] == "data_vault"
        assert data["destination_label"] == "Data Vault"
        assert data["storage_format"] == "html"
        assert data["supports_memory_extraction"] is False
        assert data["extraction_status"] == "not_applicable"
        record_id = data["record_id"]
        assert record_id.startswith("MEMO-")

        # Check standalone HTML file on disk and via HTML endpoint
        html_path = tmp_path / "vault" / "data_vault" / record_id / "page.html"
        assert html_path.is_file()
        html_text = html_path.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in html_text
        assert "<table>" in html_text or "<table " in html_text
        assert "Access policy" in html_text

        html_res = client.get(f"/api/v1/plugin/data-vault/records/{record_id}/html")
        assert html_res.status_code == 200
        assert "Understanding Verifiable Data Ownership" in html_res.text

        # Ensure Data Vault did NOT write into import_batches or normalized_messages
        store = app.state.vault_store
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM normalized_messages").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0

        # Extraction endpoint must reject Data Vault records
        ext_res = client.post("/api/v1/plugin/deliveries/req-web-1/extract")
        assert ext_res.status_code == 422
        assert ext_res.json()["detail"]["code"] == "EXTRACTION_NOT_SUPPORTED_FOR_DATA_VAULT"

        # Idempotent replay returns 200 with identical record_id
        replay = client.post(
            "/api/v1/plugin/data-vault/records",
            json={"project_id": "personal", "payload": _web_payload()},
        )
        assert replay.status_code == 200
        assert replay.json()["data"]["record_id"] == record_id
        assert replay.json()["data"]["idempotent_replay"] is True

        # Conflict when same request_id is reused with different snapshot
        conflict = client.post(
            "/api/v1/plugin/data-vault/records",
            json={"project_id": "personal", "payload": _web_payload(title="Changed Title")},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "REQUEST_ID_CONFLICT"


def test_agent_vault_uses_everos_umif_and_rejects_cross_vault_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    with TestClient(app) as client:
        # Cross-vault mismatch checks (VLT-03)
        mismatch_1 = client.post(
            "/api/v1/plugin/data-vault/records",
            json={"project_id": "personal", "payload": _agent_payload()},
        )
        assert mismatch_1.status_code == 422
        assert mismatch_1.json()["detail"]["code"] == "RECORD_TYPE_MISMATCH"

        mismatch_2 = client.post(
            "/api/v1/plugin/agent-vault/records",
            json={"project_id": "personal", "payload": _web_payload()},
        )
        assert mismatch_2.status_code == 422
        assert mismatch_2.json()["detail"]["code"] == "RECORD_TYPE_MISMATCH"

        # Valid Agent Vault submission
        res = client.post(
            "/api/v1/plugin/agent-vault/records",
            headers={"Origin": "chrome-extension://abcdefghijklmnop"},
            json={"project_id": "personal", "payload": _agent_payload()},
        )
        assert res.status_code == 201, res.text
        data = res.json()["data"]
        assert data["target_vault"] == "agent_vault"
        assert data["destination_label"] == "Agent Vault"
        assert data["storage_format"] == "umif"
        assert data["supports_memory_extraction"] is True
        assert data["extraction_status"] == "not_started"
        batch_id = data["batch_id"]
        assert batch_id

        # Check EverOS UMIF + SQLite tables
        store = app.state.vault_store
        with store.connect() as conn:
            batch = conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
            assert batch is not None
            assert batch["source_type"] == "plugin_agent"
            msg_count = conn.execute(
                "SELECT COUNT(*) FROM normalized_messages WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            assert msg_count == 2
        assert (tmp_path / "vault" / "evidence" / batch_id / "umif" / "manifest.json").is_file()

        # List deliveries returns the record
        list_res = client.get("/api/v1/plugin/deliveries")
        assert list_res.status_code == 200
        deliveries = list_res.json()["data"]["deliveries"]
        assert len(deliveries) == 1
        assert deliveries[0]["record_id"] == data["record_id"]

        # List projects via plugin endpoint
        proj_res = client.get("/api/v1/plugin/projects")
        assert proj_res.status_code == 200
        projects = proj_res.json()["data"]["projects"]
        assert any(p["project_id"] == "personal" for p in projects)


def test_agent_vault_start_extraction_and_scoped_candidate_review(
    monkeypatch, tmp_path: Path
) -> None:
    import asyncio
    import httpx
    import everos.entrypoints.memo_vault.routes.imports as import_routes

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            pass

        async def extract_import(self, *, batch_id, app_id, project_id, user_id, agent_id, messages):
            first_id = str(messages[0]["id"])
            return {
                "status": "extracted",
                "method": "local",
                "candidate_count": 1,
                "candidate_scope": {"app_id": app_id, "project_id": project_id, "user_id": user_id},
                "candidates": [
                    {
                        "candidate_id": f"cand-{batch_id[:8]}",
                        "kind": "fact",
                        "memory_type": "atomic_fact",
                        "content": "The user discussed ownership of agent memory.",
                        "source_message_ids": [first_id],
                    }
                ],
                "user_id": user_id,
                "agent_id": agent_id,
                "conversation_count": 1,
                "deferred_tool_message_count": 0,
                "unsupported_role_message_count": 0,
            }

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", FakeAdapter)

    async def scenario() -> None:
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            owner_headers = {"Authorization": f"Bearer {app.state.owner_security.secret}"}
            ingest_a = await client.post(
                "/api/v1/plugin/agent-vault/records",
                json={"project_id": "personal", "payload": _agent_payload("req-extract-a")},
            )
            assert ingest_a.status_code == 201, ingest_a.text
            data_a = ingest_a.json()["data"]
            batch_a = data_a["batch_id"]

            ingest_b = await client.post(
                "/api/v1/plugin/agent-vault/records",
                json={"project_id": "personal", "payload": _agent_payload("req-extract-b")},
            )
            assert ingest_b.status_code == 201

            ext = await client.post("/api/v1/plugin/deliveries/req-extract-a/extract")
            assert ext.status_code == 202, ext.text
            assert ext.json()["data"]["extraction_status"] in ("queued", "running", "extracted")

            status_data = {}
            for _ in range(100):
                status_res = await client.get("/api/v1/plugin/deliveries/req-extract-a")
                assert status_res.status_code == 200
                status_data = status_res.json()["data"]
                if status_data["extraction_status"] == "extracted":
                    break
                await asyncio.sleep(0.01)

            assert status_data["extraction_status"] == "extracted"
            assert status_data["candidate_count"] == 1

            # Calling extract again after completion is idempotent and preserves extracted state
            ext_again = await client.post("/api/v1/plugin/deliveries/req-extract-a/extract")
            assert ext_again.status_code == 202
            assert ext_again.json()["data"]["extraction_status"] == "extracted"

            # Scoped candidate query by batch_id returns only candidates for this record
            cand_res = await client.get(
                f"/api/v1/candidates?status=pending&batch_id={batch_a}",
                headers=owner_headers,
            )
            assert cand_res.status_code == 200
            candidates = cand_res.json()["data"]["candidates"]
            assert len(candidates) == 1
            cand = candidates[0]
            assert cand["batch_id"] == batch_a
            assert cand["evidence_status"] == "valid"
            assert cand["evidence"]

            # Querying by batch_b returns 0 candidates
            cand_b_res = await client.get(
                f"/api/v1/candidates?status=pending&batch_id={ingest_b.json()['data']['batch_id']}",
                headers=owner_headers,
            )
            assert cand_b_res.status_code == 200
            assert cand_b_res.json()["data"]["candidates"] == []

            msg_id = cand["evidence"][0]["message_id"]
            src_res = await client.get(
                f"/api/v1/candidates/{cand['id']}/source/{msg_id}",
                headers=owner_headers,
            )
            assert src_res.status_code == 200
            assert len(src_res.json()["data"]["messages"]) == 2

    asyncio.run(scenario())
