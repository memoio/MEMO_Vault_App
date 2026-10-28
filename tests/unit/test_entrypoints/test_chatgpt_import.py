"""ChatGPT export conversion and Vault routing boundaries."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from everos.entrypoints.memo_vault.chatgpt_export import convert_chatgpt_export
from everos.entrypoints.memo_vault.imports import ImportService, ImportWorkflowError
from everos.entrypoints.memo_vault.routes.imports import _persist_candidates
from everos.entrypoints.memo_vault.storage import VaultStore
from everos.umif.validator import validate_package


SAMPLE = next((Path(__file__).resolve().parents[3] / "docs/memo_input/ChatGPT").glob("*.zip"))


def _zip(files: dict[str, object]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value if isinstance(value, bytes) else json.dumps(value))
    return output.getvalue()


def _convert(raw: bytes):
    return convert_chatgpt_export(raw, "export.zip", "personal",
                                  max_unpacked_bytes=50 * 1024 * 1024,
                                  max_files=100, max_compression_ratio=200)


def test_supplied_export_preserves_only_visible_messages_for_memory() -> None:
    package = _convert(SAMPLE.read_bytes())
    assert validate_package(package).valid
    assert len(package.sessions) == 3
    assert package.import_report["counts"]["memory_input_messages"] == 9
    assert package.import_report["skipped_event_counts"]["reasoning_recap"] == 4
    assert package.import_report["skipped_event_counts"]["thoughts"] == 1
    assert not package.optional_records["documents.jsonl"]


def test_branches_tools_and_real_file_are_separate_tracks() -> None:
    def message(mid: str, role: str, text: str, **extra):
        return {"id": mid, "author": {"role": role},
                "content": {"content_type": "text", "parts": [text]}, **extra}
    mapping = {
        "u": {"parent": None, "message": message("u", "user", "question")},
        "old": {"parent": "u", "message": message("old", "assistant", "old reply")},
        "call": {"parent": "u", "message": message("call", "assistant", "search", recipient="browser")},
        "result": {"parent": "call", "message": message("result", "tool", "found")},
        "new": {"parent": "result", "message": message("new", "assistant", "new reply")},
    }
    raw = _zip({"conversations.json": [{"conversation_id": "c", "current_node": "new", "mapping": mapping}],
                "library_files.json": [{"path": "files/note.txt", "name": "note.txt"}],
                "files/note.txt": b"note"})
    package = _convert(raw)
    assert validate_package(package).valid
    assert [event.routing_hints["user_memory"] for event in package.events
            if event.record_type == "event.message"] == ["allow", "allow", "deny"]
    assert [event.record_type for event in package.events if event.record_type != "event.message"] == [
        "event.tool_call", "event.tool_result"]
    assert len(package.optional_records["documents.jsonl"]) == 1


def test_preview_commit_and_candidate_evidence_exclude_other_branch(tmp_path: Path) -> None:
    def row(mid: str, text: str, parent: str | None):
        return {"parent": parent, "message": {"id": mid, "author": {"role": "user" if mid == "u" else "assistant"},
                "content": {"content_type": "text", "parts": [text]}}}
    raw = _zip({"conversations.json": [{"conversation_id": "c", "current_node": "new",
                                        "mapping": {"u": row("u", "q", None),
                                                    "old": row("old", "old", "u"),
                                                    "new": row("new", "new", "u")}}]})
    store = VaultStore(tmp_path)
    store.initialize()
    service = ImportService(store)
    preview = service.preview("export.zip", "chatgpt", raw, project_id="personal", content_type="application/zip")
    batch = service.commit(preview["preview_id"], "personal")
    with store.connect() as connection:
        rows = connection.execute("SELECT id,memory_input FROM normalized_messages WHERE batch_id=? ORDER BY ordinal",
                                  (batch["batch_id"],)).fetchall()
        assert [row["memory_input"] for row in rows] == [1, 1, 0]
        source_batch = dict(connection.execute("SELECT * FROM import_batches WHERE id=?", (batch["batch_id"],)).fetchone())
    with pytest.raises(HTTPException):
        _persist_candidates(store, batch_id=batch["batch_id"], batch=source_batch,
                            candidates=[{"candidate_id": "candidate-1", "kind": "fact", "content": "old",
                                         "source_message_ids": [rows[2]["id"]]}])


def test_nested_archive_is_rejected() -> None:
    with pytest.raises(ImportWorkflowError, match="Nested archive"):
        _convert(_zip({"conversations.json": [], "files/nested.zip": b"PK"}))


def test_manifest_sharded_conversations_keep_source_pointer() -> None:
    message = {"id": "m1", "author": {"role": "user"},
               "content": {"content_type": "text", "parts": ["hello"]}}
    raw = _zip({
        "export_manifest.json": {"logical_files": {"conversations.json": {
            "files": ["conversations-000.json"], "sharded": True}}},
        "conversations-000.json": [{"conversation_id": "c1", "current_node": "m1",
                                    "mapping": {"m1": {"parent": None, "message": message}}}],
    })
    package = _convert(raw)
    assert validate_package(package).valid
    assert package.events[0].provenance["member_path"] == "conversations-000.json"


@pytest.mark.asyncio
async def test_extract_route_uses_only_current_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import everos.entrypoints.memo_vault.routes.imports as routes

    def row(mid: str, text: str, parent: str | None):
        return {"parent": parent, "message": {"id": mid, "author": {"role": "user" if mid == "u" else "assistant"},
                "content": {"content_type": "text", "parts": [text]}}}
    raw = _zip({"conversations.json": [{"conversation_id": "c", "current_node": "new",
                                        "mapping": {"u": row("u", "question", None),
                                                    "old": row("old", "old reply", "u"),
                                                    "new": row("new", "new reply", "u")}}]})
    store = VaultStore(tmp_path)
    store.initialize()
    service = ImportService(store)
    preview = service.preview("export.zip", "chatgpt", raw, project_id="personal", content_type="application/zip")
    batch = service.commit(preview["preview_id"], "personal")
    observed: list[dict] = []

    class Adapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def extract_import(self, **kwargs):
            observed.extend(kwargs["messages"])
            return {"status": "extracted", "method": "local", "candidates": [
                {"candidate_id": "candidate-new", "kind": "fact", "content": "new reply",
                 "source_message_ids": [kwargs["messages"][1]["id"]]}]}

    monkeypatch.setattr(routes, "EverOSHttpAdapter", Adapter)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(vault_store=store)))
    response = await routes.extract_import(batch["batch_id"], request)
    assert response.data["candidate_count"] == 1
    assert [row["content"] for row in observed] == ["question", "new reply"]
    with store.connect() as connection:
        assert connection.execute("SELECT status FROM candidates WHERE id='candidate-new'").fetchone()[0] == "pending"
