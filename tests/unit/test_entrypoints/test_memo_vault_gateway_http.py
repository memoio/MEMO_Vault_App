"""Real HTTP boundary tests for owner sessions and scoped external access."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from everos.entrypoints.memo_vault.auth import OwnerSecurity, install_owner_security
from everos.entrypoints.memo_vault.external_app import create_external_app
from everos.entrypoints.memo_vault.gateway_runtime import configure_gateway
from everos.entrypoints.memo_vault.routes.grants import router
from everos.entrypoints.memo_vault.storage import VaultStore
from tests.unit.test_entrypoints.test_api.test_routes.test_memo_vault_exports import (
    FakeAdapter,
    _seed_confirmed,
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMO_VAULT_PUBLIC_DEMO", "0")
    store = VaultStore(tmp_path)
    store.initialize()
    adapter = FakeAdapter()
    adapter.records["personal"] = [
        _seed_confirmed(
            store, memory_id="visible", everos_id="core", body="Uses Python"
        ),
        _seed_confirmed(
            store,
            memory_id="ask",
            everos_id="core-ask",
            external_use="ask",
            body="private ask",
        ),
    ]
    owner = FastAPI()
    owner.state.vault_store = store
    owner.state.everos_adapter = adapter
    configure_gateway(owner, store, adapter)
    owner.state.owner_security = OwnerSecurity(
        tmp_path, allowed_origins=["http://vault.test"]
    )
    install_owner_security(owner)
    owner.include_router(router, prefix="/api/v1")
    external = create_external_app(store=store, adapter=adapter)
    return owner, external, adapter


@pytest.mark.asyncio
async def test_owner_requires_session_and_cross_origin_is_rejected(runtime):
    owner, _, _ = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner), base_url="http://vault.test"
    ) as c:
        assert (await c.get("/api/v1/grants")).status_code == 401
        token = owner.state.owner_security.bootstrap_path.read_text().strip()
        assert (
            await c.post(
                "/api/v1/auth/session",
                json={"token": token},
                headers={"Origin": "http://evil.test"},
            )
        ).status_code == 403
        login = await c.post(
            "/api/v1/auth/session",
            json={"token": token},
            headers={"Origin": "http://vault.test"},
        )
        assert login.status_code == 200
        assert (await c.post("/api/v1/gateway/resume")).status_code == 403
        csrf = login.json()["csrf_token"]
        assert (
            await c.post("/api/v1/gateway/resume", headers={"X-Vault-CSRF": csrf})
        ).status_code == 200
        assert (await c.get("/api/v1/grants")).status_code == 200


@pytest.mark.asyncio
async def test_external_scope_revoke_and_route_allowlist(runtime):
    owner, external, _adapter = runtime
    repo = owner.state.gateway_repository
    from everos.service.memo_vault_export_contracts import GrantCreate

    grant = repo.create_grant(
        GrantCreate(
            subject="agent:test", purpose="test", project_ids=["personal"]
        ).model_dump(mode="json")
    )
    repo.set_paused(False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=external), base_url="http://127.0.0.1"
    ) as c:
        body = {"project_ids": ["personal"], "query": "Python"}
        assert (
            await c.post("/api/v1/external/memories/search", json=body)
        ).status_code == 401
        c.headers["Authorization"] = "Bearer " + grant["token"]
        result = await c.post("/api/v1/external/memories/search", json=body)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["memories"][0]["memory_id"] == "visible"
        assert "private ask" not in result.text
        assert (await c.get("/api/v1/grants")).status_code == 404
        assert (
            await c.post(
                "/api/v1/external/memories/search", json=body | {"subject": "someone"}
            )
        ).status_code == 422
        repo.revoke(grant["id"])
        assert (
            await c.post("/api/v1/external/memories/search", json=body)
        ).status_code == 403


@pytest.mark.asyncio
async def test_storage_offline_fails_closed_and_context_reuses_pack(runtime):
    owner, external, adapter = runtime
    from everos.service.memo_vault_export_contracts import GrantCreate

    repo = owner.state.gateway_repository
    grant = repo.create_grant(
        GrantCreate(
            subject="agent:test", purpose="test", project_ids=["personal"]
        ).model_dump(mode="json")
    )
    repo.set_paused(False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=external),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + grant["token"]},
    ) as c:
        result = await c.post(
            "/api/v1/external/context/build",
            json={"project_ids": ["personal"], "query": "Python"},
        )
        assert result.status_code == 200, result.text
        assert result.json()["data"]["pack"]["memories"][0]["text"] == "Uses Python"
        adapter.offline = True
        result = await c.post(
            "/api/v1/external/memories/get",
            json={"project_ids": ["personal"], "memory_id": "visible"},
        )
        assert result.status_code == 403
        assert "Uses Python" not in result.text


@pytest.mark.asyncio
async def test_uninitialized_owner_listener_never_exposes_existing_routes():
    uninitialized = FastAPI()
    install_owner_security(uninitialized)

    @uninitialized.get("/api/v1/memories")
    async def sensitive():
        return {"secret": "not visible"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=uninitialized), base_url="http://127.0.0.1"
    ) as c:
        response = await c.get("/api/v1/memories")
        assert response.status_code == 503
        assert "not visible" not in response.text


@pytest.mark.asyncio
async def test_fixed_pack_owner_approval_ask_and_draft_are_single_use(runtime):
    owner, external, _ = runtime
    owner.include_router(
        __import__(
            "everos.entrypoints.memo_vault.routes.exports", fromlist=["router"]
        ).router,
        prefix="/api/v1",
    )
    token = owner.state.owner_security.bootstrap_path.read_text().strip()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner),
        base_url="http://vault.test",
        headers={"Authorization": "Bearer " + token},
    ) as c:
        config = {
            "namespaces": ["personal"],
            "kinds": ["fact", "episode", "profile"],
            "format": "vault-memory/1",
            "evidence_mode": "none",
            "token_budget": 32000,
            "memory_ids": ["visible", "ask"],
        }
        preview = await c.post("/api/v1/packs/preview", json=config)
        assert preview.status_code == 200
        data = preview.json()["data"]
        delivery = config | {
            "preview_sha256": data["preview_sha256"],
            "preview_generated_at": data["generated_at"],
            "confirmed_ask_ids": [],
            "confirm_unrecoverable_transfer": True,
            "delivery_action": "copy",
        }
        assert (
            await c.post("/api/v1/grants/pack-drafts", json=delivery)
        ).status_code == 422
        draft = await c.post(
            "/api/v1/grants/pack-drafts", json=delivery | {"confirmed_ask_ids": ["ask"]}
        )
        assert draft.status_code == 200, draft.text
        grant_body = {
            "subject": "agent:fixed",
            "purpose": "deliver",
            "mode": "approved_pack",
            "project_ids": ["personal"],
            "token_budget": 32000,
            "evidence_mode": "none",
            "confirm_transfer": True,
            "draft_id": draft.json()["data"]["draft_id"],
        }
        created = await c.post("/api/v1/grants", json=grant_body)
        assert created.status_code == 200, created.text
        repeated = await c.post("/api/v1/grants", json=grant_body)
        assert repeated.status_code == 409
        grant = created.json()["data"]
    owner.state.gateway_repository.set_paused(False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=external),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + grant["token"]},
    ) as c:
        response = await c.post(f"/api/v1/external/packs/{grant['pack_id']}/deliver")
        assert response.status_code == 200, response.text
        assert {m["memory_id"] for m in response.json()["memories"]} == {
            "visible",
            "ask",
        }
        assert (
            await c.post(f"/api/v1/external/packs/{grant['pack_id']}/deliver")
        ).status_code == 403


@pytest.mark.asyncio
async def test_chunked_request_limit_and_unavailable_capabilities(runtime):
    owner, external, adapter = runtime
    from everos.service.memo_vault_export_contracts import GrantCreate

    grant = owner.state.gateway_repository.create_grant(
        GrantCreate(
            subject="test", purpose="test", project_ids=["personal"]
        ).model_dump(mode="json")
    )
    owner.state.gateway_repository.set_paused(False)

    async def chunks():
        yield b'{"query":"'
        yield b"x" * 70000
        yield b'","project_ids":["personal"]}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=external),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + grant["token"]},
    ) as c:
        assert (
            await c.post(
                "/api/v1/external/memories/search",
                content=chunks(),
                headers={"Content-Type": "application/json"},
            )
        ).status_code == 413
        adapter.offline = True
        assert (await c.get("/api/v1/external/capabilities")).status_code == 403


@pytest.mark.asyncio
async def test_discovery_obeys_rate_limit_and_rechecks_revocation_after_health(runtime):
    owner, external, adapter = runtime
    from everos.service.memo_vault_export_contracts import GrantCreate

    repo = owner.state.gateway_repository
    repo.set_paused(False)
    limited = repo.create_grant(
        GrantCreate(
            subject="limited",
            purpose="test",
            project_ids=["personal"],
            requests_per_minute=1,
        ).model_dump(mode="json")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=external), base_url="http://127.0.0.1"
    ) as c:
        headers = {"Authorization": "Bearer " + limited["token"]}
        assert (
            await c.get("/api/v1/external/capabilities", headers=headers)
        ).status_code == 200
        assert (
            await c.get("/api/v1/external/capabilities", headers=headers)
        ).status_code == 429
        race = repo.create_grant(
            GrantCreate(
                subject="race", purpose="test", project_ids=["personal"]
            ).model_dump(mode="json")
        )
        original = adapter.health

        async def health_with_revoke():
            repo.revoke(race["id"])
            return await original()

        adapter.health = health_with_revoke
        response = await c.get(
            "/api/v1/external/capabilities",
            headers={"Authorization": "Bearer " + race["token"]},
        )
        assert response.status_code == 403
        assert "personal" not in response.text


@pytest.mark.asyncio
async def test_gateway_aborts_oversized_upstream_scan_before_reading_all_pages(runtime):
    owner, _, _ = runtime
    from everos.entrypoints.memo_vault.adapter import EverOSHttpAdapter
    from everos.service.memo_vault_export_contracts import GrantCreate

    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            200, json={"data": {"total_count": 5001, "count": 0, "atomic_facts": []}}
        )

    adapter = EverOSHttpAdapter(
        "http://everos", transport=httpx.MockTransport(upstream)
    )
    external = create_external_app(store=owner.state.vault_store, adapter=adapter)
    repo = external.state.gateway_repository
    repo.set_paused(False)
    grant = repo.create_grant(
        GrantCreate(
            subject="bounded", purpose="test", project_ids=["personal"]
        ).model_dump(mode="json")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=external),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + grant["token"]},
    ) as c:
        response = await c.post(
            "/api/v1/external/memories/search",
            json={"project_ids": ["personal"], "query": "Python"},
        )
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "SCAN_LIMIT"
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_owner_creation_retry_cannot_duplicate_or_reveal_credential(runtime):
    owner, _, _ = runtime
    token = owner.state.owner_security.bootstrap_path.read_text().strip()
    body = {
        "subject": "retry",
        "purpose": "test",
        "project_ids": ["personal"],
        "confirm_transfer": True,
        "confirm_future_allow": True,
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Idempotency-Key": "creation_retry_1234567890",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner),
        base_url="http://vault.test",
        headers=headers,
    ) as c:
        first = await c.post("/api/v1/grants", json=body)
        assert first.status_code == 200
        repeated = await c.post("/api/v1/grants", json=body)
        assert repeated.status_code == 409
        assert len(owner.state.gateway_repository.list_grants()) == 1
        assert first.json()["data"]["token"] not in repeated.text
