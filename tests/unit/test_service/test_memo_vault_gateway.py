"""Behavioral coverage of grant policy, delivery and revocation boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from everos.component.utils.datetime import get_utc_now
from everos.infra.memo_vault_gateway import GatewayRepository, RepositoryDeniedError
from everos.service.memo_vault_export_contracts import (
    GatewayError,
    GrantCreate,
    SourceMemory,
)
from everos.service.memo_vault_gateway import GatewayService


def source(memory_id: str = "visible", **changes: object) -> SourceMemory:
    values = dict(
        memory_id=memory_id,
        everos_memory_id=f"core-{memory_id}",
        project_id="personal",
        namespace="personal",
        kind="fact",
        memory_type="atomic_fact",
        revision=1,
        content="Project uses Python",
        timestamp="2026-10-20T00:00:00Z",
        recorded_at="2026-10-20T00:00:00Z",
        confirmed_at="2026-10-20T00:00:00Z",
        external_use="allow",
        valid_until=None,
        expiry_status="valid",
        lifecycle_status="active",
        evidence_kind="human_authored",
        content_sha256=None,
        evidence=(),
        index_available=True,
    )
    return SourceMemory(**(values | changes))


@pytest.fixture
def runtime(tmp_path):
    from everos.entrypoints.memo_vault.storage import VaultStore
    from tests.unit.test_entrypoints.test_api.test_routes import (
        test_memo_vault_exports as pack_fixtures,
    )

    store = VaultStore(tmp_path)
    store.initialize()
    for memory_id, policy in [("visible", "allow"), ("ask", "ask"), ("deny", "deny")]:
        pack_fixtures._seed_confirmed(
            store,
            memory_id=memory_id,
            everos_id=f"core-{memory_id}",
            external_use=policy,
        )
    with store.connect() as db:
        db.execute(
            "UPDATE memories SET confirmed_at='2026-10-20T00:00:00Z', "
            "evidence_kind='human_authored'"
        )
    repo = GatewayRepository(store.database_path)
    repo.initialize()
    repo.set_paused(False)
    records = [
        source(),
        source("ask", external_use="ask", content="secret ask"),
        source("deny", external_use="deny", content="secret deny"),
    ]

    async def load(project_ids):
        return list(records)

    def render(items, config, generated_at):
        text = json.dumps(
            {
                "schema_version": "vault-memory/1",
                "generated_at": generated_at,
                "memories": [
                    {"memory_id": m.memory_id, "text": m.content} for m in items
                ],
            },
            sort_keys=True,
        )
        return text, len(text) * 2, [m.memory_id for m in items]

    service = GatewayService(repo, load, render)
    grant = repo.create_grant(
        GrantCreate(
            subject="agent:test", project_ids=["personal"], purpose="test"
        ).model_dump(mode="json")
    )
    return repo, service, records, grant


@pytest.mark.asyncio
async def test_live_search_never_returns_ask_deny_or_hidden_counts(runtime):
    repo, service, _records, grant = runtime
    result = await service.call(
        grant["token"],
        "search_memory",
        {"project_ids": ["personal"], "query": "Python"},
    )
    assert [m["memory_id"] for m in result["data"]["memories"]] == ["visible"]
    assert "secret" not in json.dumps(result)
    assert "total_count" not in result["data"]
    assert repo.audit(grant["id"])[0]["outcome"] == "admitted"


@pytest.mark.asyncio
async def test_subject_cannot_expand_scope_or_read_ask(runtime):
    _, service, _, grant = runtime
    for operation, body in [
        ("get_memory", {"project_ids": ["personal"], "memory_id": "ask"}),
        ("search_memory", {"project_ids": ["foreign"], "query": "Python"}),
    ]:
        with pytest.raises(GatewayError, match="access denied"):
            await service.call(grant["token"], operation, body)


@pytest.mark.asyncio
async def test_revoke_and_pause_block_both_metadata_and_content(runtime):
    repo, service, _, grant = runtime
    repo.set_paused(True)
    with pytest.raises(GatewayError):
        await service.call(
            grant["token"],
            "search_memory",
            {"project_ids": ["personal"], "query": "Python"},
        )
    repo.set_paused(False)
    repo.revoke(grant["id"])
    with pytest.raises(GatewayError):
        service.capabilities(grant["token"])


@pytest.mark.asyncio
async def test_revocation_during_source_await_cannot_admit_response(runtime):
    repo, service, _, grant = runtime

    async def revoke_while_loading(project_ids):
        repo.revoke(grant["id"])
        return [source()]

    service.load_sources = revoke_while_loading
    with pytest.raises(GatewayError):
        await service.call(
            grant["token"],
            "get_memory",
            {"project_ids": ["personal"], "memory_id": "visible"},
        )
    assert not any(row["outcome"] == "admitted" for row in repo.audit(grant["id"]))


@pytest.mark.asyncio
async def test_evidence_needs_separate_permission(runtime):
    _, service, _, grant = runtime
    with pytest.raises(GatewayError):
        await service.call(
            grant["token"],
            "get_evidence",
            {"project_ids": ["personal"], "memory_id": "visible", "evidence_id": "e"},
        )


def test_secret_is_not_recoverable_and_rotate_invalidates_old_token(runtime):
    repo, service, _, grant = runtime
    assert "token" not in repo.list_grants()[0]
    assert grant["token"] not in repo.database_path.read_bytes().decode("latin1")
    rotated = repo.rotate(grant["id"])
    with pytest.raises(GatewayError):
        service.capabilities(grant["token"])
    assert service.capabilities(rotated["token"])["subject"] == "agent:test"


@pytest.mark.asyncio
async def test_fixed_pack_is_single_use_and_unrelated_change_does_not_stale(runtime):
    repo, service, records, _ = runtime
    pack = service.manifest(
        [records[0]],
        {"format": "vault-memory/1", "evidence_mode": "none", "token_budget": 4000},
        "2026-10-20T00:00:00Z",
    )
    grant = repo.create_grant(
        GrantCreate(
            subject="fixed",
            mode="approved_pack",
            project_ids=["personal"],
            purpose="test",
        ).model_dump(mode="json"),
        manifest=pack,
    )
    records.append(source("unrelated", content="Added later"))
    results = await asyncio.gather(
        *(
            service.call(
                grant["token"], "read_memory_pack", {"pack_id": grant["pack_id"]}
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, GatewayError) for r in results) == 1


@pytest.mark.asyncio
async def test_fixed_pack_selected_change_rejects_all_content(runtime):
    repo, service, records, _ = runtime
    pack = service.manifest(
        [records[0]],
        {"format": "vault-memory/1", "evidence_mode": "none", "token_budget": 4000},
        "2026-10-20T00:00:00Z",
    )
    grant = repo.create_grant(
        GrantCreate(
            subject="fixed",
            mode="approved_pack",
            project_ids=["personal"],
            purpose="test",
        ).model_dump(mode="json"),
        manifest=pack,
    )
    records[0] = records[0].model_copy(update={"content": "Changed", "revision": 2})
    with pytest.raises(GatewayError) as error:
        await service.call(
            grant["token"], "read_memory_pack", {"pack_id": grant["pack_id"]}
        )
    assert error.value.code == "PACK_APPROVAL_STALE"
    assert repo.get_grant(grant["id"])["status"] == "stale"


def test_pause_persists_and_grants_expire(runtime):
    repo, service, _, grant = runtime
    repo.set_paused(True)
    other = GatewayRepository(repo.database_path)
    other.initialize()
    assert other.status()["paused"] is True
    repo.set_paused(False)
    with repo.connect() as db:
        db.execute(
            "UPDATE export_grants SET expires_at = ?",
            ((get_utc_now() - timedelta(hours=1)).isoformat(),),
        )
    with pytest.raises(GatewayError):
        service.capabilities(grant["token"])


@pytest.mark.asyncio
async def test_cursor_binds_query_and_visible_result_versions(runtime):
    _, service, records, grant = runtime
    records.append(source("second", content="Python testing"))
    body = {"project_ids": ["personal"], "query": "Python", "limit": 1}
    result = await service.call(grant["token"], "search_memory", body)
    cursor = result["data"]["next_cursor"]
    assert cursor
    records[0] = records[0].model_copy(update={"content": "Updated Python"})
    with pytest.raises(GatewayError) as error:
        await service.call(grant["token"], "search_memory", body | {"cursor": cursor})
    assert error.value.status_code == 409


def test_same_approved_draft_cannot_create_two_grants(runtime):
    repo, service, records, _ = runtime
    manifest = service.manifest(
        [records[0]],
        {"format": "vault-memory/1", "evidence_mode": "none", "token_budget": 4000},
        "2026-10-20T00:00:00Z",
    )
    draft_id = repo.save_draft(manifest, {"project_ids": ["personal"]})
    config = GrantCreate(
        subject="fixed", mode="approved_pack", project_ids=["personal"], purpose="test"
    ).model_dump(mode="json")
    repo.create_grant(config, manifest=manifest, draft_id=draft_id)
    with pytest.raises(RepositoryDeniedError):
        repo.create_grant(config, manifest=manifest, draft_id=draft_id)
    assert len([g for g in repo.list_grants() if g["mode"] == "approved_pack"]) == 1


@pytest.mark.asyncio
async def test_context_gets_real_member_ids_from_markdown_renderer(runtime):
    _repo, service, _records, grant = runtime
    result = await service.call(
        grant["token"],
        "build_context",
        {"project_ids": ["personal"], "query": "Python", "format": "markdown"},
    )
    assert result["data"]["included_ids"] == ["visible"]


@pytest.mark.asyncio
async def test_rate_and_parallel_limits_are_shared_in_repository(runtime):
    from everos.infra.memo_vault_gateway import RepositoryLimitedError

    repo, service, _records, grant = runtime
    _, first = repo.begin(grant["token"], "get_memory", "api")
    _, second = repo.begin(grant["token"], "get_memory", "mcp")
    with pytest.raises(RepositoryLimitedError):
        repo.begin(grant["token"], "get_memory", "api")
    repo.deny(first, "CANCELLED")
    repo.deny(second, "CANCELLED")
    limited = repo.create_grant(
        GrantCreate(
            subject="rate",
            project_ids=["personal"],
            purpose="test",
            requests_per_minute=1,
        ).model_dump(mode="json")
    )
    await service.call(
        limited["token"],
        "get_memory",
        {"project_ids": ["personal"], "memory_id": "visible"},
    )
    with pytest.raises(GatewayError) as error:
        await service.call(
            limited["token"],
            "get_memory",
            {"project_ids": ["personal"], "memory_id": "visible"},
        )
    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_missing_governance_table_fails_closed(runtime):
    repo, service, _records, grant = runtime
    with repo.connect() as db:
        db.execute("DROP TABLE IF EXISTS memories")
    with pytest.raises(GatewayError):
        await service.call(
            grant["token"],
            "get_memory",
            {"project_ids": ["personal"], "memory_id": "visible"},
        )


@pytest.mark.asyncio
async def test_stale_pack_cannot_reactivate_when_source_recovers(runtime):
    repo, service, records, _ = runtime
    config = {"format": "vault-memory/1", "evidence_mode": "none", "token_budget": 4000}
    manifest = service.manifest([records[0]], config, "2026-10-20T00:00:00Z")
    grant = repo.create_grant(
        GrantCreate(
            subject="fixed",
            mode="approved_pack",
            project_ids=["personal"],
            purpose="test",
        ).model_dump(mode="json"),
        manifest=manifest,
    )
    original = records[0]
    records[0] = original.model_copy(update={"index_available": False})
    with pytest.raises(GatewayError):
        await service.call(
            grant["token"], "read_memory_pack", {"pack_id": grant["pack_id"]}
        )
    records[0] = original
    with pytest.raises(GatewayError):
        await service.call(
            grant["token"], "read_memory_pack", {"pack_id": grant["pack_id"]}
        )
    from everos.infra.memo_vault_gateway import RepositoryDeniedError

    with pytest.raises(RepositoryDeniedError):
        repo.rotate(grant["id"])
