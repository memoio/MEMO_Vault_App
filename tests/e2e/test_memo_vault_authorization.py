"""Synthetic end-to-end acceptance for the browser and a real stdio MCP client.

Run with: pytest tests/e2e/test_memo_vault_authorization.py -m slow -q
Requires optional playwright (with Chromium), mcp, and UI node_modules.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from everos.entrypoints.memo_vault.auth import OwnerSecurity, install_owner_security
from everos.entrypoints.memo_vault.external_app import create_external_app
from everos.entrypoints.memo_vault.gateway_runtime import configure_gateway
from everos.entrypoints.memo_vault.routes import (
    exports,
    grants,
    memories,
    projects,
    system,
)
from everos.entrypoints.memo_vault.storage import VaultStore
from everos.service.memo_vault_export_contracts import GrantCreate
from tests.unit.test_entrypoints.test_api.test_routes.test_memo_vault_exports import (
    FakeAdapter,
    _seed_confirmed,
)

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def serve(app, port):
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("Acceptance server failed to start")
        time.sleep(0.025)
    else:
        raise RuntimeError("Acceptance server startup timeout")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    owner_port, gateway_port, ui_port = free_port(), free_port(), free_port()
    ui_url = f"http://127.0.0.1:{ui_port}"
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    monkeypatch.setenv("MEMO_VAULT_UI_ORIGINS", ui_url)
    monkeypatch.setenv("MEMO_VAULT_GATEWAY_URL", gateway_url)
    store = VaultStore(tmp_path)
    store.initialize()
    adapter = FakeAdapter()
    adapter.records["personal"] = [
        _seed_confirmed(
            store,
            memory_id="visible",
            everos_id="core-visible",
            body="Python project approved memory",
        ),
        _seed_confirmed(
            store,
            memory_id="ask",
            everos_id="core-ask",
            body="Hidden ask content",
            external_use="ask",
        ),
    ]
    owner = FastAPI()
    owner.state.vault_store, owner.state.everos_adapter = store, adapter
    configure_gateway(owner, store, adapter)
    owner.state.owner_security = OwnerSecurity(tmp_path, allowed_origins=[ui_url])
    for router in (
        grants.router,
        projects.router,
        memories.router,
        exports.router,
        system.router,
    ):
        owner.include_router(router, prefix="/api/v1")
    install_owner_security(owner)
    external = create_external_app(store=store, adapter=adapter)
    with serve(owner, owner_port), serve(external, gateway_port):
        yield owner, gateway_url, ui_url, owner_port, store


def test_browser_create_read_and_revoke(stack, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    owner, gateway_url, ui_url, owner_port, store = stack
    env = dict(os.environ, MEMO_VAULT_UI_API_URL=f"http://127.0.0.1:{owner_port}")
    with (tmp_path / "vite.log").open("w") as log:
        process = subprocess.Popen(
            [
                "npm",
                "run",
                "dev",
                "--",
                "--port",
                ui_url.rsplit(":", 1)[1],
                "--strictPort",
            ],
            cwd=ROOT / "apps/memo-vault-ui",
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(200):
                try:
                    if httpx.get(ui_url).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("UI did not start")
            with playwright.sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1050})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("dialog", lambda dialog: dialog.accept())
                page.goto(ui_url)
                page.get_by_label("本地管理凭证").fill(
                    owner.state.owner_security.bootstrap_path.read_text().strip()
                )
                page.get_by_role("button", name="连接", exact=True).click()
                page.get_by_role("button", name="授权与 MCP", exact=True).click()
                page.get_by_role("button", name="+ 新建授权", exact=True).click()
                dialog = page.get_by_role("dialog")
                dialog.get_by_label("主体", exact=True).fill("agent:browser-test")
                dialog.get_by_label("用途", exact=True).fill("Acceptance test")
                dialog.get_by_role(
                    "checkbox", name=store.list_projects()[0]["name"], exact=True
                ).check()
                dialog.get_by_role("checkbox", name="我理解实时授权包含").check()
                dialog.get_by_role("checkbox", name="我确认授权范围").check()
                dialog.get_by_role("button", name="确认并创建").click()
                reveal = page.locator(".secret-reveal")
                reveal.wait_for(timeout=5000)
                token = reveal.locator("code").inner_text()
                assert token.startswith("mv_")
                page.get_by_role("button", name="启用外部访问", exact=True).click()
                page.get_by_label("外部访问凭证", exact=True).fill(token)
                page.get_by_label("关键词", exact=True).fill("Python")
                page.route(
                    f"{gateway_url}/api/v1/external/**",
                    lambda route: route.abort("failed"),
                )
                page.get_by_role("button", name="发起调用", exact=True).click()
                page.locator(".connection-error").wait_for(timeout=5000)
                assert "没有收到外部网关响应" in page.locator(
                    ".connection-error"
                ).inner_text()
                page.unroute(f"{gateway_url}/api/v1/external/**")
                page.get_by_role("button", name="发起调用", exact=True).click()
                page.get_by_role("status").filter(
                    has_text="Python project approved memory"
                ).wait_for()
                assert (
                    "Hidden ask content"
                    not in page.locator(".connection-panel").inner_text()
                )
                page.get_by_role("tab", name="API 调用", exact=True).click()
                assert (
                    "/context/build"
                    in page.locator(".integration-panel pre").inner_text()
                )
                assert token not in page.locator(".integration-panel pre").inner_text()
                page.get_by_role("button", name="吊销", exact=True).click()
                page.get_by_text("已吊销", exact=True).wait_for()
                response = httpx.post(
                    gateway_url + "/api/v1/external/memories/get",
                    headers={"Authorization": "Bearer " + token},
                    json={"project_ids": ["personal"], "memory_id": "visible"},
                )
                assert response.status_code == 403
                assert not errors, errors
                page.get_by_role("button", name="导出中心", exact=True).click()
                page.get_by_role(
                    "button", name="通过 API / MCP 授权", exact=True
                ).click()
                pack_dialog = page.get_by_role("dialog")
                pack_dialog.get_by_role("checkbox", name="我了解交付后的内容").check()
                pack_dialog.get_by_role(
                    "button", name="通过 API / MCP 授权", exact=True
                ).click()
                fixed_dialog = page.get_by_role("dialog")
                fixed_dialog.get_by_label("主体", exact=True).fill(
                    "agent:fixed-browser"
                )
                fixed_dialog.get_by_label("用途", exact=True).fill(
                    "Fixed Pack acceptance"
                )
                fixed_dialog.get_by_role("checkbox", name="我确认授权范围").check()
                fixed_dialog.get_by_role("button", name="确认并创建").click()
                page.locator(".secret-reveal").get_by_role(
                    "button", name="连接测试", exact=True
                ).click()
                page.get_by_label("工具", exact=True).select_option("read_memory_pack")
                page.get_by_role("button", name="发起调用", exact=True).click()
                page.get_by_text("已交付", exact=True).wait_for()
                assert not errors, errors
                page.set_viewport_size({"width": 430, "height": 930})
                assert (
                    page.locator(".authorization-cards").evaluate(
                        "(e) => getComputedStyle(e).gridTemplateColumns"
                        '.split(" ").length'
                    )
                    == 1
                )
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.asyncio
async def test_actual_stdio_mcp_discovery_read_and_revoke(stack):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    owner, gateway_url, _, _, _ = stack
    repository = owner.state.gateway_repository
    repository.set_paused(False)
    grant = repository.create_grant(
        GrantCreate(
            subject="agent:stdio", purpose="Acceptance", project_ids=["personal"]
        ).model_dump(mode="json")
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "everos.entrypoints.cli.main",
            "vault-mcp",
            "--gateway",
            gateway_url,
        ],
        env={"MEMO_VAULT_TOKEN": grant["token"]},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {
            "search_memory",
            "get_memory",
            "build_context",
        }
        result = await client.call_tool(
            "get_memory", {"project_ids": ["personal"], "memory_id": "visible"}
        )
        assert not result.isError
        assert (
            result.structuredContent["data"]["text"] == "Python project approved memory"
        )
        repository.revoke(grant["id"])
        denied = await client.call_tool(
            "get_memory", {"project_ids": ["personal"], "memory_id": "visible"}
        )
        assert denied.isError
