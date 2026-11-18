from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from everos.entrypoints.memo_vault.adapter import EverOSHealth, EverOSMemoryRecord
from everos.entrypoints.memo_vault.app import app
from everos.entrypoints.memo_vault.routes import exports as export_routes
from everos.entrypoints.memo_vault.routes import system as system_routes
from everos.entrypoints.memo_vault.storage import VaultStore


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.records: dict[str, list[EverOSMemoryRecord]] = {}
        self.offline = False

    async def health(self) -> EverOSHealth:
        return EverOSHealth(reachable=not self.offline, status="offline" if self.offline else "ok")

    async def list_user_memories(self, **scope: str) -> list[EverOSMemoryRecord]:
        self.calls.append(scope)
        if self.offline:
            from everos.entrypoints.memo_vault.adapter import EverOSAdapterError

            raise EverOSAdapterError("EverOS is unavailable", code="EVEROS_UNAVAILABLE", status_code=503)
        return self.records.get(scope["project_id"], [])


def _seed_confirmed(
    store: VaultStore,
    *,
    memory_id: str,
    everos_id: str,
    project_id: str = "personal",
    external_use: str = "allow",
    kind: str = "fact",
    body: str = "Canonical route test body",
    with_evidence: bool = False,
) -> EverOSMemoryRecord:
    identity = store.get_identity()
    batch_id = f"batch-{memory_id}"
    now = datetime.now(UTC).isoformat()
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO import_batches
               (id, source_name, file_sha256, status, namespace, app_id, project_id,
                user_id, agent_id, raw_relpath)
               VALUES (?, ?, ?, 'archived', ?, ?, ?, ?, ?, '')""",
            (
                batch_id,
                "pack-test.txt",
                hashlib.sha256(batch_id.encode()).hexdigest(),
                project_id,
                identity["app_id"],
                project_id,
                identity["user_id"],
                identity["agent_id"],
            ),
        )
        connection.execute(
            """INSERT INTO candidates
               (id, batch_id, everos_scope, everos_memory_id, kind, memory_type,
                content, status, external_use, target_project_id, created_at)
               VALUES (?, ?, 'user', ?, ?, 'atomic_fact', 'candidate body must not export',
                       'confirmed', ?, ?, ?)""",
            (memory_id, batch_id, everos_id, kind, external_use, project_id, now),
        )
        connection.execute(
            """INSERT INTO memories
               (id, everos_memory_id, namespace, kind, memory_type, status,
                evidence_kind, external_use, lifecycle_status, confirmed_at)
               VALUES (?, ?, ?, ?, 'atomic_fact', 'confirmed', 'human_verified', ?, 'active', ?)""",
            (memory_id, everos_id, project_id, kind, external_use, now),
        )
        if with_evidence:
            message_id = f"message-{memory_id}"
            quote = "The user confirmed the canonical route test body."
            connection.execute(
                """INSERT INTO normalized_messages
                   (id, batch_id, conversation_id, ordinal, role, content, content_sha256)
                   VALUES (?, ?, 'conversation-1', 1, 'user', ?, ?)""",
                (message_id, batch_id, quote, hashlib.sha256(quote.encode()).hexdigest()),
            )
            connection.execute(
                """INSERT INTO candidate_evidence
                   (candidate_id, message_id, char_start, char_end, quote, quote_sha256)
                   VALUES (?, ?, 0, ?, ?, ?)""",
                (memory_id, message_id, len(quote), quote, hashlib.sha256(quote.encode()).hexdigest()),
            )
    return EverOSMemoryRecord(everos_id, "atomic_fact", body, now, {})


@pytest.fixture
def pack_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = VaultStore(tmp_path / "vault")
    store.initialize()
    adapter = FakeAdapter()
    app.state.vault_store = store
    app.state.everos_adapter = adapter
    settings = SimpleNamespace(
        everos_base_url="http://everos",
        everos_timeout_seconds=1,
        data_dir=str(store.data_dir),
        max_import_bytes=1024,
        max_archive_unpacked_bytes=1024,
        max_archive_files=10,
        max_archive_compression_ratio=20,
    )
    monkeypatch.setattr(
        export_routes,
        "get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(system_routes, "get_settings", lambda: settings)
    return store, adapter


async def _post(path: str, body: dict[str, object]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://vault.test"
    ) as client:
        return await client.post(path, json=body)


async def _get(path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://vault.test"
    ) as client:
        return await client.get(path)


def test_preview_is_enveloped_and_export_requires_ask_confirmation_without_audit(pack_runtime) -> None:
    store, adapter = pack_runtime
    record = _seed_confirmed(
        store,
        memory_id="ask-memory",
        everos_id="everos-ask",
        external_use="ask",
        body="Canonical ask body",
    )
    adapter.records["personal"] = [record]
    request = {
        "namespaces": ["personal"],
        "kinds": ["fact", "episode", "profile"],
        "format": "markdown",
        "token_budget": 4000,
        "evidence_mode": "id",
        "memory_ids": ["ask-memory"],
    }

    preview_response = asyncio.run(_post("/api/v1/packs/preview", request))

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()["data"]
    assert preview["included_count"] == 1
    assert preview["selected_ask_ids"] == ["ask-memory"]
    assert "Canonical ask body" in preview["rendered_content"]
    export_request = {
        **request,
        "preview_sha256": preview["preview_sha256"],
        "preview_generated_at": preview["generated_at"],
        "confirmed_ask_ids": [],
        "confirm_evidence": False,
        "confirm_unrecoverable_transfer": True,
        "delivery_action": "copy",
    }
    rejected = asyncio.run(_post("/api/v1/packs/export", export_request))
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "PACK_ASK_CONFIRMATION_REQUIRED"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0


def test_export_audits_metadata_only_and_scope_errors_are_generic(pack_runtime) -> None:
    store, adapter = pack_runtime
    record = _seed_confirmed(store, memory_id="allow-memory", everos_id="everos-allow")
    adapter.records["personal"] = [record]
    request = {
        "namespaces": ["personal"],
        "kinds": ["fact"],
        "format": "vault-memory/1",
        "token_budget": 4000,
        "evidence_mode": "id",
        "memory_ids": None,
    }
    preview = asyncio.run(_post("/api/v1/packs/preview", request)).json()["data"]
    export_request = {
        **request,
        "memory_ids": ["allow-memory"],
        "preview_sha256": preview["preview_sha256"],
        "preview_generated_at": preview["generated_at"],
        "confirmed_ask_ids": [],
        "confirm_evidence": False,
        "confirm_unrecoverable_transfer": True,
        "delivery_action": "download",
    }
    exported = asyncio.run(_post("/api/v1/packs/export", export_request))
    assert exported.status_code == 200, exported.text
    data = exported.json()["data"]
    assert data["content_sha256"] == preview["content_sha256"]
    with store.connect() as connection:
        row = connection.execute(
            "SELECT item_ids_json, content_sha256, metadata_json FROM exports"
        ).fetchone()
    assert json.loads(row["item_ids_json"]) == ["allow-memory"]
    assert row["content_sha256"] == data["content_sha256"]
    audit = json.loads(row["metadata_json"])
    assert audit["item_revisions"] == {"allow-memory": 1}
    assert audit["delivery_action"] == "download"
    assert "Canonical route test body" not in row["metadata_json"]

    other_project = store.create_project(name="Other project", parent_project_id="personal")
    adapter.calls.clear()
    cross_scope = asyncio.run(_post("/api/v1/packs/preview", {
        **request,
        "namespaces": [other_project["project_id"]],
        "memory_ids": ["allow-memory"],
    }))
    assert cross_scope.status_code == 422
    assert cross_scope.json()["detail"]["code"] == "PACK_SELECTION_INVALID"
    assert "allow-memory" not in cross_scope.text


def test_pack_includes_selected_space_and_nested_spaces(pack_runtime) -> None:
    store, adapter = pack_runtime
    child = store.create_project(name="Nested", parent_project_id="personal")["project_id"]
    adapter.records[child] = [_seed_confirmed(store, memory_id="nested", everos_id="nested-core", project_id=child)]
    config = {"namespaces": ["personal"], "kinds": ["fact"], "format": "markdown", "evidence_mode": "none", "token_budget": 4000}
    response = asyncio.run(_post("/api/v1/packs/preview", config))
    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    assert preview["included_count"] == 1
    result = asyncio.run(_post("/api/v1/packs/export", config | {
        "memory_ids": ["nested"], "preview_sha256": preview["preview_sha256"],
        "preview_generated_at": preview["generated_at"], "confirmed_ask_ids": [],
        "confirm_unrecoverable_transfer": True, "delivery_action": "copy",
    }))
    assert result.status_code == 200, result.text


def test_unknown_project_is_rejected_before_everos_and_offline_never_exports(pack_runtime) -> None:
    store, adapter = pack_runtime
    unknown = asyncio.run(_post("/api/v1/packs/preview", {
        "namespaces": ["missing-project"], "kinds": ["fact"],
    }))
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
    assert adapter.calls == []

    removed_kind = asyncio.run(_post("/api/v1/packs/preview", {
        "namespaces": ["personal"], "kinds": ["preference"],
    }))
    assert removed_kind.status_code == 422
    assert adapter.calls == []

    record = _seed_confirmed(store, memory_id="offline-memory", everos_id="everos-offline")
    adapter.records["personal"] = [record]
    adapter.offline = True
    failed = asyncio.run(_post("/api/v1/packs/preview", {
        "namespaces": ["personal"], "kinds": ["fact"],
    }))
    assert failed.status_code == 503
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0


def test_export_revalidates_local_policy_after_everos_read(pack_runtime) -> None:
    store, adapter = pack_runtime
    record = _seed_confirmed(store, memory_id="changing-memory", everos_id="everos-changing")
    adapter.records["personal"] = [record]
    request = {
        "namespaces": ["personal"],
        "kinds": ["fact"],
        "format": "markdown",
        "token_budget": 4000,
        "evidence_mode": "id",
        "memory_ids": ["changing-memory"],
    }
    preview = asyncio.run(_post("/api/v1/packs/preview", request)).json()["data"]
    original_list = adapter.list_user_memories

    async def revoke_during_read(**scope: str) -> list[EverOSMemoryRecord]:
        records = await original_list(**scope)
        with store.connect() as connection:
            connection.execute(
                "UPDATE memories SET external_use = 'deny' WHERE id = 'changing-memory'"
            )
        return records

    adapter.list_user_memories = revoke_during_read  # type: ignore[method-assign]
    response = asyncio.run(_post("/api/v1/packs/export", {
        **request,
        "preview_sha256": preview["preview_sha256"],
        "preview_generated_at": preview["generated_at"],
        "confirmed_ask_ids": [],
        "confirm_unrecoverable_transfer": True,
        "delivery_action": "copy",
    }))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PACK_PREVIEW_STALE"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0


def test_quote_evidence_requires_a_separate_confirmation(pack_runtime) -> None:
    store, adapter = pack_runtime
    record = _seed_confirmed(
        store,
        memory_id="evidence-memory",
        everos_id="everos-evidence",
        with_evidence=True,
    )
    adapter.records["personal"] = [record]
    request = {
        "namespaces": ["personal"],
        "kinds": ["fact"],
        "format": "vault-memory/1",
        "token_budget": 4000,
        "evidence_mode": "snippet",
        "memory_ids": ["evidence-memory"],
    }
    preview = asyncio.run(_post("/api/v1/packs/preview", request)).json()["data"]
    response = asyncio.run(_post("/api/v1/packs/export", {
        **request,
        "preview_sha256": preview["preview_sha256"],
        "preview_generated_at": preview["generated_at"],
        "confirmed_ask_ids": [],
        "confirm_evidence": False,
        "confirm_unrecoverable_transfer": True,
        "delivery_action": "download",
    }))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PACK_EVIDENCE_CONFIRMATION_REQUIRED"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0


def test_existing_export_table_migrates_idempotently(tmp_path: Path) -> None:
    data_dir = tmp_path / "legacy-vault"
    data_dir.mkdir()
    database_path = data_dir / "vault.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE exports (
                id TEXT PRIMARY KEY,
                export_type TEXT NOT NULL,
                format TEXT NOT NULL,
                item_ids_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute(
            """INSERT INTO exports
               (id, export_type, format, item_ids_json, content_sha256)
               VALUES ('old-export', 'legacy', 'json', '[]', 'abc')"""
        )

    store = VaultStore(data_dir)
    store.initialize()
    store.initialize()
    with store.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(exports)")}
        default = connection.execute("SELECT metadata_json FROM exports WHERE id = 'old-export'").fetchone()
    assert "metadata_json" in columns
    assert default[0] == "{}"


def test_health_reports_memory_pack_ready_without_changing_adp_status(pack_runtime) -> None:
    response = asyncio.run(_get("/api/v1/system/health"))

    assert response.status_code == 200
    features = response.json()["data"]["features"]
    assert features["memory_pack"] == "ready"
    assert features["adp_export"] == "not_started"
