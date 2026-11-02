from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from everos.entrypoints.memo_vault.adapter import (
    EverOSAdapterError,
    EverOSHttpAdapter,
    EverOSMemoryRef,
)
from everos.entrypoints.memo_vault.app import app
from everos.entrypoints.memo_vault.imports import (
    ImportWorkflowError,
    parse_import_file,
)
from everos.entrypoints.memo_vault.storage import VaultStore


def test_json_message_array_becomes_one_conversation_with_timestamps() -> None:
    result = parse_import_file(
        "chat.json",
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "  hi  ", "timestamp": 1234},
                    {"role": "assistant", "content": "hello", "id": "m2"},
                ]
            }
        ).encode(),
    )
    assert result.conversation_count == 1
    assert result.message_count == 2
    assert result.messages[0].content == "hi"
    assert result.messages[0].timestamp_ms == 1234
    assert result.messages[1].source_message_id == "m2"


def test_json_rejects_unrecognized_shape() -> None:
    with pytest.raises(ImportWorkflowError) as error:
        parse_import_file("chat.json", b'{"conversation": "not messages"}')
    assert error.value.code == "IMPORT_PARSE_FAILED"


def test_plain_text_without_speaker_labels_is_one_user_message() -> None:
    result = parse_import_file("notes.txt", b"Just a note.\nSecond line.")

    assert result.conversation_count == 1
    assert result.message_count == 1
    assert result.messages[0].role == "user"
    assert result.messages[0].content == "Just a note.\nSecond line."


def test_markdown_speaker_headings_split_messages() -> None:
    result = parse_import_file(
        "chat.md", b"## User\nI like tea.\n\n## Assistant\nGot it."
    )

    assert [(message.role, message.content) for message in result.messages] == [
        ("user", "I like tea."),
        ("assistant", "Got it."),
    ]


def test_unsupported_and_binary_files_are_rejected() -> None:
    for filename, payload in (("archive.zip", b"PK"), ("notes.txt", b"\xff\x00")):
        with pytest.raises(ImportWorkflowError) as error:
            parse_import_file(filename, payload)
        assert error.value.code == "IMPORT_SECURITY_REJECTED"


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_zip_parser_reads_only_supported_text_entries_as_separate_conversations() -> (
    None
):
    parsed = parse_import_file(
        "conversation.zip",
        _make_zip(
            {
                "chat/a.md": b"## User\nhello",
                "chat/b.json": b'{"messages":[{"role":"assistant","content":"world"}]}',
            }
        ),
        content_type="application/zip",
    )

    assert parsed.conversation_count == 2
    assert [(message.role, message.content) for message in parsed.messages] == [
        ("user", "hello"),
        ("assistant", "world"),
    ]


def test_zip_rejects_member_paths_deeper_than_configured_limit() -> None:
    deep_path = "/".join([*(f"d{index}" for index in range(9)), "chat.md"])
    with pytest.raises(ImportWorkflowError) as error:
        parse_import_file(
            "conversation.zip",
            _make_zip({deep_path: b"## User\nhello"}),
            content_type="application/zip",
        )
    assert error.value.code == "IMPORT_SECURITY_REJECTED"


def test_vault_identity_and_default_project_are_stable(tmp_path: Path) -> None:
    store = VaultStore(tmp_path / "vault")
    store.initialize()
    identity = store.get_identity()
    projects = store.list_projects()

    reopened = VaultStore(tmp_path / "vault")
    reopened.initialize()

    assert identity == reopened.get_identity()
    assert identity["app_id"].startswith("memo_vault_")
    assert identity["user_id"].startswith("user_")
    assert identity["agent_id"].startswith("agent_")
    assert [(item["project_id"], item["name"]) for item in projects] == [("personal", "个人空间")]


@pytest.mark.asyncio
async def test_project_api_lists_and_creates_projects(
    vault_client: httpx.AsyncClient,
) -> None:
    listed = await vault_client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert [(item["project_id"], item["name"]) for item in listed.json()["data"]["projects"]] == [("personal", "个人空间")]

    created = await vault_client.post(
        "/api/v1/projects", json={"name": "项目 A", "parent_project_id": "personal"}
    )
    assert created.status_code == 201
    assert created.json()["data"]["project_id"].startswith("space_")
    assert created.json()["data"]["name"] == "项目 A"
    invalid = await vault_client.post(
        "/api/v1/projects", json={"name": "无效/名称"}
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_zip_preview_combines_supported_files_without_extracting_to_disk(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    payload = _make_zip(
        {
            "chat/part-a.md": b"## User\nhello",
            "chat/part-b.json": b'{"messages":[{"role":"assistant","content":"world"}]}',
        }
    )

    response = await vault_client.post(
        "/api/v1/imports/preview",
        files={"file": ("conversation.zip", payload, "application/zip")},
    )

    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    assert preview["conversation_count"] == 2
    assert preview["message_count"] == 2
    assert {message["content"] for message in preview["messages"]} == {
        "hello",
        "world",
    }
    assert not list((tmp_path / "vault" / "evidence").iterdir())
    assert len(list((tmp_path / "vault" / "previews").iterdir())) == 2


@pytest.mark.asyncio
async def test_unsafe_zip_entries_are_rejected_without_any_preview_or_archive_writes(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    unsafe_archives = [
        _make_zip({"../escape.md": b"## User\nno"}),
        _make_zip({"nested.zip": b"PK\x03\x04"}),
        _make_zip({"chat.md": b"A" * (2 * 1024 * 1024)}),
    ]

    for payload in unsafe_archives:
        response = await vault_client.post(
            "/api/v1/imports/preview",
            files={"file": ("conversation.zip", payload, "application/zip")},
        )
        assert response.status_code in {413, 415, 422}
        assert response.json()["detail"]["code"] == "IMPORT_SECURITY_REJECTED"
        assert list((tmp_path / "vault" / "previews").iterdir()) == []
        with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
                == 0
            )


@pytest.mark.asyncio
async def test_zip_limits_reject_entry_count_and_uncompressed_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("MEMO_VAULT_MAX_ARCHIVE_FILES", "1")
    monkeypatch.setenv("MEMO_VAULT_MAX_ARCHIVE_UNPACKED_BYTES", "8")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        too_many = await client.post(
            "/api/v1/imports/preview",
            files={
                "file": (
                    "many.zip",
                    _make_zip({"one.txt": b"1", "two.txt": b"2"}),
                    "application/zip",
                )
            },
        )
        too_large = await client.post(
            "/api/v1/imports/preview",
            files={
                "file": (
                    "large.zip",
                    _make_zip({"large.txt": b"123456789"}),
                    "application/zip",
                )
            },
        )

    for response in (too_many, too_large):
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "IMPORT_SECURITY_REJECTED"
    assert list((tmp_path / "vault" / "previews").iterdir()) == []


@pytest.mark.asyncio
async def test_zip_with_no_supported_member_formats_fails_as_one_atomic_preview(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    response = await vault_client.post(
        "/api/v1/imports/preview",
        files={
            "file": (
                "unsupported.zip",
                _make_zip({"notes.pdf": b"%PDF unsupported"}),
                "application/zip",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_PARSE_FAILED"
    assert list((tmp_path / "vault" / "previews").iterdir()) == []
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_standardized_message_hash_deduplicates_across_batches(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    first_payload = b"User: Same   normalized message"
    first = await vault_client.post(
        "/api/v1/imports/preview",
        files={"file": ("first.txt", first_payload, "text/plain")},
    )
    first_data = first.json()["data"]
    committed = await vault_client.post(
        "/api/v1/imports/commit",
        json={"preview_id": first_data["preview_id"], "project_id": "personal"},
    )
    second = await vault_client.post(
        "/api/v1/imports/preview",
        files={
            "file": (
                "second.txt",
                b"User: Same normalized message",
                "text/plain",
            )
        },
    )

    assert second.status_code == 200
    assert second.json()["data"]["duplicate_message_count"] == 1
    assert second.json()["data"]["messages"][0]["duplicate"] is True
    archived = await vault_client.post(
        "/api/v1/imports/commit",
        json={
            "preview_id": second.json()["data"]["preview_id"],
            "project_id": "personal",
        },
    )

    assert archived.status_code == 200
    assert archived.json()["data"]["duplicate_message_count"] == 1
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 2
        )
        rows = connection.execute(
            "SELECT is_duplicate FROM normalized_messages ORDER BY rowid"
        ).fetchall()
    assert rows == [(0,), (1,)]
    assert committed.status_code == 200


@pytest.mark.asyncio
async def test_all_duplicate_messages_skip_everos_extraction(
    vault_client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import everos.entrypoints.memo_vault.routes.imports as import_routes

    class UnexpectedEverOSAdapter:
        def __init__(self, *_: object, **__: object) -> None:
            raise AssertionError("EverOS must not receive duplicate-only messages")

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", UnexpectedEverOSAdapter)
    batch_ids: list[str] = []
    for filename, payload in (
        ("first.txt", b"User: Same   normalized message"),
        ("second.txt", b"User: Same normalized message"),
    ):
        preview = await vault_client.post(
            "/api/v1/imports/preview",
            files={
                "file": (
                    filename,
                    payload,
                    "text/plain",
                )
            },
        )
        data = preview.json()["data"]
        committed = await vault_client.post(
            "/api/v1/imports/commit",
            json={"preview_id": data["preview_id"], "project_id": "personal"},
        )
        batch_ids.append(committed.json()["data"]["batch_id"])

    response = await vault_client.post(f"/api/v1/imports/{batch_ids[1]}/extract")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "no_new_messages",
        "method": "none",
        "candidate_count": 0,
        "candidate_scope": None,
        "deferred_tool_message_count": 0,
        "unsupported_role_message_count": 0,
    }
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        status = connection.execute(
            "SELECT extraction_status, extraction_method FROM import_batches WHERE id = ?",
            (batch_ids[1],),
        ).fetchone()
    assert status == ("extracted", "none")


@pytest.mark.asyncio
async def test_tool_only_batch_defers_extraction_without_claiming_running(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    preview = await vault_client.post(
        "/api/v1/imports/preview",
        files={
            "file": (
                "tool.json",
                b'{"messages":[{"role":"tool","content":"result"}]}',
                "application/json",
            )
        },
    )
    committed = await vault_client.post(
        "/api/v1/imports/commit",
        json={
            "preview_id": preview.json()["data"]["preview_id"],
            "project_id": "personal",
        },
    )

    response = await vault_client.post(
        f"/api/v1/imports/{committed.json()['data']['batch_id']}/extract"
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "deferred_tool_messages"
    assert response.json()["data"]["deferred_tool_message_count"] == 1
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        extraction_status = connection.execute(
            "SELECT extraction_status FROM import_batches WHERE id = ?",
            (committed.json()["data"]["batch_id"],),
        ).fetchone()[0]
    assert extraction_status == "not_started"


@pytest.mark.asyncio
async def test_deepseek_harness_tool_traces_are_not_sent_to_memory_extraction(
    vault_client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import everos.entrypoints.memo_vault.routes.imports as import_routes

    observed: dict[str, object] = {}

    class CandidateAdapter:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def extract_import(self, **kwargs: object) -> dict[str, object]:
            observed["messages"] = kwargs["messages"]
            return {
                "status": "extracted",
                "method": "local",
                "candidate_count": 0,
                "candidate_scope": None,
                "candidates": [],
            }

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", CandidateAdapter)
    payload = Path("tests/fixtures/memo_vault/deepseek_harness_v4.jsonl").read_bytes()
    preview = await vault_client.post(
        "/api/v1/imports/preview",
        data={"source_type": "deepseek_harness", "project_id": "personal"},
        files={"file": ("session.v4.jsonl", payload, "application/x-ndjson")},
    )
    assert preview.status_code == 200, preview.text
    batch = await vault_client.post(
        "/api/v1/imports/commit",
        json={
            "preview_id": preview.json()["data"]["preview_id"],
            "project_id": "personal",
        },
    )
    assert batch.status_code == 200, batch.text

    response = await vault_client.post(
        f"/api/v1/imports/{batch.json()['data']['batch_id']}/extract"
    )

    assert response.status_code == 200, response.text
    messages = observed["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert all("tool" not in str(message["content"]) for message in messages)
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        trace_count = connection.execute(
            "SELECT COUNT(*) FROM normalized_tool_events WHERE batch_id = ?",
            (batch.json()["data"]["batch_id"],),
        ).fetchone()[0]
    assert trace_count == 2


@pytest.mark.asyncio
async def test_zip_parse_failure_is_atomic_and_leaves_no_temporary_preview(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    response = await vault_client.post(
        "/api/v1/imports/preview",
        files={
            "file": (
                "mixed.zip",
                _make_zip(
                    {
                        "valid.md": b"## User\nparsed first",
                        "invalid.json": b'{"unknown":true}',
                    }
                ),
                "application/zip",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_PARSE_FAILED"
    assert list((tmp_path / "vault" / "previews").iterdir()) == []
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM normalized_messages").fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_preview_rejects_incompatible_mime_without_writing_preview(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    response = await vault_client.post(
        "/api/v1/imports/preview",
        files={"file": ("notes.md", b"plain text", "text/html")},
    )

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "IMPORT_SECURITY_REJECTED"
    assert list((tmp_path / "vault" / "previews").iterdir()) == []
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_preview_rejects_text_with_binary_control_bytes_without_writes(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    response = await vault_client.post(
        "/api/v1/imports/preview",
        files={"file": ("notes.txt", b"safe text\x01payload", "text/plain")},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_SECURITY_REJECTED"
    assert list((tmp_path / "vault" / "previews").iterdir()) == []
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        )


def test_vault_store_migrates_the_original_skeleton_schema(tmp_path: Path) -> None:
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    database = data_dir / "vault.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE import_batches (
                id TEXT PRIMARY KEY, source_name TEXT NOT NULL,
                file_sha256 TEXT NOT NULL, namespace TEXT, status TEXT NOT NULL,
                extraction_status TEXT NOT NULL DEFAULT 'not_started',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE normalized_messages (
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, timestamp_ms INTEGER,
                content_sha256 TEXT NOT NULL,
                UNIQUE(batch_id, conversation_id, ordinal)
            );
            INSERT INTO import_batches
                (id, source_name, file_sha256, namespace, status)
                VALUES ('legacy-batch', 'old.txt', 'old-hash', 'research/project-a', 'archived');
            """
        )

    VaultStore(data_dir).initialize()

    with sqlite3.connect(database) as connection:
        batch_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(import_batches)")
        }
        message_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(normalized_messages)")
        }
        legacy_scope = connection.execute(
            "SELECT app_id, project_id, user_id, agent_id FROM import_batches WHERE id = 'legacy-batch'"
        ).fetchone()
        projects = connection.execute(
            "SELECT project_id FROM project_spaces WHERE project_id = 'research-project-a'"
        ).fetchall()
    assert {"raw_relpath", "archived_at", "source_type"} <= batch_columns
    assert {"app_id", "project_id", "user_id", "agent_id"} <= batch_columns
    assert {"source_message_id", "is_duplicate"} <= message_columns
    assert legacy_scope[0].startswith("memo_vault_")
    assert legacy_scope[1] == "research-project-a"
    assert legacy_scope[2].startswith("user_")
    assert legacy_scope[3].startswith("agent_")
    assert len(projects) == 1


def test_duplicate_message_hash_ignores_whitespace_but_keeps_both_messages() -> None:
    result = parse_import_file(
        "chat.json",
        json.dumps(
            [
                {"role": "user", "content": "same   message"},
                {"role": "assistant", "content": "same message"},
            ]
        ).encode(),
    )
    assert result.duplicate_message_count == 1
    assert len(result.messages) == 2
    assert (
        result.messages[0].content_hash == hashlib.sha256(b"same message").hexdigest()
    )


def test_extract_import_persists_candidate_and_source_evidence_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import everos.entrypoints.memo_vault.routes.imports as import_routes

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("MEMO_VAULT_EVEROS_TIMEOUT_SECONDS", "45")
    observed: dict[str, object] = {}

    class CandidateAdapter:
        def __init__(self, *_: object, **kwargs: object) -> None:
            observed["timeout_seconds"] = kwargs.get("timeout_seconds")

        async def extract_import(self, **kwargs):  # type: ignore[no-untyped-def]
            observed.update(kwargs)
            return {
                "status": "extracted",
                "method": "local",
                "candidate_count": 1,
                "candidate_scope": {
                    "app_id": kwargs["app_id"],
                    "project_id": kwargs["project_id"],
                    "user_id": kwargs["user_id"],
                },
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "kind": "fact",
                        "content": "User prefers concise responses.",
                        "source_message_ids": [kwargs["messages"][0]["id"]],
                    }
                ],
                "deferred_tool_message_count": 0,
                "unsupported_role_message_count": 0,
            }

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", CandidateAdapter)

    async def scenario() -> tuple[dict[str, object], Path]:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            preview = await client.post(
                "/api/v1/imports/preview",
                files={
                    "file": (
                        "chat.json",
                        json.dumps(
                            {
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "Please be concise.",
                                    },
                                    {"role": "assistant", "content": "Understood."},
                                ]
                            }
                        ).encode(),
                        "application/json",
                    )
                },
            )
            committed = await client.post(
                "/api/v1/imports/commit",
                json={
                    "preview_id": preview.json()["data"]["preview_id"],
                    "project_id": "personal",
                },
            )
            batch_id = committed.json()["data"]["batch_id"]
            extracted = await client.post(f"/api/v1/imports/{batch_id}/extract")
            return extracted.json()["data"], app.state.vault_store.database_path

    result, database_path = asyncio.run(scenario())

    assert result["candidate_count"] == 1
    assert observed["timeout_seconds"] == 45.0
    assert observed["user_id"].startswith("user_")
    assert observed["agent_id"].startswith("agent_")
    assert all(
        message["role"] in {"user", "assistant"} for message in observed["messages"]
    )
    assert all("sender_id" not in message for message in observed["messages"])
    with sqlite3.connect(database_path) as connection:
        candidate = connection.execute(
            "SELECT id, kind, content, status FROM candidates"
        ).fetchone()
        evidence = connection.execute(
            "SELECT message_id, quote FROM candidate_evidence"
        ).fetchone()
        archived_count = connection.execute(
            "SELECT COUNT(*) FROM normalized_messages"
        ).fetchone()[0]
    assert candidate == (
        "candidate-1",
        "fact",
        "User prefers concise responses.",
        "pending",
    )
    assert evidence[1] == "Please be concise."
    assert archived_count == 2


@pytest.fixture
async def vault_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        yield client


@pytest.mark.asyncio
async def test_preview_does_not_archive_until_commit_and_commit_persists_evidence(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    payload = json.dumps(
        {"messages": [{"role": "user", "content": "remember this"}]}
    ).encode()
    preview = await vault_client.post(
        "/api/v1/imports/preview",
        files={"file": ("chat.json", payload, "application/json")},
    )

    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["conversation_count"] == 1
    assert data["message_count"] == 1
    database = tmp_path / "vault" / "vault.sqlite3"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        )

    committed = await vault_client.post(
        "/api/v1/imports/commit",
        json={"preview_id": data["preview_id"], "project_id": "personal"},
    )
    assert committed.status_code == 200
    batch = committed.json()["data"]
    assert batch["project_id"] == "personal"
    assert batch["app_id"].startswith("memo_vault_")
    assert batch["user_id"].startswith("user_")
    assert batch["agent_id"].startswith("agent_")
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM normalized_messages").fetchone()[0]
            == 1
        )
    with sqlite3.connect(database) as connection:
        raw_path = connection.execute(
            "SELECT raw_relpath FROM import_batches WHERE id = ?", (batch["batch_id"],)
        ).fetchone()[0]
    archived = tmp_path / "vault" / raw_path
    assert archived.read_bytes() == payload


@pytest.mark.asyncio
async def test_duplicate_raw_file_is_reported_without_creating_second_batch(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    payload = b"A plain note"
    first_batch_id: str | None = None
    for index in range(2):
        preview = await vault_client.post(
            "/api/v1/imports/preview",
            files={"file": ("note.txt", payload, "text/plain")},
        )
        data = preview.json()["data"]
        if index == 1:
            assert data["duplicate_batch_id"]
            assert data["duplicate_batch_id"] == first_batch_id
            continue
        committed = await vault_client.post(
            "/api/v1/imports/commit",
            json={"preview_id": data["preview_id"], "project_id": "personal"},
        )
        first_batch_id = committed.json()["data"]["batch_id"]
    assert first_batch_id is not None
    status = await vault_client.get(f"/api/v1/imports/{first_batch_id}/status")
    assert status.status_code == 200
    assert status.json()["data"]["batch_id"] == first_batch_id
    assert status.json()["data"]["project_id"] == "personal"
    assert status.json()["data"]["extraction_status"] == "not_started"
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1
        )


@pytest.mark.asyncio
async def test_invalid_project_id_cannot_commit_preview(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    preview = await vault_client.post(
        "/api/v1/imports/preview", files={"file": ("note.txt", b"text", "text/plain")}
    )
    preview_id = preview.json()["data"]["preview_id"]
    response = await vault_client.post(
        "/api/v1/imports/commit",
        json={"preview_id": preview_id, "project_id": "../outside"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_PROJECT_ID"
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_parse_failure_returns_error_and_creates_no_batch(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    response = await vault_client.post(
        "/api/v1/imports/preview",
        files={
            "file": (
                "chat.json",
                b'{"conversation": "unknown shape"}',
                "application/json",
            )
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_PARSE_FAILED"
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_cancelling_preview_removes_temporary_copy(
    vault_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    preview = await vault_client.post(
        "/api/v1/imports/preview",
        files={"file": ("note.txt", b"temporary note", "text/plain")},
    )
    preview_id = preview.json()["data"]["preview_id"]
    preview_dir = tmp_path / "vault" / "previews"
    assert len(list(preview_dir.iterdir())) == 2

    cancelled = await vault_client.delete(f"/api/v1/imports/previews/{preview_id}")
    commit = await vault_client.post(
        "/api/v1/imports/commit",
        json={"preview_id": preview_id, "project_id": "personal"},
    )

    assert cancelled.status_code == 200
    assert list(preview_dir.iterdir()) == []
    assert commit.status_code == 404


@pytest.mark.asyncio
async def test_configured_upload_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("MEMO_VAULT_MAX_IMPORT_BYTES", "3")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        response = await client.post(
            "/api/v1/imports/preview",
            files={"file": ("note.txt", b"four", "text/plain")},
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "IMPORT_SECURITY_REJECTED"


@pytest.mark.asyncio
async def test_unavailable_everos_leaves_archived_file_and_marks_extraction_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("MEMO_VAULT_EVEROS_URL", "http://127.0.0.1:1")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        preview = await client.post(
            "/api/v1/imports/preview",
            files={
                "file": (
                    "chat.json",
                    b'{"messages":[{"role":"user","content":"hi"}]}',
                )
            },
        )
        preview_data = preview.json()["data"]
        committed = await client.post(
            "/api/v1/imports/commit",
            json={
                "preview_id": preview_data["preview_id"],
                "project_id": "personal",
            },
        )
        batch_id = committed.json()["data"]["batch_id"]

        extraction = await client.post(f"/api/v1/imports/{batch_id}/extract")

    assert extraction.status_code == 503
    assert extraction.json()["detail"]["code"] == "EVEROS_UNAVAILABLE"
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite3") as connection:
        row = connection.execute(
            "SELECT extraction_status, raw_relpath FROM import_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
    assert row[0] == "failed"
    assert (tmp_path / "vault" / row[1]).is_file()


def test_adapter_preserves_conversation_in_stable_vault_scope() -> None:
    observed: dict[str, object] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        observed["request_timeout"] = request.extensions.get("timeout")
        payload = json.loads(request.content)
        observed["path"] = request.url.path
        observed["candidate_request"] = payload
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "extracted",
                    "method": "local",
                    "candidate_count": 1,
                    "candidate_scope": {
                        "app_id": "memo_vault_demo",
                        "project_id": "project-a",
                        "user_id": "user_demo",
                    },
                    "candidates": [
                        {
                            "candidate_id": "candidate-1",
                            "kind": "fact",
                            "content": "private text",
                            "source_message_ids": ["message-1"],
                        }
                    ],
                }
            },
        )

    adapter = EverOSHttpAdapter(
        "http://everos.test", transport=httpx.MockTransport(respond)
    )
    result = asyncio.run(
        adapter.extract_import(
            batch_id="a" * 32,
            app_id="memo_vault_demo",
            project_id="project-a",
            user_id="user_demo",
            agent_id="agent_demo",
            messages=[
                {
                    "id": "message-1",
                    "conversation_id": "conversation-1",
                    "ordinal": 0,
                    "role": "user",
                    "content": "private text",
                    "timestamp_ms": 1234,
                },
                {
                    "id": "message-2",
                    "conversation_id": "conversation-1",
                    "ordinal": 1,
                    "role": "assistant",
                    "content": "assistant reply",
                    "timestamp_ms": 1235,
                },
                {
                    "id": "message-3",
                    "conversation_id": "conversation-1",
                    "ordinal": 2,
                    "role": "tool",
                    "content": "tool output",
                    "timestamp_ms": 1236,
                },
                {
                    "id": "message-4",
                    "conversation_id": "conversation-1",
                    "ordinal": 3,
                    "role": "system",
                    "content": "system instruction",
                    "timestamp_ms": 1237,
                },
            ],
        )
    )

    request_payload = observed["candidate_request"]
    assert observed["path"] == "/api/v2/memo-vault/candidates/extract"
    assert request_payload["app_id"] == "memo_vault_demo"
    assert request_payload["project_id"] == "project-a"
    assert request_payload["user_id"] == "user_demo"
    assert request_payload["agent_id"] == "agent_demo"
    assert all("sender_id" not in message for message in request_payload["messages"])
    assert [message["role"] for message in request_payload["messages"]] == [
        "user",
        "assistant",
    ]
    assert [message["id"] for message in request_payload["messages"]] == [
        "message-1",
        "message-2",
    ]
    assert result["candidate_count"] == 1
    assert result["deferred_tool_message_count"] == 1
    assert result["unsupported_role_message_count"] == 1
    assert observed["request_timeout"]["read"] == 600.0


def test_adapter_preserves_structured_everos_extraction_error() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(
            503,
            json={
                "detail": {
                    "code": "LLM_UNAVAILABLE",
                    "message": "LLM provider is not configured.",
                }
            },
        )

    adapter = EverOSHttpAdapter(
        "http://everos.test", transport=httpx.MockTransport(respond)
    )
    with pytest.raises(EverOSAdapterError) as caught:
        asyncio.run(
            adapter.extract_import(
                batch_id="a" * 32,
                app_id="memo_vault_demo",
                project_id="project-a",
                user_id="user_demo",
                agent_id="agent_demo",
                messages=[
                    {
                        "id": "message-1",
                        "conversation_id": "conversation-1",
                        "role": "user",
                        "content": "hello",
                        "timestamp_ms": 1234,
                    }
                ],
            )
        )

    assert caught.value.code == "LLM_UNAVAILABLE"
    assert str(caught.value) == "LLM provider is not configured."


def test_adapter_preserves_canonical_everos_extraction_error() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(
            422,
            json={
                "request_id": "request-1",
                "error": {
                    "code": "PROVIDER_NOT_CONFIGURED",
                    "message": "LLM api_key and base_url is not configured.",
                    "timestamp": "2026-10-21T03:51:23Z",
                    "path": "/api/v2/memo-vault/candidates/extract",
                },
            },
        )

    adapter = EverOSHttpAdapter(
        "http://everos.test", transport=httpx.MockTransport(respond)
    )
    with pytest.raises(EverOSAdapterError) as caught:
        asyncio.run(
            adapter.extract_import(
                batch_id="a" * 32,
                app_id="memo_vault_demo",
                project_id="project-a",
                user_id="user_demo",
                agent_id="agent_demo",
                messages=[{
                    "id": "message-1",
                    "conversation_id": "conversation-1",
                    "role": "user",
                    "content": "hello",
                    "timestamp_ms": 1234,
                }],
            )
        )

    assert caught.value.code == "PROVIDER_NOT_CONFIGURED"
    assert caught.value.status_code == 422
    assert str(caught.value) == "LLM api_key and base_url is not configured."


def test_import_route_returns_upstream_provider_configuration_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import everos.entrypoints.memo_vault.routes.imports as import_routes

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    class CandidateAdapter:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def extract_import(self, **__: object) -> dict[str, object]:
            raise EverOSAdapterError(
                "LLM api_key and base_url is not configured.",
                code="PROVIDER_NOT_CONFIGURED",
                status_code=422,
            )

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", CandidateAdapter)

    async def scenario() -> httpx.Response:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            preview = await client.post(
                "/api/v1/imports/preview",
                files={
                    "file": (
                        "chat.json",
                        b'{"messages":[{"role":"user","content":"hello"}]}',
                        "application/json",
                    )
                },
            )
            assert preview.status_code == 200, preview.text
            batch = await client.post(
                "/api/v1/imports/commit",
                json={
                    "preview_id": preview.json()["data"]["preview_id"],
                    "project_id": "personal",
                },
            )
            assert batch.status_code == 200, batch.text
            return await client.post(
                f"/api/v1/imports/{batch.json()['data']['batch_id']}/extract"
            )

    response = asyncio.run(scenario())

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PROVIDER_NOT_CONFIGURED"
    assert "api_key and base_url" in response.json()["detail"]["message"]


def test_candidate_review_lists_evidence_and_publishes_approved_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    async def scenario() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            store = app.state.vault_store
            payload = json.dumps(
                {"messages": [
                    {"role": "user", "content": "I like tea."},
                    {"role": "assistant", "content": "I will remember that."},
                ]}
            ).encode()
            preview = await client.post(
                "/api/v1/imports/preview",
                files={"file": ("chat.json", payload, "application/json")},
            )
            batch = await client.post(
                "/api/v1/imports/commit",
                json={
                    "preview_id": preview.json()["data"]["preview_id"],
                    "project_id": "personal",
                },
            )
            batch_id = batch.json()["data"]["batch_id"]
            identity = store.get_identity()
            with store.connect() as connection:
                message_id = connection.execute(
                    "SELECT id FROM normalized_messages WHERE batch_id = ?", (batch_id,)
                ).fetchone()["id"]
                connection.execute(
                    "INSERT INTO candidates "
                    "(id, batch_id, everos_scope, kind, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        "candidate-review-1",
                        batch_id,
                        "scope",
                        "preference",
                        "I like tea.",
                    ),
                )
                connection.execute(
                    "INSERT INTO candidate_evidence "
                    "(candidate_id, message_id, char_start, char_end, quote, "
                    "quote_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "candidate-review-1",
                        message_id,
                        0,
                        10,
                        "I like tea.",
                        hashlib.sha256(b"I like tea.").hexdigest(),
                    ),
                )

            class Publisher:
                def __init__(self) -> None:
                    self.calls: list[dict[str, str]] = []

                async def publish_approved(
                    self, **kwargs
                ):  # type: ignore[no-untyped-def]
                    self.calls.append(kwargs)
                    return EverOSMemoryRef(
                        memory_id="af_approved_1",
                        scope=kwargs["scope"],
                        content=kwargs["content"],
                        revision=1,
                    )

            publisher = Publisher()
            app.state.everos_adapter = publisher
            listed = await client.get("/api/v1/candidates")
            assert listed.status_code == 200
            candidate = listed.json()["data"]["candidates"][0]
            assert candidate["id"] == "candidate-review-1"
            assert candidate["content"] == "I like tea."
            assert candidate["evidence"][0]["message_id"] == message_id
            assert "quote" not in candidate["evidence"][0]
            assert "source_content" not in candidate["evidence"][0]
            conversation = await client.get(
                f"/api/v1/candidates/candidate-review-1/source/{message_id}"
            )
            assert conversation.status_code == 200
            assert [message["content"] for message in conversation.json()["data"]["messages"]] == [
                "I like tea.", "I will remember that."
            ]
            assert (await client.get(
                "/api/v1/candidates/candidate-review-1/source/not-linked"
            )).status_code == 404
            assert candidate["origin"] == "import"
            assert candidate["evidence_status"] == "valid"
            assert candidate["validity_days"] is None
            assert len((await client.get("/api/v1/candidates?origin=import&evidence=valid")).json()["data"]["candidates"]) == 1
            assert len((await client.get("/api/v1/candidates?origin=agent")).json()["data"]["candidates"]) == 0
            assert len((await client.get("/api/v1/candidates?evidence=invalid")).json()["data"]["candidates"]) == 0

            confirmed = await client.post(
                "/api/v1/candidates/candidate-review-1/confirm",
                json={
                    "revision": 1,
                    "kind": "preference",
                    "namespace": "personal",
                    "external_use": "ask",
                    "validity_days": 30,
                    "evidence_kind": "human_verified",
                },
            )
            assert confirmed.status_code == 200
            assert confirmed.json()["data"]["status"] == "confirmed"
            assert publisher.calls == [
                {
                    "memory_id": "candidate-review-1",
                    "scope": (
                        f"{identity['app_id']}/personal/users/{identity['user_id']}"
                    ),
                    "content": "I like tea.",
                }
            ]
            with sqlite3.connect(store.database_path) as connection:
                assert (
                    connection.execute(
                        "SELECT status FROM candidates WHERE id = ?",
                        ("candidate-review-1",),
                    ).fetchone()[0]
                    == "confirmed"
                )
                metadata = connection.execute(
                    "SELECT target_project_id, validity_days, valid_until, external_use "
                    "FROM candidates WHERE id = ?", ("candidate-review-1",)
                ).fetchone()
                assert metadata[0] == "personal"
                assert metadata[1] == 30
                assert metadata[2]
                memory = connection.execute(
                    "SELECT namespace, origin, valid_until, external_use FROM memories WHERE id = ?",
                    ("candidate-review-1",),
                ).fetchone()
                assert memory == ("personal", "import", metadata[2], "ask")

    asyncio.run(scenario())


def test_everos_adapter_reports_health_and_publishes_exact_approved_text() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "1.2.3"})
        return httpx.Response(
            200,
            json={
                "request_id": "r1",
                "data": {
                    "memory_id": "user_af_20261020_0001",
                    "scope": "memo_vault/project-a/users/user-1",
                    "content": "I like tea.",
                    "revision": 1,
                },
            },
        )

    adapter = EverOSHttpAdapter(
        "http://everos.test", transport=httpx.MockTransport(respond)
    )

    async def scenario() -> None:
        health = await adapter.health()
        published = await adapter.publish_approved(
            memory_id="candidate-1",
            scope="memo_vault/project-a/users/user-1",
            content="I like tea.",
        )
        assert health.reachable and health.status == "ok"
        assert published.memory_id == "user_af_20261020_0001"
        assert published.content == "I like tea."

    asyncio.run(scenario())
    assert requests[0].url.path == "/health"
    assert requests[1].url.path == "/api/v2/memo-vault/candidates/candidate-1/publish"
    assert json.loads(requests[1].content) == {
        "candidate_id": "candidate-1",
        "app_id": "memo_vault",
        "project_id": "project-a",
        "user_id": "user-1",
        "content": "I like tea.",
    }


def test_vault_health_reports_live_everos_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import everos.entrypoints.memo_vault.routes.system as system_routes
    from everos.entrypoints.memo_vault.adapter import EverOSHealth

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    class Probe:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def health(self) -> EverOSHealth:
            return EverOSHealth(True, "ok", "1.2.3")

    monkeypatch.setattr(system_routes, "EverOSHttpAdapter", Probe)

    async def scenario() -> dict[str, object]:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            app.state.everos_adapter = Probe()
            response = await client.get("/api/v1/system/health")
            assert response.status_code == 200
            return response.json()["data"]

    result = asyncio.run(scenario())
    assert result["everos_status"] == "online"
    assert result["everos_checked_at"]
    assert result["features"]["review"] == "ready"


def test_candidate_evidence_rejects_unmentioned_people_and_accepts_linked_source() -> None:
    from everos.entrypoints.memo_vault.evidence import evidence_status

    source = "本周讨论 TREC dataset 的评估方法。"
    assert evidence_status("Caroline planned to invite friends for the trip.", [source]) == "mismatch"
    assert evidence_status("The team discussed TREC dataset evaluation.", [source]) == "valid"
    assert evidence_status("A candidate with no source.", []) == "missing"


def test_candidate_review_keeps_archived_source_and_accepts_human_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    async def scenario() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            preview = await client.post(
                "/api/v1/imports/preview",
                files={"file": ("plan.md", b"## User\nThe plan describes a memory MVP.", "text/markdown")},
            )
            batch = await client.post(
                "/api/v1/imports/commit",
                json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"},
            )
            batch_id = batch.json()["data"]["batch_id"]
            store = app.state.vault_store
            with store.connect() as connection:
                message = connection.execute(
                    "SELECT id, content FROM normalized_messages WHERE batch_id = ?", (batch_id,)
                ).fetchone()
                connection.execute(
                    "INSERT INTO candidates (id, batch_id, everos_scope, kind, content) VALUES (?, ?, ?, 'fact', ?)",
                    ("wrong-source", batch_id, "scope", "Caroline planned to invite friends for the trip."),
                )
                connection.execute(
                    "INSERT INTO candidate_evidence (candidate_id, message_id, quote, quote_sha256) VALUES (?, ?, ?, ?)",
                    ("wrong-source", message["id"], message["content"], hashlib.sha256(message["content"].encode()).hexdigest()),
                )
            result = await client.get("/api/v1/candidates")
            item = result.json()["data"]["candidates"][0]
            assert item["evidence_status"] == "valid"
            assert item["origin"] == "import"
            filtered = await client.get("/api/v1/candidates?evidence=invalid")
            assert filtered.json()["data"]["candidates"] == []
            assert (await client.get("/api/v1/candidates?evidence=valid")).json()["data"]["total"] == 1

            class Publisher:
                async def publish_approved(self, **kwargs):  # type: ignore[no-untyped-def]
                    return EverOSMemoryRef(
                        memory_id="af_corrected_source",
                        scope=kwargs["scope"],
                        content=kwargs["content"],
                        revision=1,
                    )

            app.state.everos_adapter = Publisher()
            corrected = await client.post(
                "/api/v1/candidates/wrong-source/confirm",
                json={
                    "revision": 1,
                    "kind": "fact",
                    "namespace": "personal",
                    "content": "The plan describes a memory MVP.",
                    "evidence_kind": "human_verified",
                },
            )
            assert corrected.status_code == 200

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_umif_package_and_stage_survive_model_failure_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import everos.entrypoints.memo_vault.routes.imports as import_routes
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    class FailingAdapter:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def extract_import(self, **__: object) -> dict[str, object]:
            raise EverOSAdapterError(
                "Candidate model returned invalid structured output.",
                code="EXTERNAL_SERVICE_UNAVAILABLE", stage="model_episode",
            )

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", FailingAdapter)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        preview = await client.post(
            "/api/v1/imports/preview",
            files={"file": ("session.v4.jsonl", Path("tests/fixtures/memo_vault/deepseek_harness_v4.jsonl").read_bytes(), "application/x-ndjson")},
            data={"source_type": "deepseek_harness", "project_id": "personal"},
        )
        assert preview.status_code == 200, preview.text
        committed = await client.post("/api/v1/imports/commit", json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"})
        assert committed.status_code == 200, committed.text
        batch_id = committed.json()["data"]["batch_id"]
        for _ in range(2):
            response = await client.post(f"/api/v1/imports/{batch_id}/extract")
            assert response.status_code == 503
            assert response.json()["detail"]["stage"] == "model_episode"
        status = await client.get(f"/api/v1/imports/{batch_id}/status")
        assert status.status_code == 200
        info = status.json()["data"]
        assert info["error_stage"] == "model_episode"
        assert info["extraction_status"] == "failed"
        assert (tmp_path / "vault" / info["umif_package_relpath"] / "manifest.json").is_file()


@pytest.mark.asyncio
async def test_candidate_persistence_failure_keeps_umif_and_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import everos.entrypoints.memo_vault.routes.imports as import_routes
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    class InvalidCandidateAdapter:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def extract_import(self, **__: object) -> dict[str, object]:
            return {"status": "extracted", "method": "local", "candidates": [{
                "candidate_id": "bad", "kind": "fact", "content": "unsupported source",
                "source_message_ids": ["outside-batch"],
            }]}

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", InvalidCandidateAdapter)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        preview = await client.post("/api/v1/imports/preview", files={"file": ("chat.md", b"## User\nI like tea.", "text/markdown")})
        assert preview.status_code == 200, preview.text
        committed = await client.post("/api/v1/imports/commit", json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"})
        assert committed.status_code == 200, committed.text
        batch_id = committed.json()["data"]["batch_id"]
        result = await client.post(f"/api/v1/imports/{batch_id}/extract")
        assert result.status_code == 502
        assert result.json()["detail"]["stage"] == "candidate_persist"
        status = (await client.get(f"/api/v1/imports/{batch_id}/status")).json()["data"]
        assert status["error_stage"] == "candidate_persist"
        assert (tmp_path / "vault" / status["umif_package_relpath"] / "manifest.json").is_file()


def test_generic_duplicate_checks_are_scoped_to_project(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.imports import ImportService

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    project = store.create_project(name="Other project", parent_project_id="personal")
    service = ImportService(store)
    payload = b"## User\nThe same source in two projects."
    first = service.preview("note.md", "generic", payload, project_id="personal")
    concurrent = service.preview("note.md", "generic", payload, project_id="personal")
    first_batch = service.commit(first["preview_id"], "personal")
    with pytest.raises(ImportWorkflowError) as concurrent_error:
        service.commit(concurrent["preview_id"], "personal")
    assert concurrent_error.value.code == "IMPORT_DUPLICATE_FILE"
    second = service.preview("note.md", "generic", payload, project_id=project["project_id"])
    assert second["duplicate_batch_id"] is None
    assert second["duplicate_message_count"] == 0
    second_batch = service.commit(second["preview_id"], project["project_id"])
    with store.connect() as connection:
        rows = connection.execute("SELECT is_duplicate FROM normalized_messages WHERE batch_id = ?", (second_batch["batch_id"],)).fetchall()
    assert rows and all(row[0] == 0 for row in rows)
    duplicate = service.preview("note.md", "generic", payload, project_id="personal")
    assert duplicate["duplicate_batch_id"] == first_batch["batch_id"]
    with pytest.raises(ImportWorkflowError) as error:
        service.commit(duplicate["preview_id"], "personal")
    assert error.value.code == "IMPORT_DUPLICATE_FILE"


def test_generic_umif_scope_matches_selected_project(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.umif.package import read_package
    store = VaultStore(tmp_path / "vault")
    store.initialize()
    project = store.create_project(name="Scope B", parent_project_id="personal")
    service = ImportService(store)
    preview = service.preview("note.md", "generic", b"## User\nI like tea.", project_id=project["project_id"])
    with pytest.raises(ImportWorkflowError) as wrong:
        service.commit(preview["preview_id"], "personal")
    assert wrong.value.code == "PREVIEW_PROJECT_MISMATCH"
    batch = service.commit(preview["preview_id"], project["project_id"])
    with store.connect() as connection:
        row = connection.execute("SELECT umif_package_relpath FROM import_batches WHERE id = ?", (batch["batch_id"],)).fetchone()
    package = read_package(store.data_dir / row["umif_package_relpath"])
    assert package.manifest.default_scope["project_id"] == project["project_id"]


def test_generic_umif_ids_are_scoped_to_source_and_conversation() -> None:
    from everos.entrypoints.memo_vault.imports import convert_parsed_markdown
    from everos.umif.validator import validate_package

    first = parse_import_file("first.md", b"## User\nFirst note")
    second = parse_import_file("second.md", b"## User\nSecond note")
    first_package = convert_parsed_markdown(first, "personal", source_identity="first")
    second_package = convert_parsed_markdown(second, "personal", source_identity="second")
    assert first_package.sessions[0].record_id != second_package.sessions[0].record_id
    assert first_package.events[0].record_id != second_package.events[0].record_id

    archive = _make_zip({
        "a.json": b'{"messages":[{"id":"m1","role":"user","content":"First"}]}',
        "b.json": b'{"messages":[{"id":"m1","role":"user","content":"Second"}]}',
    })
    parsed = parse_import_file("notes.zip", archive)
    package = convert_parsed_markdown(parsed, "personal", source_identity="archive")
    assert validate_package(package).valid
    assert len({event.record_id for event in package.events}) == 2


def test_background_extraction_returns_immediately_and_rejects_duplicates(tmp_path, monkeypatch):
    import everos.entrypoints.memo_vault.routes.imports as import_routes

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowAdapter:
        def __init__(self, *args, **kwargs):
            pass

        async def extract_import(self, **kwargs):
            started.set()
            await release.wait()
            return {"status": "extracted", "method": "local", "candidates": [], "candidate_count": 0}

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", SlowAdapter)

    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            preview = await client.post("/api/v1/imports/preview", files={"file": ("x.txt", b"hello", "text/plain")})
            committed = await client.post("/api/v1/imports/commit", json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"})
            batch = committed.json()["data"]["batch_id"]
            response = await asyncio.wait_for(client.post(f"/api/v1/imports/{batch}/extract/start"), 2)
            assert response.status_code == 202
            await asyncio.wait_for(started.wait(), 2)
            duplicate = await client.post(f"/api/v1/imports/{batch}/extract/start")
            assert duplicate.status_code == 409
            release.set()
            for _ in range(100):
                state = (await client.get(f"/api/v1/imports/{batch}/status")).json()["data"]
                if state["extraction_status"] == "extracted":
                    break
                await asyncio.sleep(0.01)
            assert state["extraction_status"] == "extracted"

    asyncio.run(scenario())


def test_background_extraction_is_marked_failed_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            preview = await client.post(
                "/api/v1/imports/preview",
                files={"file": ("x.txt", b"hello", "text/plain")},
            )
            committed = await client.post(
                "/api/v1/imports/commit",
                json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"},
            )
            batch = committed.json()["data"]["batch_id"]
            with app.state.vault_store.connect() as connection:
                connection.execute(
                    "UPDATE import_batches SET extraction_status = 'running' WHERE id = ?",
                    (batch,),
                )
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            state = (await client.get(f"/api/v1/imports/{batch}/status")).json()["data"]
            assert state["extraction_status"] == "failed"
            assert state["error_code"] == "EXTRACTION_INTERRUPTED"

    asyncio.run(scenario())


def test_background_extraction_reports_adapter_timeout(tmp_path, monkeypatch):
    import everos.entrypoints.memo_vault.routes.imports as import_routes

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    observed = {}

    class TimeoutAdapter:
        def __init__(self, *args, **kwargs):
            observed.update(kwargs)

        async def extract_import(self, **kwargs):
            raise EverOSAdapterError("model request timed out", stage="everos_health")

    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", TimeoutAdapter)

    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            preview = await client.post(
                "/api/v1/imports/preview",
                files={"file": ("x.txt", b"hello", "text/plain")},
            )
            committed = await client.post(
                "/api/v1/imports/commit",
                json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"},
            )
            batch = committed.json()["data"]["batch_id"]
            assert (await client.post(f"/api/v1/imports/{batch}/extract/start")).status_code == 202
            for _ in range(100):
                state = (await client.get(f"/api/v1/imports/{batch}/status")).json()["data"]
                if state["extraction_status"] == "failed":
                    break
                await asyncio.sleep(0.01)
            assert state["error_message"] == "model request timed out"
            assert observed["timeout_seconds"] == 7200.0

    asyncio.run(scenario())


def test_background_extraction_preserves_deferred_result(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            preview = await client.post("/api/v1/imports/preview", files={"file": ("tool.json", b'{"messages":[{"role":"tool","content":"result"}]}', "application/json")})
            committed = await client.post("/api/v1/imports/commit", json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"})
            batch = committed.json()["data"]["batch_id"]
            assert (await client.post(f"/api/v1/imports/{batch}/extract/start")).status_code == 202
            for _ in range(100):
                state = (await client.get(f"/api/v1/imports/{batch}/status")).json()["data"]
                if state.get("extraction_result") is not None:
                    break
                await asyncio.sleep(0.01)
            assert state["extraction_result"]["status"] == "deferred_tool_messages"
            assert state["extraction_result"]["deferred_tool_message_count"] == 1

    asyncio.run(scenario())


def test_background_unexpected_exception_is_recorded(tmp_path, monkeypatch):
    import everos.entrypoints.memo_vault.routes.imports as import_routes

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    class BrokenAdapter:
        def __init__(self, *args, **kwargs):
            raise ValueError("broken setup")
    monkeypatch.setattr(import_routes, "EverOSHttpAdapter", BrokenAdapter)

    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            preview = await client.post("/api/v1/imports/preview", files={"file": ("x.txt", b"hello", "text/plain")})
            committed = await client.post("/api/v1/imports/commit", json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"})
            batch = committed.json()["data"]["batch_id"]
            assert (await client.post(f"/api/v1/imports/{batch}/extract/start")).status_code == 202
            for _ in range(100):
                state = (await client.get(f"/api/v1/imports/{batch}/status")).json()["data"]
                if state["extraction_status"] == "failed":
                    break
                await asyncio.sleep(0.01)
            assert state["error_code"] == "EXTRACTION_FAILED"
    asyncio.run(scenario())


def test_split_episode_candidates_preserve_text_and_sources_in_vault(tmp_path, monkeypatch):
    from everos.entrypoints.memo_vault.routes.imports import _persist_candidates
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))

    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            preview = await client.post("/api/v1/imports/preview", files={"file": ("note.txt", "用户讨论了项目计划".encode(), "text/plain")})
            committed = await client.post("/api/v1/imports/commit", json={"preview_id": preview.json()["data"]["preview_id"], "project_id": "personal"})
            batch_id = committed.json()["data"]["batch_id"]
            store = app.state.vault_store
            with store.connect() as connection:
                batch = dict(connection.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone())
                message_id = connection.execute("SELECT id FROM normalized_messages WHERE batch_id=?", (batch_id,)).fetchone()[0]
            parts = ["用户讨论项目。\n\n", "后续计划。"]
            candidates = [{"candidate_id": f"split-{i}", "memory_type": "episode", "kind": "episode", "content": part, "source_message_ids": [message_id], "metadata": {"episode_group_id": "group", "part_index": i, "part_count": 2}} for i, part in enumerate(parts, 1)]
            assert _persist_candidates(store, batch_id=batch_id, batch=batch, candidates=candidates) == 2
            response = await client.get("/api/v1/candidates")
            assert response.status_code == 200
            rows = [row for row in response.json()["data"]["candidates"] if row["id"].startswith("split-")]
            rows.sort(key=lambda row: row["metadata"]["part_index"])
            assert "".join(row["content"] for row in rows) == "".join(parts)
            assert all(row["status"] == "pending" and row["evidence"] for row in rows)
            rejected = await client.post("/api/v1/candidates/split-1/reject", json={"revision": rows[0]["revision"]})
            assert rejected.status_code == 200
            with store.connect() as connection:
                statuses = dict(connection.execute("SELECT id, status FROM candidates WHERE id IN ('split-1', 'split-2')").fetchall())
            assert statuses == {"split-1": "rejected", "split-2": "pending"}
    asyncio.run(scenario())
