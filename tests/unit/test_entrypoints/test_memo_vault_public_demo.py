import httpx
import pytest
from fastapi import FastAPI

from everos.entrypoints.memo_vault.auth import OwnerSecurity, install_owner_security


@pytest.mark.asyncio
@pytest.mark.parametrize("setting,enabled", [(None, True), ("1", True), ("0", False)])
async def test_optional_public_demo_keeps_origin_and_csrf_checks(
    tmp_path, monkeypatch, setting, enabled
):
    if setting is None:
        monkeypatch.delenv("MEMO_VAULT_PUBLIC_DEMO", raising=False)
    else:
        monkeypatch.setenv("MEMO_VAULT_PUBLIC_DEMO", setting)
    app = FastAPI()
    app.state.owner_security = OwnerSecurity(
        tmp_path, allowed_origins=["https://vault.example"]
    )
    install_owner_security(app)

    @app.get("/api/v1/example")
    async def read_example():
        return {"ok": True}

    @app.post("/api/v1/example")
    async def write_example():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://vault.example"
    ) as client:
        session = (await client.get("/api/v1/auth/session")).json()
        assert session["authenticated"] is enabled
        assert (await client.get("/api/v1/example")).status_code == (
            200 if enabled else 401
        )
        assert (await client.post("/api/v1/example")).status_code == (
            403 if enabled else 401
        )
        if enabled:
            assert (
                await client.post(
                    "/api/v1/example", headers={"Authorization": "Bearer invalid"}
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/v1/example", headers={"X-Vault-CSRF": session["csrf_token"]}
                )
            ).status_code == 200
            assert (
                await client.get(
                    "/api/v1/example", headers={"Origin": "https://evil.example"}
                )
            ).status_code == 403
