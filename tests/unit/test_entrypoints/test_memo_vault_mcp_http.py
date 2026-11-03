import asyncio

import httpx
import pytest

from everos.entrypoints.memo_vault_mcp.http_server import create_http_app
from everos.service.memo_vault_export_contracts import GrantCreate
from tests.unit.test_entrypoints.test_memo_vault_gateway_http import (
    runtime as gateway_runtime_fixture,  # noqa: F401
)


@pytest.mark.asyncio
async def test_remote_mcp_requires_scoped_key_and_rechecks_revocation(
    gateway_runtime_fixture,  # noqa: F811
):
    owner, external, _ = gateway_runtime_fixture
    repo = owner.state.gateway_repository
    grant = repo.create_grant(
        GrantCreate(
            subject="agent:test", purpose="test", project_ids=["personal"]
        ).model_dump(mode="json")
    )
    second = repo.create_grant(
        GrantCreate(
            subject="agent:second",
            purpose="test",
            project_ids=["personal"],
            operations=["get_memory"],
        ).model_dump(mode="json")
    )
    repo.set_paused(False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=external), base_url="http://127.0.0.1"
    ) as gateway:
        app = create_http_app(gateway="http://127.0.0.1", http=gateway)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client,
        ):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            assert (
                await client.post("/mcp", json=body, headers=headers)
            ).status_code == 401
            headers["Authorization"] = "Bearer " + grant["token"]
            response = await client.post("/mcp", json=body, headers=headers)
            assert response.status_code == 200, response.text
            assert "search_memory" in response.text
            call = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_memory",
                    "arguments": {
                        "project_ids": ["personal"],
                        "memory_id": "visible",
                    },
                },
            }
            result = await client.post("/mcp", json=call, headers=headers)
            assert result.status_code == 200 and "Uses Python" in result.text

            async def discover(token):
                return await client.post(
                    "/mcp",
                    json=body,
                    headers=headers | {"Authorization": "Bearer " + token},
                )

            first, other = await asyncio.gather(
                discover(grant["token"]), discover(second["token"])
            )
            assert "search_memory" in first.text
            assert "get_memory" in other.text and "search_memory" not in other.text
            assert (await discover("mv_invalid")).status_code == 403
            repo.revoke(grant["id"])
            assert (
                await client.post("/mcp", json=body, headers=headers)
            ).status_code in (401, 403)
