from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from everos.entrypoints.memo_vault.adapter import (
    EverOSAdapterError,
    EverOSMemoryRecord,
)
from everos.entrypoints.memo_vault.library import MemoVaultLibraryService
from everos.entrypoints.memo_vault.storage import VaultStore


class LifecycleFailingAdapter:
    def __init__(self, *, memory_id: str, content: str) -> None:
        self.memory_id = memory_id
        self.content = content
        self.lifecycle_calls = 0

    async def get_user_memory(self, **_: str) -> EverOSMemoryRecord:
        return EverOSMemoryRecord(
            memory_id=self.memory_id,
            memory_type="episode",
            content=self.content,
            timestamp="2026-10-27T00:00:00Z",
            metadata={},
        )

    async def apply_memory_lifecycle(self, **_: object) -> dict[str, object]:
        self.lifecycle_calls += 1
        raise EverOSAdapterError(
            "Internal server error", code="INTERNAL_ERROR", status_code=500
        )


class LifecycleUpdatingAdapter(LifecycleFailingAdapter):
    async def apply_memory_lifecycle(self, **payload: object) -> dict[str, object]:
        self.lifecycle_calls += 1
        self.content = str(payload["content"])
        return {
            "memory_id": self.memory_id,
            "lifecycle_status": "active",
            "revision": int(payload["expected_revision"]) + 1,
            "content": self.content,
        }


def _seed_memory(store: VaultStore, *, content: str) -> tuple[str, str]:
    identity = store.get_identity()
    memory_id = "test-memory-id"
    everos_memory_id = "user_test_episode_1"
    batch_id = "test-batch-id"
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO import_batches
               (id, source_name, file_sha256, status, namespace, app_id, project_id,
                user_id, agent_id, raw_relpath)
               VALUES (
                   ?, 'test.txt', ?, 'archived', 'personal', ?, 'personal', ?, ?, ''
               )""",
            (
                batch_id,
                hashlib.sha256(b"test").hexdigest(),
                identity["app_id"],
                identity["user_id"],
                identity["agent_id"],
            ),
        )
        connection.execute(
            """INSERT INTO candidates
               (id, batch_id, everos_scope, everos_memory_id, kind, memory_type,
                content, status, external_use, target_project_id)
               VALUES (
                   ?, ?, 'user', ?, 'episode', 'episode', ?, 'confirmed',
                   'ask', 'personal'
               )""",
            (memory_id, batch_id, everos_memory_id, content),
        )
        connection.execute(
            """INSERT INTO memories
               (id, everos_memory_id, namespace, kind, memory_type, status,
                evidence_kind, external_use, lifecycle_status, confirmed_at)
               VALUES (?, ?, 'personal', 'episode', 'episode', 'confirmed',
                       'human_verified', 'ask', 'active', ?)""",
            (memory_id, everos_memory_id, datetime.now(UTC).isoformat()),
        )
    return memory_id, everos_memory_id


def test_policy_only_correction_does_not_call_everos_lifecycle(tmp_path: Path) -> None:
    store = VaultStore(tmp_path / "vault")
    store.initialize()
    content = "A stable test memory."
    memory_id, everos_memory_id = _seed_memory(store, content=content)
    adapter = LifecycleFailingAdapter(memory_id=everos_memory_id, content=content)

    result = asyncio.run(
        MemoVaultLibraryService(store, adapter).apply_lifecycle(
            project_id="personal",
            memory_id=memory_id,
            action="correct",
            expected_revision=1,
            content=content,
            external_use="allow",
        )
    )

    assert result["external_use"] == "allow"
    assert result["revision"] == 1
    assert adapter.lifecycle_calls == 0
    with store.connect() as connection:
        memory_policy = connection.execute(
            "SELECT external_use FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()[0]
        candidate_policy = connection.execute(
            "SELECT external_use FROM candidates WHERE id = ?", (memory_id,)
        ).fetchone()[0]
    assert memory_policy == candidate_policy == "allow"


def test_content_correction_still_uses_everos_lifecycle(tmp_path: Path) -> None:
    store = VaultStore(tmp_path / "vault")
    store.initialize()
    memory_id, everos_memory_id = _seed_memory(store, content="Original memory.")
    adapter = LifecycleUpdatingAdapter(
        memory_id=everos_memory_id, content="Original memory."
    )

    result = asyncio.run(
        MemoVaultLibraryService(store, adapter).apply_lifecycle(
            project_id="personal",
            memory_id=memory_id,
            action="correct",
            expected_revision=1,
            content="Corrected memory.",
            external_use="deny",
        )
    )

    assert result["content"] == "Corrected memory."
    assert result["external_use"] == "deny"
    assert result["revision"] == 2
    assert adapter.lifecycle_calls == 1
