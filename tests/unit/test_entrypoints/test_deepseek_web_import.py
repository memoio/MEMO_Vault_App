"""DeepSeek web archives share the existing import and review pipeline."""

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from everos.entrypoints.memo_vault.imports import ImportService, ImportWorkflowError
from everos.entrypoints.memo_vault.storage import VaultStore


def archive(mapping):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("user.json", json.dumps({"user_id": "account"}))
        z.writestr(
            "conversations.json",
            json.dumps(
                [{"id": "conversation", "title": "Example", "mapping": mapping}]
            ),
        )
    return output.getvalue()


def node(parent, children, fragments):
    return {
        "parent": parent,
        "children": children,
        "message": {
            "inserted_at": "2026-10-28T12:00:00Z",
            "model": "deepseek-chat",
            "fragments": fragments,
        },
    }


def test_web_zip_preview_commit_routes_visible_fragments(tmp_path):
    store = VaultStore(tmp_path)
    store.initialize()
    service = ImportService(store)
    raw = archive(
        {
            "root": {"parent": None, "children": ["u"], "message": None},
            "u": node("root", ["a"], [{"type": "REQUEST", "content": "问题"}]),
            "a": node(
                "u",
                [],
                [
                    {"type": "THINK", "content": "秘密推理"},
                    {"type": "RESPONSE", "content": "回答"},
                ],
            ),
        }
    )
    preview = service.preview("web.zip", "deepseek_harness", raw, project_id="personal")
    batch = service.commit(preview["preview_id"], "personal")
    with store.connect() as db:
        rows = db.execute(
            "SELECT role,content,memory_input FROM normalized_messages "
            "WHERE batch_id=? ORDER BY ordinal",
            (batch["batch_id"],),
        ).fetchall()
    assert [(r["role"], r["content"], r["memory_input"]) for r in rows] == [
        ("user", "问题", 1),
        ("assistant", "回答", 1),
    ]


def test_ambiguous_branches_are_not_selected_for_extraction(tmp_path):
    store = VaultStore(tmp_path)
    store.initialize()
    raw = archive(
        {
            "root": {"parent": None, "children": ["a", "b"], "message": None},
            "a": node("root", [], [{"type": "REQUEST", "content": "a"}]),
            "b": node("root", [], [{"type": "REQUEST", "content": "b"}]),
        }
    )
    service = ImportService(store)
    preview = service.preview("web.zip", "deepseek_harness", raw, project_id="personal")
    batch = service.commit(preview["preview_id"], "personal")
    with store.connect() as db:
        rows = db.execute(
            "SELECT memory_input FROM normalized_messages WHERE batch_id=?",
            (batch["batch_id"],),
        ).fetchall()
    assert [r[0] for r in rows] == [0, 0]
    assert preview["warnings"]


def test_rejects_zip_with_both_web_and_harness_signatures(tmp_path):
    store = VaultStore(tmp_path)
    store.initialize()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("session.v4.jsonl", "{}")
        z.writestr("conversations.json", "[]")
    with pytest.raises(ImportWorkflowError):
        ImportService(store).preview(
            "mixed.zip", "deepseek_harness", output.getvalue(), project_id="personal"
        )


def test_rejects_unsafe_web_archive_path(tmp_path):
    store = VaultStore(tmp_path)
    store.initialize()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("conversations.json", "[]")
        z.writestr("user.json", '{"user_id":"a"}')
        z.writestr("../unsafe.txt", "bad")
    with pytest.raises(ImportWorkflowError, match="Unsafe"):
        ImportService(store).preview(
            "web.zip", "deepseek_harness", output.getvalue(), project_id="personal"
        )


def test_candidate_api_accepts_long_web_message_for_memcell_splitting():
    from everos.entrypoints.api.routes.memo_vault_candidates import CandidateMessageDTO
    from everos.service.memo_vault_candidates import ChatMessage, _split_message

    message = CandidateMessageDTO(
        id="message-1",
        role="assistant",
        content=("这是一段用于验证长消息切分和来源定位的文本。\n" * 10_000)[:187_588],
        timestamp_ms=1,
    )
    assert len(message.content) == 187_588
    chat = ChatMessage(
        id=message.id,
        role=message.role,
        sender_id="user",
        sender_name="assistant",
        content=message.content,
        timestamp=message.timestamp_ms,
    )
    pieces = _split_message(chat)
    assert len(pieces) > 1
    assert "".join(piece.message.content for piece in pieces) == message.content
    assert all(
        message.content[piece.char_start : piece.char_end] == piece.message.content
        for piece in pieces
    )


def test_real_sample_preview_and_commit_when_available(tmp_path):
    sample = Path("docs/memo_input/deepseek/deepseek_data-2026-10-28.zip")
    if not sample.is_file():
        pytest.skip("private DeepSeek sample not present")
    store = VaultStore(tmp_path)
    store.initialize()
    service = ImportService(store)
    preview = service.preview(
        sample.name, "deepseek_harness", sample.read_bytes(), project_id="personal"
    )
    assert preview["conversation_count"] == 29
    batch = service.commit(preview["preview_id"], "personal")
    with store.connect() as db:
        counts = db.execute(
            "SELECT COUNT(*), SUM(memory_input) FROM normalized_messages "
            "WHERE batch_id=?",
            (batch["batch_id"],),
        ).fetchone()
    assert tuple(counts) == (172, 162)
    assert preview["warnings"]


@pytest.mark.asyncio
async def test_web_zip_review_to_markdown_pack(tmp_path, monkeypatch):
    import everos.entrypoints.memo_vault.app as app_module
    import everos.entrypoints.memo_vault.routes.imports as import_routes
    from everos.entrypoints.memo_vault.adapter import (
        EverOSMemoryRecord,
        EverOSMemoryRef,
    )

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    monkeypatch.setattr(
        app_module,
        "OwnerSecurity",
        lambda *_a, **_kw: SimpleNamespace(check=lambda _request: None),
    )

    class FakeEverOS:
        published = None

        async def extract_import(self, **kwargs):
            message = next(m for m in kwargs["messages"] if m["role"] == "user")
            return {
                "status": "extracted",
                "method": "local",
                "candidates": [
                    {
                        "candidate_id": "web-review-1",
                        "kind": "fact",
                        "content": message["content"],
                        "source_message_ids": [message["id"]],
                    }
                ],
            }

        async def publish_approved(self, **kwargs):
            self.published = kwargs["content"]
            return EverOSMemoryRef(
                "everos-web-review-1", kwargs["scope"], self.published, 1
            )

        async def list_user_memories(self, **kwargs):
            if self.published is None or kwargs["memory_type"] != "atomic_fact":
                return []
            return [
                EverOSMemoryRecord(
                    "everos-web-review-1",
                    "atomic_fact",
                    self.published,
                    "2026-10-28T00:00:00Z",
                    {},
                )
            ]

    fake = FakeEverOS()
    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", lambda *_a, **_kw: fake)
    raw = archive(
        {
            "root": {"parent": None, "children": ["u"], "message": None},
            "u": node("root", [], [{"type": "REQUEST", "content": "用户喜欢喝茶"}]),
        }
    )
    app = app_module.app
    async with app.router.lifespan_context(app):
        app.state.everos_adapter = fake
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            p = await client.post(
                "/api/v1/imports/preview",
                data={"source_type": "deepseek_harness", "project_id": "personal"},
                files={"file": ("web.zip", raw, "application/zip")},
            )
            assert p.status_code == 200
            b = await client.post(
                "/api/v1/imports/commit",
                json={
                    "preview_id": p.json()["data"]["preview_id"],
                    "project_id": "personal",
                },
            )
            assert b.status_code == 200
            extracted = await client.post(
                f"/api/v1/imports/{b.json()['data']['batch_id']}/extract"
            )
            assert (
                extracted.status_code == 200
                and extracted.json()["data"]["candidate_count"] == 1
            )
            confirmed = await client.post(
                "/api/v1/candidates/web-review-1/confirm",
                json={
                    "revision": 1,
                    "kind": "fact",
                    "namespace": "personal",
                    "external_use": "allow",
                    "evidence_kind": "human_verified",
                },
            )
            assert confirmed.status_code == 200
            request = {
                "namespaces": ["personal"],
                "kinds": ["fact"],
                "format": "markdown",
                "token_budget": 4000,
                "evidence_mode": "id",
                "memory_ids": ["web-review-1"],
            }
            preview = await client.post("/api/v1/packs/preview", json=request)
            assert (
                preview.status_code == 200
                and preview.json()["data"]["included_count"] == 1
            )
            data = preview.json()["data"]
            exported = await client.post(
                "/api/v1/packs/export",
                json={
                    **request,
                    "preview_sha256": data["preview_sha256"],
                    "preview_generated_at": data["generated_at"],
                    "confirmed_ask_ids": [],
                    "confirm_unrecoverable_transfer": True,
                    "delivery_action": "download",
                },
            )
            assert exported.status_code == 200
            assert fake.published in exported.json()["data"]["rendered_content"]
