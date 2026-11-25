"""Unit tests for plugin-facing Agent Chat routes (/api/v1/plugin/agent-chat/*)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from everos.entrypoints.memo_vault.agent_chat_service import AgentChatStore
from everos.entrypoints.memo_vault.auth import OwnerSecurity, install_owner_security
from everos.entrypoints.memo_vault.routes.agent_chat import plugin_router, router
from everos.entrypoints.memo_vault.storage import VaultStore


@pytest.mark.asyncio
async def test_plugin_agent_chat_auth_materials_limit_and_cancel(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()

    app = FastAPI()
    app.state.agent_chat_store = store
    app.state.owner_security = OwnerSecurity(
        tmp_path, allowed_origins=["http://vault.test"]
    )
    app.state.agent_chat_tasks = set()
    app.state.agent_chat_task_map = {}
    app.state.agent_chat_stop_requests = set()

    received: list[list[dict]] = []
    block_event = asyncio.Event()
    started_event = asyncio.Event()

    class Runtime:
        async def generate(self, agent, messages):
            received.append(messages)
            if "SLOW_STOP" in messages[-1]["content"]:
                started_event.set()
                await block_event.wait()
            return "回答引用 [M1]"

    app.state.agent_runtime = Runtime()
    app.include_router(router, prefix="/api/v1")
    app.include_router(plugin_router, prefix="/api/v1")
    install_owner_security(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://vault.test"
    ) as client:
        # Owner route stays 401 without cookie/token
        assert (await client.get("/api/v1/agent-chat/models")).status_code == 401
        # Plugin route is accessible
        models_res = await client.get("/api/v1/plugin/agent-chat/models")
        assert models_res.status_code == 200
        assert any(m["id"] == "qwen" for m in models_res.json()["data"]["models"])

        # Create session
        created = await client.post(
            "/api/v1/plugin/agent-chat/sessions", json={"project_id": "personal"}
        )
        assert created.status_code == 201
        sid = created.json()["data"]["id"]

        # 1) Oversized attached materials -> 413 CONTEXT_LIMIT_EXCEEDED without writing messages
        too_big = await client.post(
            f"/api/v1/plugin/agent-chat/sessions/{sid}/messages",
            json={
                "content": "Summarize",
                "model_id": "qwen",
                "agent_id": "default",
                "request_id": "req-big",
                "use_project_context": False,
                "attached_materials": [
                    {
                        "ref_id": "M1",
                        "source_type": "web_page",
                        "title": "Big doc",
                        "content": "a" * 2000,
                    }
                ],
            },
        )
        assert too_big.status_code == 413
        assert too_big.json()["detail"]["code"] == "CONTEXT_LIMIT_EXCEEDED"
        assert store.get_session(sid)["messages"] == []

        # 2) Valid attached materials -> injected with [M1] and stored in context_meta
        ok_res = await client.post(
            f"/api/v1/plugin/agent-chat/sessions/{sid}/messages",
            json={
                "content": "请总结要点",
                "model_id": "qwen",
                "agent_id": "default",
                "request_id": "req-ok",
                "use_project_context": False,
                "attached_materials": [
                    {
                        "ref_id": "M1",
                        "source_type": "web_page",
                        "title": "MEMO Spec",
                        "source_url": "https://example.com/spec",
                        "content": "MEMO Vault supports local-first memory.",
                    }
                ],
            },
        )
        assert ok_res.status_code == 200
        await asyncio.gather(*list(app.state.agent_chat_tasks))
        assert "[M1]" in received[0][-1]["content"]
        assert "MEMO Vault supports local-first memory." in received[0][-1]["content"]
        session = store.get_session(sid)
        assert session["messages"][0]["content"] == "请总结要点"
        assert session["messages"][1]["status"] == "completed"
        assert session["messages"][1]["context_meta"]["attached_materials"][0]["ref_id"] == "M1"

        # 3) Cancel pending generation -> status becomes "stopped" and does not pollute next turn
        slow_res = await client.post(
            f"/api/v1/plugin/agent-chat/sessions/{sid}/messages",
            json={
                "content": "SLOW_STOP test",
                "model_id": "qwen",
                "agent_id": "default",
                "request_id": "req-slow",
                "use_project_context": False,
            },
        )
        assert slow_res.status_code == 200
        mid = slow_res.json()["data"]["messages"][-1]["id"]
        await asyncio.wait_for(started_event.wait(), timeout=2.0)

        cancel_res = await client.post(
            f"/api/v1/plugin/agent-chat/sessions/{sid}/messages/{mid}/cancel"
        )
        assert cancel_res.status_code == 200
        stopped_msg = next(
            m for m in cancel_res.json()["data"]["messages"] if m["id"] == mid
        )
        assert stopped_msg["status"] == "stopped"
        assert any(e["type"] == "generation_stopped" for e in stopped_msg["events"])

        # 4) Upload attachments (markdown, html, docx, and unsupported exe)
        md_res = await client.post(
            "/api/v1/plugin/agent-chat/attachments",
            files={"file": ("notes.md", b"# Hello\nVault attachment test.", "text/markdown")},
        )
        assert md_res.status_code == 200
        assert "Vault attachment test." in md_res.json()["data"]["content"]

        html_res = await client.post(
            "/api/v1/plugin/agent-chat/attachments",
            files={
                "file": (
                    "page.html",
                    b"<html><body><script>bad()</script><h1>Title</h1><p>Body text</p></body></html>",
                    "text/html",
                )
            },
        )
        assert html_res.status_code == 200
        assert "Body text" in html_res.json()["data"]["content"]
        assert "bad()" not in html_res.json()["data"]["content"]

        import io
        import zipfile

        docx_buf = io.BytesIO()
        with zipfile.ZipFile(docx_buf, "w") as zf:
            zf.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Hello from DOCX</w:t></w:r></w:p></w:body></w:document>",
            )
        docx_res = await client.post(
            "/api/v1/plugin/agent-chat/attachments",
            files={"file": ("report.docx", docx_buf.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        assert docx_res.status_code == 200
        assert docx_res.json()["data"]["content"] == "Hello from DOCX"

        bad_res = await client.post(
            "/api/v1/plugin/agent-chat/attachments",
            files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        assert bad_res.status_code == 415


@pytest.mark.asyncio
async def test_plugin_cors_preflight_and_origin_isolation(tmp_path):
    from everos.entrypoints.memo_vault.app import app as vault_app

    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()

    prev_security = getattr(vault_app.state, "owner_security", None)
    prev_chat_store = getattr(vault_app.state, "agent_chat_store", None)
    vault_app.state.owner_security = OwnerSecurity(
        tmp_path, allowed_origins=["https://memo-vault-app-test-gpu.memolabs.net"]
    )
    vault_app.state.agent_chat_store = store
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=vault_app),
            base_url="https://memo-vault-app-test-gpu.memolabs.net",
        ) as client:
            ext_origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
            preflight = await client.options(
                "/api/v1/plugin/agent-chat/models",
                headers={
                    "Origin": ext_origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "x-memo-plugin-client,content-type",
                },
            )
            assert preflight.status_code == 200
            assert preflight.headers.get("access-control-allow-origin") == ext_origin

            get_res = await client.get(
                "/api/v1/plugin/agent-chat/models",
                headers={"Origin": ext_origin, "X-Memo-Plugin-Client": "memo-plugins"},
            )
            assert get_res.status_code == 200
            assert get_res.headers.get("access-control-allow-origin") == ext_origin

            evil_preflight = await client.options(
                "/api/v1/plugin/agent-chat/models",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert evil_preflight.headers.get("access-control-allow-origin") is None
    finally:
        vault_app.state.owner_security = prev_security
        vault_app.state.agent_chat_store = prev_chat_store
