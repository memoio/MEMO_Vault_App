"""MCP protocol tests against a real scoped HTTP gateway via ASGI transport."""

from __future__ import annotations

import anyio
import httpx
import pytest

pytest.importorskip("mcp")
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from everos.entrypoints.memo_vault.external_app import create_external_app
from everos.entrypoints.memo_vault.storage import VaultStore
from everos.entrypoints.memo_vault_mcp import GatewayMcpClient, create_server
from everos.service.memo_vault_export_contracts import GrantCreate
from tests.unit.test_entrypoints.test_api.test_routes.test_memo_vault_exports import (
    FakeAdapter,
    _seed_confirmed,
)


@pytest.mark.asyncio
async def test_real_mcp_tool_discovery_read_and_revocation(tmp_path):
    store = VaultStore(tmp_path)
    store.initialize()
    adapter = FakeAdapter()
    adapter.records["personal"] = [
        _seed_confirmed(
            store,
            memory_id="visible",
            everos_id="core-visible",
            body="MCP Python memory",
        )
    ]
    app = create_external_app(store=store, adapter=adapter)
    repo = app.state.gateway_repository
    grant = repo.create_grant(
        GrantCreate(
            subject="mcp:test",
            purpose="protocol",
            project_ids=["personal"],
            operations=["search_memory", "get_memory"],
        ).model_dump(mode="json")
    )
    repo.set_paused(False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as http:
        server = create_server(
            GatewayMcpClient("http://127.0.0.1", grant["token"], http=http)
        )
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            anyio.create_task_group() as tasks,
        ):

            async def serve():
                await server.run(
                    *server_streams, server.create_initialization_options()
                )

            tasks.start_soon(serve)
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {t.name for t in tools.tools} == {"search_memory", "get_memory"}
                get_schema = next(
                    t.inputSchema for t in tools.tools if t.name == "get_memory"
                )
                assert get_schema["properties"]["project_ids"]["items"]["enum"] == [
                    "personal"
                ]
                result = await client.call_tool(
                    "get_memory", {"project_ids": ["personal"], "memory_id": "visible"}
                )
                assert not result.isError
                assert result.structuredContent["data"]["text"] == "MCP Python memory"
                assert grant["token"] not in str(result)
                illegal = await client.call_tool(
                    "get_memory",
                    {
                        "project_ids": ["personal"],
                        "memory_id": "visible",
                        "token": "fake",
                    },
                )
                assert illegal.isError
                repo.revoke(grant["id"])
                denied = await client.call_tool(
                    "get_memory", {"project_ids": ["personal"], "memory_id": "visible"}
                )
                assert denied.isError and "MCP Python memory" not in str(denied)
            tasks.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_fixed_pack_is_discoverable_and_deliverable_from_token_only(tmp_path):
    store = VaultStore(tmp_path)
    store.initialize()
    adapter = FakeAdapter()
    adapter.records["personal"] = [
        _seed_confirmed(
            store,
            memory_id="fixed-memory",
            everos_id="core-fixed",
            body="Fixed MCP payload",
        )
    ]
    app = create_external_app(store=store, adapter=adapter)
    service = app.state.gateway_service
    sources = await service.load_sources(["personal"])
    manifest = service.manifest(
        sources,
        {"format": "vault-memory/1", "evidence_mode": "none", "token_budget": 32000},
        "2026-10-20T00:00:00Z",
    )
    repo = app.state.gateway_repository
    grant = repo.create_grant(
        GrantCreate(
            subject="fixed",
            mode="approved_pack",
            purpose="test",
            project_ids=["personal"],
        ).model_dump(mode="json"),
        manifest=manifest,
    )
    repo.set_paused(False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as http:
        server = create_server(
            GatewayMcpClient("http://127.0.0.1", grant["token"], http=http)
        )
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            anyio.create_task_group() as tasks,
        ):

            async def serve():
                await server.run(
                    *server_streams, server.create_initialization_options()
                )

            tasks.start_soon(serve)
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                tools = await client.list_tools()
                tool = next(t for t in tools.tools if t.name == "read_memory_pack")
                discovered_id = tool.inputSchema["properties"]["pack_id"]["enum"][0]
                result = await client.call_tool(
                    "read_memory_pack", {"pack_id": discovered_id}
                )
                assert not result.isError
                assert (
                    result.structuredContent["data"]["pack"]["memories"][0]["text"]
                    == "Fixed MCP payload"
                )
                repeated = await client.call_tool(
                    "read_memory_pack", {"pack_id": discovered_id}
                )
                assert repeated.isError
            tasks.cancel_scope.cancel()
