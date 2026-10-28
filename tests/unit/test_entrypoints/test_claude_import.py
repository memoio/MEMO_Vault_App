"""Claude acquisition, fidelity, review and immutable package contracts."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


def archive(files: dict[str, object]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        for name, value in files.items():
            z.writestr(name, value if isinstance(value, bytes) else json.dumps(value))
    return stream.getvalue()


def sample() -> bytes:
    return archive(
        {
            "users.json": [
                {"uuid": "account-1", "email_address": "private@example.test"}
            ],
            "conversations.json": [
                {
                    "uuid": "chat-1",
                    "name": "Example",
                    "project_uuid": "project-1",
                    "chat_messages": [
                        {
                            "uuid": "message-1",
                            "sender": "human",
                            "created_at": "2026-01-01T00:00:00Z",
                            "text": "Use uv for Python.",
                            "content": [{"type": "text", "text": "Use uv for Python."}],
                        },
                        {
                            "uuid": "message-2",
                            "sender": "assistant",
                            "text": "Understood.",
                        },
                    ],
                }
            ],
            "projects.json": [
                {
                    "uuid": "project-1",
                    "name": "Work",
                    "prompt_template": "Project instructions",
                    "docs": [
                        {
                            "uuid": "doc-1",
                            "filename": "notes.txt",
                            "content": "Reference material",
                        }
                    ],
                }
            ],
            "memories.json": [{"uuid": "memory-1", "content": "The user prefers uv."}],
        }
    )


def test_local_zip_bundle_is_one_archived_import(tmp_path: Path) -> None:
    """A downloaded multi-part export must remain one UMIF package and batch."""
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    service = ImportService(store)
    manager = ImportJobManager(store, service)
    parts = {
        "light_metadata-000.zip": archive({"users.json": [{"uuid": "account-1"}]}),
        "conversations-000.zip": archive(
            {
                "conversations.json": [
                    {
                        "uuid": "chat-1",
                        "project_uuid": "project-1",
                        "chat_messages": [
                            {"uuid": "msg-1", "sender": "human", "text": "hello"}
                        ],
                    }
                ]
            }
        ),
        "projects-000.zip": archive(
            {
                "projects.json": [
                    {
                        "uuid": "project-1",
                        "name": "Work",
                        "prompt_template": "Be concise",
                    }
                ]
            }
        ),
        "memories-000.zip": archive(
            {
                "memories.json": [
                    {"uuid": "memory-1", "content": "The user likes concise answers."}
                ]
            }
        ),
    }
    sources = []
    for name, data in parts.items():
        path = tmp_path / name
        path.write_bytes(data)
        sources.append((name, path))

    job = manager.create_local_bundle(sources, "personal")
    assert job["input_kind"] == "local_bundle"
    assert job["part_count"] == 4
    manager.run(job["id"])
    assert manager.status(job["id"])["status"] == "ready_for_preview"
    preview = manager.preview(job["id"])
    assert preview["conversation_count"] == 1
    assert preview["message_count"] == 1
    assert preview["umif_record_count"] >= 10
    batch = service.commit(preview["preview_id"], "personal")
    archived = store.data_dir / "evidence" / batch["batch_id"] / "source"
    assert {path.name for path in archived.glob("*.zip")} == set(parts)
    assert manager.status(job["id"])["status"] == "committed"


def test_local_zip_bundle_rejects_duplicate_names_before_creating_job(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    manager = ImportJobManager(store, ImportService(store))
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(sample())
    second.write_bytes(sample())
    with pytest.raises(ImportWorkflowError, match="Duplicate"):
        manager.create_local_bundle(
            [("conversations-000.zip", first), ("conversations-000.zip", second)],
            "personal",
        )
    assert manager.list_jobs() == []


def test_local_zip_bundle_uses_manifest_only_to_check_complete_file_set(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    manager = ImportJobManager(store, ImportService(store))
    source = tmp_path / "conversations-000.zip"
    source.write_bytes(sample())
    manifest = json.dumps(
        {
            "version": "1.0",
            "total_files": 2,
            "data_files": [
                {
                    "category": category,
                    "part": 0,
                    "batch_index": index,
                    "filename": filename,
                    "export_url": f"https://claude.ai/export/{filename}?secret=not-used",
                }
                for index, (category, filename) in enumerate(
                    [
                        ("conversations", "conversations-000.zip"),
                        ("memories", "memories-000.zip"),
                    ]
                )
            ],
        }
    ).encode()
    with pytest.raises(ImportWorkflowError, match="Missing ZIP"):
        manager.create_local_bundle(
            [("conversations-000.zip", source)], "personal", manifest=manifest
        )
    assert manager.list_jobs() == []


def test_local_bundle_archives_uppercase_zip_extension(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    service = ImportService(store)
    manager = ImportJobManager(store, service)
    source = tmp_path / "CLAUDE.ZIP"
    source.write_bytes(sample())
    job = manager.create_local_bundle([("CLAUDE.ZIP", source)], "personal")
    manager.run(job["id"])
    preview = manager.preview(job["id"])
    batch = service.commit(preview["preview_id"], "personal")
    assert (
        store.data_dir / "evidence" / batch["batch_id"] / "source" / "CLAUDE.ZIP"
    ).is_file()


def test_local_bundle_with_manifest_cannot_replace_a_zip(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    manager = ImportJobManager(store, ImportService(store))
    source = tmp_path / "part.zip"
    original = sample()
    source.write_bytes(original)
    manifest = json.dumps(
        {
            "version": "1.0",
            "total_files": 1,
            "data_files": [
                {
                    "category": "conversations",
                    "part": 0,
                    "batch_index": 0,
                    "filename": "part.zip",
                    "export_url": "https://claude.ai/export/part.zip",
                }
            ],
        }
    ).encode()
    job = manager.create_local_bundle(
        [("part.zip", source)], "personal", manifest=manifest
    )
    with pytest.raises(ImportWorkflowError) as error:
        manager.supply_part(job["id"], "part.zip", archive({"users.json": []}))
    assert error.value.status_code == 409
    assert (manager.path(job["id"]) / "part.zip").read_bytes() == original


def test_cancel_during_local_bundle_conversion_stays_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import everos.entrypoints.memo_vault.import_jobs as jobs_module
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    manager = ImportJobManager(store, ImportService(store))
    source = tmp_path / "part.zip"
    source.write_bytes(sample())
    job = manager.create_local_bundle([("part.zip", source)], "personal")
    real_convert = jobs_module.convert_claude_export

    def cancel_after_parse(*args, **kwargs):
        result = real_convert(*args, **kwargs)
        manager.cancel(job["id"])
        return result

    monkeypatch.setattr(jobs_module, "convert_claude_export", cancel_after_parse)
    manager.run(job["id"])
    assert manager.status(job["id"])["status"] == "cancelled"


async def test_local_upload_body_limit_rejects_before_multipart_spooling() -> None:
    from everos.entrypoints.memo_vault.request_limits import (
        ClaudeLocalUploadLimitMiddleware,
    )

    called = False
    body_reads = 0
    sent = []

    async def downstream(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        nonlocal body_reads
        body_reads += 1
        return {"type": "http.request", "body": b"large", "more_body": False}

    async def send(message):
        sent.append(message)

    app = ClaudeLocalUploadLimitMiddleware(downstream, overhead_bytes=0)
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/import-jobs/local",
            "headers": [(b"content-length", b"100")],
            "app": SimpleNamespace(
                state=SimpleNamespace(import_jobs=SimpleNamespace(max_total_bytes=8))
            ),
        },
        receive,
        send,
    )
    assert sent[0]["status"] == 413
    assert body_reads == 0
    assert not called


async def test_local_upload_body_limit_counts_chunked_request_bytes() -> None:
    from everos.entrypoints.memo_vault.request_limits import (
        ClaudeLocalUploadLimitMiddleware,
    )

    consumed = []
    sent = []
    chunks = iter(
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"67890", "more_body": False},
        ]
    )

    async def downstream(scope, receive, send):
        consumed.append(await receive())
        consumed.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    app = ClaudeLocalUploadLimitMiddleware(downstream, overhead_bytes=0)
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/import-jobs/local",
            "headers": [],
            "app": SimpleNamespace(
                state=SimpleNamespace(import_jobs=SimpleNamespace(max_total_bytes=8))
            ),
        },
        receive,
        send,
    )
    assert sent[0]["status"] == 413
    assert len(consumed) == 1


async def test_http_chunked_oversized_local_upload_returns_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from everos.entrypoints.memo_vault.app import app

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        app.state.import_jobs.max_total_bytes = 1024

        async def body():
            yield (
                b'--upload\r\nContent-Disposition: form-data; name="files"; '
                b'filename="a.zip"\r\nContent-Type: application/zip\r\n\r\n'
            )
            yield b"x" * (5 * 1024 * 1024)
            yield b"\r\n--upload--\r\n"

        response = await client.post(
            "/api/v1/import-jobs/local",
            content=body(),
            headers={"content-type": "multipart/form-data; boundary=upload"},
        )
        assert response.status_code == 413
        assert app.state.import_jobs.list_jobs() == []


def test_claude_conversion_preserves_roles_assertions_and_provenance(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export
    from everos.umif.package import read_package, write_package
    from everos.umif.validator import validate_package

    p = tmp_path / "export.zip"
    p.write_bytes(sample())
    package = convert_claude_export([p], "personal")
    assert [e.payload["role"] for e in package.events] == ["user", "assistant"]
    assert package.events[0].payload["content_parts"] == [
        {"type": "text", "text": "Use uv for Python."}
    ]
    assert package.events[1].timestamp_ms is None
    assert len(package.optional_records["documents.jsonl"]) == 2
    assert len(package.optional_records["memories.jsonl"]) == 1
    assert package.events[0].provenance["member_path"] == "conversations.json"
    assert "private@example.test" not in package.model_dump_json()
    write_package(package, tmp_path / "umif")
    restored = read_package(tmp_path / "umif")
    assert validate_package(restored, tmp_path / "umif").valid
    assert restored.blobs == package.blobs
    assert (
        convert_claude_export([p], "personal").manifest.package_id
        == package.manifest.package_id
    )


def test_claude_rejects_unsafe_zip_and_unknown_schema(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError

    p = tmp_path / "export.zip"
    p.write_bytes(archive({"../escape.json": []}))
    with pytest.raises(ImportWorkflowError, match="path"):
        convert_claude_export([p], "personal")
    p.write_bytes(archive({"conversations.json": {"new_unknown_format": []}}))
    with pytest.raises(ImportWorkflowError, match="schema"):
        convert_claude_export([p], "personal")


def test_manifest_rejects_duplicate_parts_and_unsafe_urls() -> None:
    from everos.entrypoints.memo_vault.claude_manifest import parse_manifest
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError

    row = {
        "category": "conversations",
        "part": 0,
        "batch_index": 0,
        "filename": "conversations-000.zip",
        "export_url": "https://claude.ai/export/a/download/b?secret=x",
    }
    assert (
        len(
            parse_manifest(
                json.dumps(
                    {"version": "1.0", "total_files": 1, "data_files": [row]}
                ).encode()
            )
        )
        == 1
    )
    for url in [
        "http://claude.ai/a",
        "https://127.0.0.1/a",
        "https://claude.ai.evil.test/a",
    ]:
        with pytest.raises(ImportWorkflowError):
            parse_manifest(
                json.dumps(
                    {
                        "version": "1.0",
                        "total_files": 1,
                        "data_files": [{**row, "export_url": url}],
                    }
                ).encode()
            )
    with pytest.raises(ImportWorkflowError):
        parse_manifest(
            json.dumps(
                {"version": "1.0", "total_files": 2, "data_files": [row, row]}
            ).encode()
        )


def test_download_cache_reused_and_expired_link_is_safe(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.export_downloader import ExportDownloader
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, content=sample())

    downloader = ExportDownloader(transport=httpx.MockTransport(handler))
    p = tmp_path / "export.zip"
    downloader.download("https://claude.ai/export/a/download/b?secret=private", p)
    downloader.download("https://claude.ai/export/a/download/b?secret=changed", p)
    assert calls == ["GET"]
    expired = ExportDownloader(
        transport=httpx.MockTransport(lambda _: httpx.Response(403))
    )
    with pytest.raises(ImportWorkflowError) as error:
        expired.download(
            "https://claude.ai/export/a/download/b?secret=private",
            tmp_path / "missing.zip",
        )
    assert error.value.code == "CLAUDE_LINK_EXPIRED"
    assert "private" not in str(error.value)


def test_job_to_immutable_preview_and_commit(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore
    from everos.umif.package import read_package

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    service = ImportService(store)
    manager = ImportJobManager(store, service)
    job = manager.create("export.zip", sample(), "personal")
    manager.run(job["id"])
    status = manager.status(job["id"])
    assert status["status"] == "ready_for_preview", status
    preview = manager.preview(job["id"])
    assert preview["message_count"] == 2
    result = service.commit(preview["preview_id"], "personal")
    with store.connect() as c:
        batch = c.execute(
            "SELECT * FROM import_batches WHERE id=?", (result["batch_id"],)
        ).fetchone()
    package = read_package(store.data_dir / batch["umif_package_relpath"])
    assert len(package.optional_records["memories.jsonl"]) == 1
    assert list((store.data_dir / batch["raw_relpath"]).parent.glob("source/*zip"))


def test_blob_tampering_fails_validation(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export
    from everos.umif.package import write_package
    from everos.umif.validator import validate_package

    p = tmp_path / "export.zip"
    p.write_bytes(sample())
    package = convert_claude_export([p], "personal")
    write_package(package, tmp_path / "umif")
    blob = tmp_path / "umif" / next(iter(package.blobs))
    blob.write_bytes(b"tampered")
    assert not validate_package(package, tmp_path / "umif").valid


async def test_http_claude_job_preview_and_saved_memory_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from everos.entrypoints.memo_vault.app import app

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        created = await client.post(
            "/api/v1/import-jobs",
            data={"project_id": "personal"},
            files={"file": ("claude.zip", sample(), "application/zip")},
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["data"]["id"]
        for _ in range(100):
            import asyncio

            status = (await client.get(f"/api/v1/import-jobs/{job_id}")).json()["data"]
            if status["status"] in {"ready_for_preview", "failed"}:
                break
            await asyncio.sleep(0.01)
        assert status["status"] == "ready_for_preview", status
        preview = await client.post(f"/api/v1/import-jobs/{job_id}/preview", json={})
        assert preview.status_code == 200, preview.text
        batch = await client.post(
            "/api/v1/imports/commit",
            json={
                "preview_id": preview.json()["data"]["preview_id"],
                "project_id": "personal",
            },
        )
        assert batch.status_code == 200, batch.text
        batch_id = batch.json()["data"]["batch_id"]
        content = await client.get(f"/api/v1/imports/{batch_id}/claude-content")
        assert content.status_code == 200, content.text
        data = content.json()["data"]
        assert len(data["documents"]) == 2
        assertion = data["assertions"][0]
        review = await client.post(
            f"/api/v1/imports/{batch_id}/assertions/{assertion['record_id']}/review"
        )
        assert review.status_code == 200, review.text
        again = await client.post(
            f"/api/v1/imports/{batch_id}/assertions/{assertion['record_id']}/review"
        )
        assert (
            again.json()["data"]["candidate_id"]
            == review.json()["data"]["candidate_id"]
        )
        with app.state.vault_store.connect() as c:
            source = c.execute(
                "SELECT role,memory_input FROM "
                "normalized_messages WHERE role='platform_summary'"
            ).fetchone()
            assert tuple(source) == ("platform_summary", 0)
            assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


async def test_http_multi_zip_upload_creates_one_local_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from everos.entrypoints.memo_vault.app import app

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    offline_manifest = json.dumps(
        {
            "version": "1.0",
            "total_files": 2,
            "data_files": [
                {
                    "category": category,
                    "part": 0,
                    "batch_index": index,
                    "filename": filename,
                    "export_url": f"https://claude.ai/export/{filename}?secret=not-used",
                }
                for index, (category, filename) in enumerate(
                    [
                        ("users", "users-000.zip"),
                        ("conversations", "conversations-000.zip"),
                    ]
                )
            ],
        }
    ).encode()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        created = await client.post(
            "/api/v1/import-jobs/local",
            data={"project_id": "personal"},
            files=[
                ("manifest", ("manifest.json", offline_manifest, "application/json")),
                (
                    "files",
                    (
                        "users-000.zip",
                        archive(
                            {
                                "users.json": [{"uuid": "account-1"}],
                            }
                        ),
                        "application/zip",
                    ),
                ),
                (
                    "files",
                    (
                        "conversations-000.zip",
                        archive(
                            {
                                "conversations.json": [
                                    {
                                        "uuid": "chat-1",
                                        "chat_messages": [
                                            {
                                                "uuid": "msg-1",
                                                "sender": "human",
                                                "text": "hello",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ),
                        "application/zip",
                    ),
                ),
            ],
        )
        assert created.status_code == 202, created.text
        job = created.json()["data"]
        assert job["input_kind"] == "local_bundle"
        assert job["part_count"] == 2
        assert job["manifest_verified"] is True
        for _ in range(100):
            import asyncio

            status = (await client.get(f"/api/v1/import-jobs/{job['id']}")).json()[
                "data"
            ]
            if status["status"] in {"ready_for_preview", "failed"}:
                break
            await asyncio.sleep(0.01)
        assert status["status"] == "ready_for_preview", status
        assert len((await client.get("/api/v1/import-jobs")).json()["data"]) == 1


def test_manifest_resume_preserves_completed_part_and_replacement(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.export_downloader import ExportDownloader
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    calls = []
    payloads = {
        "metadata.zip": archive({"users.json": [{"uuid": "a"}]}),
        "conversations.zip": sample(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        calls.append(name)
        return (
            httpx.Response(403)
            if name == "conversations.zip"
            else httpx.Response(200, content=payloads[name])
        )

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    downloader = ExportDownloader(transport=httpx.MockTransport(handler))
    manager = ImportJobManager(store, ImportService(store), downloader)
    manifest = {
        "version": "1.0",
        "total_files": 2,
        "data_files": [
            {
                "category": name,
                "part": 0,
                "batch_index": i,
                "filename": name,
                "export_url": f"https://claude.ai/export/{name}?secret=x",
            }
            for i, name in enumerate(payloads)
        ],
    }
    job = manager.create("manifest.json", json.dumps(manifest).encode(), "personal")
    manager.run(job["id"])
    assert manager.status(job["id"])["error_code"] == "CLAUDE_LINK_EXPIRED"
    assert "secret" not in json.dumps(manager.status(job["id"]))
    manager = ImportJobManager(store, ImportService(store), downloader)
    manager.supply_part(
        job["id"], "conversations.zip", archive({"conversations.json": []})
    )
    manager.run(job["id"])
    assert manager.status(job["id"])["status"] == "ready_for_preview"
    assert calls == ["metadata.zip", "conversations.zip"]


def test_claude_same_text_in_distinct_messages_is_not_deduplicated(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    service = ImportService(store)
    manager = ImportJobManager(store, service)
    payload = archive(
        {
            "conversations.json": [
                {
                    "uuid": "chat",
                    "chat_messages": [
                        {"uuid": "m1", "sender": "human", "text": "ok"},
                        {"uuid": "m2", "sender": "human", "text": "ok"},
                    ],
                }
            ]
        }
    )
    job = manager.create("export.zip", payload, "personal")
    manager.run(job["id"])
    preview = manager.preview(job["id"])
    assert preview["duplicate_message_count"] == 0
    service.commit(preview["preview_id"], "personal")
    payload = archive(
        {
            "conversations.json": [
                {
                    "uuid": "chat",
                    "chat_messages": [
                        {"uuid": "m1", "sender": "human", "text": "ok"},
                        {"uuid": "m2", "sender": "human", "text": "ok"},
                        {"uuid": "m3", "sender": "human", "text": "ok"},
                    ],
                }
            ]
        }
    )
    job = manager.create("new.zip", payload, "personal")
    manager.run(job["id"])
    assert manager.preview(job["id"])["duplicate_message_count"] == 2


def test_split_session_and_attachment_resolution(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export
    from everos.umif.validator import validate_package

    paths = []
    for i in range(2):
        p = tmp_path / f"conversations-{i}.zip"
        p.write_bytes(
            archive(
                {
                    "conversations.json": [
                        {
                            "uuid": "chat",
                            "chat_messages": [
                                {
                                    "uuid": f"m{i}",
                                    "sender": "human",
                                    "text": str(i),
                                    "attachments": [{"file_name": "hello.txt"}],
                                }
                            ],
                        }
                    ],
                    **({"files/hello.txt": b"hello"} if i == 0 else {}),
                }
            )
        )
        paths.append(p)
    package = convert_claude_export(paths, "personal")
    assert len(package.sessions) == 1
    assert len(package.events) == 2
    assert [e.sequence for e in package.events] == [0, 1]
    assert all(
        any(p.get("asset_id") for p in e.payload["content_parts"])
        for e in package.events
    )
    assert validate_package(package).valid


def test_download_logs_do_not_contain_signed_urls(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from everos.entrypoints.memo_vault.export_downloader import ExportDownloader

    with caplog.at_level(logging.DEBUG):
        ExportDownloader(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=sample())
            )
        ).download(
            "https://claude.ai/export/a/download/b?secret=never-log-this",
            tmp_path / "e.zip",
        )
    assert "never-log-this" not in caplog.text


def test_complete_download_without_marker_is_recovered_without_network(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.export_downloader import ExportDownloader

    target = tmp_path / "e.zip"
    target.write_bytes(sample())
    calls = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403)

    ExportDownloader(transport=httpx.MockTransport(handler)).download(
        "https://claude.ai/export/a/download/b", target
    )
    assert calls == []


def test_all_source_project_references_require_explicit_merge(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    manager = ImportJobManager(store, ImportService(store))
    payload = archive(
        {
            "conversations.json": [
                {"uuid": f"c{i}", "project_uuid": f"p{i}", "chat_messages": []}
                for i in range(2)
            ]
        }
    )
    job = manager.create("export.zip", payload, "personal")
    manager.run(job["id"])
    with pytest.raises(ImportWorkflowError) as error:
        manager.preview(job["id"])
    assert error.value.code == "CLAUDE_PROJECT_MAPPING_REQUIRED"
    assert (
        manager.preview(job["id"], confirm_project_merge=True)["conversation_count"]
        == 2
    )


async def test_assertion_edit_and_resume_committed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from everos.entrypoints.memo_vault.app import app

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        manager = app.state.import_jobs
        job = manager.create(
            "export.zip",
            archive({"memories.json": [{"uuid": "long", "content": "x" * 5000}]}),
            "personal",
        )
        manager.run(job["id"])
        preview = manager.preview(job["id"])
        result = app.state.import_service.commit(preview["preview_id"], "personal")
        batch_id = result["batch_id"]
        assert manager.status(job["id"])["status"] == "committed"
        assert manager.status(job["id"])["batch_id"] == batch_id
        content = (
            await client.get(f"/api/v1/imports/{batch_id}/claude-content")
        ).json()["data"]
        record_id = content["assertions"][0]["record_id"]
        response = await client.post(
            f"/api/v1/imports/{batch_id}/assertions/{record_id}/review",
            json={"content": "A reviewed summary."},
        )
        assert response.status_code == 200, response.text
        candidates = (await client.get("/api/v1/candidates")).json()["data"][
            "candidates"
        ]
        assert candidates[0]["kind"] == "preference"
        assert candidates[0]["metadata"]["origin_kind"] == "platform_summary"
        assert candidates[0]["content"] == "A reviewed summary."


def test_overlapping_saved_memories_are_deduplicated(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export
    from everos.umif.validator import validate_package

    paths = []
    for index in range(2):
        path = tmp_path / f"memories-{index}.zip"
        path.write_bytes(
            archive({"memories.json": [{"uuid": "same", "content": "Summary"}]})
        )
        paths.append(path)
    package = convert_claude_export(paths, "personal")
    assert len(package.optional_records["memories.jsonl"]) == 1
    assert validate_package(package).valid


def test_shutdown_interruption_is_recoverable_but_user_cancel_is_not(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    manager = ImportJobManager(store, ImportService(store))
    job = manager.create("export.zip", sample(), "personal")
    manager.stopping.set()
    manager.run(job["id"])
    assert manager.status(job["id"])["status"] == "queued"
    manager = ImportJobManager(store, ImportService(store))
    assert manager.recover() == [job["id"]]
    manager.cancel(job["id"])
    assert manager.recover() == []


def test_expired_part_does_not_prevent_caching_other_parts(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.export_downloader import ExportDownloader
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return (
            httpx.Response(403)
            if len(calls) == 1
            else httpx.Response(200, content=sample())
        )

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    manager = ImportJobManager(
        store,
        ImportService(store),
        ExportDownloader(transport=httpx.MockTransport(handler)),
    )
    manifest = {
        "version": "1.0",
        "total_files": 2,
        "data_files": [
            {
                "category": str(i),
                "part": 0,
                "batch_index": i,
                "filename": f"{i}.zip",
                "export_url": f"https://claude.ai/export/{i}",
            }
            for i in range(2)
        ],
    }
    job = manager.create("manifest.json", json.dumps(manifest).encode(), "personal")
    manager.run(job["id"])
    assert len(calls) == 2
    assert (manager.path(job["id"]) / "1.zip").is_file()
    assert manager.status(job["id"])["status"] == "failed"


async def test_knowledge_delivery_is_scoped_idempotent_and_reconciles_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from everos.entrypoints.memo_vault.app import app

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        manager = app.state.import_jobs
        job = manager.create("export.zip", sample(), "personal")
        manager.run(job["id"])
        preview = manager.preview(job["id"])
        batch = app.state.import_service.commit(preview["preview_id"], "personal")
        path = f"/api/v1/imports/{batch['batch_id']}"
        records = (await client.get(path + "/claude-content")).json()["data"][
            "documents"
        ]
        calls = []
        uploaded = {}
        fail = False

        def remote(request: httpx.Request) -> httpx.Response:
            nonlocal fail
            if request.method == "POST":
                calls.append(1)
                body = request.read()
                assert b'name="project_id"\r\n\r\npersonal' in body
                import re

                uploaded["filename"] = (
                    re.search(rb'filename="([^"]+)"', body).group(1).decode()
                )
                uploaded["title"] = records[1 if fail else 0]["title"]
                if fail:
                    raise httpx.ReadTimeout("unknown response")
                return httpx.Response(201, json={"data": {"doc_id": "d_" + "a" * 12}})
            assert request.url.params["project_id"] == "personal"
            if request.url.path.endswith("/documents"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "documents": [
                                {"doc_id": "d_" + "b" * 12, "title": uploaded["title"]}
                            ],
                            "total": 1,
                        }
                    },
                )
            return httpx.Response(
                200, json={"data": {"source_name": uploaded["filename"]}}
            )

        app.state.everos_adapter.transport = httpx.MockTransport(remote)
        record_id = records[0]["record_id"]
        first = await client.post(
            path + f"/documents/{record_id}/deliver", json={"confirmed": True}
        )
        assert first.status_code == 200, first.text
        second = await client.post(
            path + f"/documents/{record_id}/deliver", json={"confirmed": True}
        )
        assert second.status_code == 200 and len(calls) == 1
        fail = True
        record_id = records[1]["record_id"]
        unknown = await client.post(
            path + f"/documents/{record_id}/deliver", json={"confirmed": True}
        )
        assert unknown.status_code == 502
        blocked = await client.post(
            path + f"/documents/{record_id}/deliver", json={"confirmed": True}
        )
        assert blocked.status_code == 409 and len(calls) == 2
        reconciled = await client.post(path + f"/documents/{record_id}/reconcile")
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["data"]["status"] == "delivered"


def test_explicit_artifact_becomes_document(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export

    path = tmp_path / "export.zip"
    path.write_bytes(
        archive(
            {
                "conversations.json": [
                    {
                        "uuid": "chat",
                        "chat_messages": [
                            {
                                "uuid": "m1",
                                "sender": "assistant",
                                "content": [
                                    {
                                        "type": "artifact",
                                        "id": "a1",
                                        "title": "Notes",
                                        "content": "An exported artifact.",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )
    package = convert_claude_export([path], "personal")
    document = package.optional_records["documents.jsonl"][0]
    assert document.payload["kind"] == "artifact"
    assert document.payload["text"] == "An exported artifact."


def test_original_evidence_tampering_blocks_commit(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError
    from everos.entrypoints.memo_vault.import_jobs import ImportJobManager
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    service = ImportService(store)
    manager = ImportJobManager(store, service)
    job = manager.create("export.zip", sample(), "personal")
    manager.run(job["id"])
    preview = manager.preview(job["id"])
    source = service.preview_dir / f"{preview['preview_id']}.source" / "export.zip"
    source.write_bytes(b"tampered")
    with pytest.raises(ImportWorkflowError) as error:
        service.commit(preview["preview_id"], "personal")
    assert error.value.code == "PREVIEW_CORRUPTED"


def test_binary_project_document_references_exported_asset(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export

    path = tmp_path / "export.zip"
    path.write_bytes(
        archive(
            {
                "projects.json": [
                    {
                        "uuid": "p1",
                        "docs": [{"uuid": "pdf1", "filename": "reference.pdf"}],
                    }
                ],
                "files/reference.pdf": b"%PDF-1.4\nexample",
            }
        )
    )
    package = convert_claude_export(
        [path], "personal", source_account_id="account-test"
    )
    document = package.optional_records["documents.jsonl"][0]
    assert document.payload["kind"] == "project_knowledge"
    asset = next(
        a
        for a in package.optional_records["assets.jsonl"]
        if a.record_id == document.payload["asset_id"]
    )
    assert package.blobs[asset.payload["blob_path"]].startswith(b"%PDF-1.4")


def test_claude_domain_migration_redirect_downloads_complete_zip(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.claude_manifest import validate_download_url
    from everos.entrypoints.memo_vault.export_downloader import ExportDownloader
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError

    hosts = []
    payload = sample()

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "claude.ai":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://claude.com/export/a/download/b?signature=private"
                },
            )
        return httpx.Response(200, content=payload)

    target = tmp_path / "export.zip"
    ExportDownloader(transport=httpx.MockTransport(handler)).download(
        "https://claude.ai/export/a/download/b?signature=private", target
    )
    assert hosts == ["claude.ai", "claude.com"]
    assert target.read_bytes() == payload
    assert (
        validate_download_url("https://claude.com/export/a/download/b") == "claude.com"
    )
    with pytest.raises(ImportWorkflowError):
        validate_download_url("https://claude.com.evil.test/export/a", redirect=True)


def test_export_webpage_is_not_reported_as_downloaded_zip(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.export_downloader import ExportDownloader
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError

    target = tmp_path / "export.zip"
    downloader = ExportDownloader(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html>Export unavailable</html>",
            )
        )
    )
    with pytest.raises(ImportWorkflowError) as error:
        downloader.download("https://claude.com/export/a/download/b", target)
    assert error.value.code == "CLAUDE_EXPORT_NOT_AVAILABLE"
    assert not target.exists()


def test_system_role_preserves_its_own_principal_and_is_not_projected(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export
    from everos.entrypoints.memo_vault.umif_projection import project_for_vault

    path = tmp_path / "export.zip"
    path.write_bytes(
        archive(
            {
                "conversations.json": [
                    {
                        "uuid": "chat",
                        "chat_messages": [
                            {
                                "uuid": "system-1",
                                "sender": "system",
                                "text": "Project context",
                            },
                            {"uuid": "user-1", "sender": "human", "text": "Hello"},
                        ],
                    }
                ]
            }
        )
    )
    package = convert_claude_export([path], "personal")
    system = package.events[0]
    author = next(
        p
        for p in package.principals
        if p.record_id == system.payload["author_principal_id"]
    )
    assert author.payload["kind"] == "system"
    assert [m.role for m in project_for_vault(package).messages] == ["user"]


def test_region_restriction_redirect_has_specific_safe_error(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.export_downloader import ExportDownloader
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError

    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host)
        return httpx.Response(
            302, headers={"Location": "https://claude.com/app-unavailable-in-region"}
        )

    target = tmp_path / "export.zip"
    with pytest.raises(ImportWorkflowError) as error:
        ExportDownloader(transport=httpx.MockTransport(handler)).download(
            "https://claude.ai/export/a/download/private-token", target
        )
    assert error.value.code == "CLAUDE_REGION_UNAVAILABLE"
    assert "private-token" not in str(error.value)
    assert requested == ["claude.ai"]
    assert not target.exists()


def test_directory_export_and_memory_files_match_live_shape(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export
    from everos.umif.validator import validate_package

    path = tmp_path / "export.zip"
    path.write_bytes(
        archive(
            {
                "users.json": [
                    {"uuid": "account-live", "email_address": "private@example.test"}
                ],
                "login_history.json": {"login_events": [{"ip_address": "192.0.2.42"}]},
                "conversations.json": [
                    {
                        "uuid": "chat-live",
                        "account": {"uuid": "account-live"},
                        "chat_messages": [
                            {
                                "uuid": "message-live",
                                "sender": "human",
                                "text": "Use uv.",
                            }
                        ],
                    }
                ],
                "projects/project-live.json": {
                    "uuid": "project-live",
                    "name": "Example",
                    "prompt_template": "Project context",
                    "docs": [
                        {
                            "uuid": "doc-live",
                            "filename": "notes.md",
                            "content": "Reference",
                        }
                    ],
                },
                "memories/account-live.json": {
                    "account_uuid": "account-live",
                    "memory_files": [
                        {
                            "path": "memory.md",
                            "content": "The user prefers uv.",
                            "updated_at": "2026-10-24T00:00:00Z",
                        }
                    ],
                },
            }
        )
    )
    package = convert_claude_export([path], "personal")
    assert len(package.optional_records["documents.jsonl"]) == 2
    memories = package.optional_records["memories.jsonl"]
    assert len(memories) == 1
    assert memories[0].payload["text"] == "The user prefers uv."
    assert memories[0].timestamp_ms == 1792800000000
    assert memories[0].provenance["json_pointer"] == "/memory_files/0"
    assert (
        package.optional_records["documents.jsonl"][0].provenance["json_pointer"] == ""
    )
    assert all(
        a.payload["filename"] != "login_history.json"
        for a in package.optional_records["assets.jsonl"]
    )
    assert package.import_report["skipped_event_counts"] == {"account_metadata": 1}
    assert validate_package(package).valid


def test_memory_file_identity_does_not_change_with_text(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export

    path = tmp_path / "export.zip"

    def convert(text: str):
        path.write_bytes(
            archive(
                {
                    "memories/owner.json": {
                        "account_uuid": "owner",
                        "memory_files": [{"path": "memory.md", "content": text}],
                    }
                }
            )
        )
        return convert_claude_export([path], "personal").optional_records[
            "memories.jsonl"
        ][0]

    first, second = convert("First summary."), convert("Updated summary.")
    assert first.record_id == second.record_id
    assert first.provenance["content_sha256"] != second.provenance["content_sha256"]


async def test_knowledge_provider_rejection_is_not_an_unknown_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from everos.entrypoints.memo_vault.app import app

    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        manager = app.state.import_jobs
        job = manager.create("export.zip", sample(), "personal")
        manager.run(job["id"])
        preview = manager.preview(job["id"])
        batch = app.state.import_service.commit(preview["preview_id"], "personal")
        path = "/api/v1/imports/" + batch["batch_id"]
        record = (await client.get(path + "/claude-content")).json()["data"][
            "documents"
        ][0]
        app.state.everos_adapter.transport = httpx.MockTransport(
            lambda _: httpx.Response(
                422,
                json={
                    "error": {
                        "code": "PROVIDER_NOT_CONFIGURED",
                        "message": "Provider 'rerank' (required by knowledge) "
                        "is not configured.",
                    }
                },
            )
        )
        response = await client.post(
            path + "/documents/" + record["record_id"] + "/deliver",
            json={"confirmed": True},
        )
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "EVEROS_RERANK_NOT_CONFIGURED"
        content = (await client.get(path + "/claude-content")).json()["data"]
        assert content["documents"][0]["status"] == "delivery_rejected"


def test_direct_zip_preview_snapshot_ignores_json_dictionary_key_order(
    tmp_path: Path,
) -> None:
    from everos.entrypoints.memo_vault.imports import ImportService
    from everos.entrypoints.memo_vault.storage import VaultStore

    store = VaultStore(tmp_path / "vault")
    store.initialize()
    service = ImportService(store)
    preview = service.preview("export.zip", "claude", sample(), project_id="personal")
    committed = service.commit(preview["preview_id"], "personal")
    assert committed["message_count"] == 2


def test_project_only_archive_requires_export_owner_context(tmp_path: Path) -> None:
    from everos.entrypoints.memo_vault.claude_export import convert_claude_export
    from everos.entrypoints.memo_vault.import_contracts import ImportWorkflowError

    path = tmp_path / "projects.zip"
    path.write_bytes(
        archive(
            {
                "projects/project.json": {
                    "uuid": "project",
                    "creator": {"uuid": "other-owner"},
                    "docs": [],
                }
            }
        )
    )
    with pytest.raises(ImportWorkflowError) as error:
        convert_claude_export([path], "personal")
    assert error.value.code == "CLAUDE_ACCOUNT_REQUIRED"
