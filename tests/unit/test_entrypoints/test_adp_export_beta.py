import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from everos.entrypoints.memo_vault.adp_converter import convert, read_records
from everos.entrypoints.memo_vault.adp_export import (
    _claude_raw_trajectory,
    redact_trajectory,
    redact_with_details,
)
from everos.entrypoints.memo_vault.app import app


def valid_rows():
    fixture = (
        Path(__file__).parents[2] / "fixtures/memo_vault/deepseek_harness_v4.jsonl"
    )
    rows, _ = read_records(fixture)
    rows[3]["data"]["turn"] = 1
    rows[3]["data"]["step"] = 1
    rows[3]["data"]["message"]["content"][2]["id"] = "call-1"
    rows[5]["data"]["turn"] = 1
    rows[5]["data"]["step"] = 1
    return rows


def test_deepseek_session_preserves_real_tool_pairs():
    trajectory, _, report = convert(valid_rows())
    calls = [item for item in trajectory["content"] if item["class_"] == "api_action"]
    observations = [
        item for item in trajectory["content"] if item.get("source") == "environment"
    ]
    assert calls
    assert len(calls) == len(observations) == report["tool_calls"]


def test_missing_tool_result_is_rejected():
    rows = valid_rows()
    rows = [row for row in rows if row.get("type") != "tool/result"]
    with pytest.raises(ValueError, match="missing result"):
        convert(rows)


def test_redaction_covers_nested_metadata_without_truncation():
    value = {
        "content": "x" * 3000 + " password=secret",
        "details": {"api_key": "secret"},
    }
    result, count = redact_trajectory(value)
    assert result["content"] == "x" * 3000 + " password=[REDACTED]"
    assert result["details"]["api_key"] == "[REDACTED]"
    assert count == 2
    assert value["details"]["api_key"] == "secret"


def test_redaction_covers_json_encoded_arguments():
    result, _ = redact_trajectory({"raw_arguments": '{"password":"unique_value"}'})
    assert "unique_value" not in result["raw_arguments"]


def test_numeric_usage_token_counts_are_not_credentials():
    value = {
        "details": {
            "assistant_steps": [
                {
                    "metadata": {
                        "usage": {
                            "inputTokens": 123,
                            "outputTokens": 45,
                            "access_token": "private-secret",
                        }
                    }
                }
            ]
        }
    }
    redacted, details = redact_with_details(value)
    usage = redacted["details"]["assistant_steps"][0]["metadata"]["usage"]
    assert usage["inputTokens"] == 123
    assert usage["outputTokens"] == 45
    assert usage["access_token"] == "[REDACTED]"
    assert len(details) == 1
    assert (
        redact_trajectory({"inputTokens": "private-secret"})[0]["inputTokens"]
        == "[REDACTED]"
    )


def test_redaction_details_group_paths_and_show_only_masked_context():
    value = {
        "content": [
            {
                "class_": "text_observation",
                "source": "user",
                "content": "password=alpha",
            },
            {"class_": "api_action", "kwargs": {"api_key": "bravo"}},
            {
                "class_": "text_observation",
                "source": "environment",
                "content": "token=charlie",
            },
        ],
        "details": {"raw_arguments": '{"password":"delta"}'},
    }
    redacted, details = redact_with_details(value)
    assert [entry["category"] for entry in details] == [
        "message",
        "tool_arguments",
        "tool_result",
        "metadata",
    ]
    assert len(details) == 4
    assert all("[REDACTED]" in entry["snippet"] for entry in details)
    assert details[0]["path"] == "/content/0/content"
    assert all(
        secret not in json.dumps(details)
        for secret in ("alpha", "bravo", "charlie", "delta")
    )
    assert redact_trajectory(value) == (redacted, len(details))


def test_claude_preserves_interleaved_tool_blocks():
    conversation = {
        "uuid": "c1",
        "chat_messages": [
            {
                "sender": "assistant",
                "content": [
                    {"type": "text", "text": "before"},
                    {
                        "type": "tool_use",
                        "id": "x",
                        "name": "search",
                        "input": {"query": "test"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "x",
                        "content": [{"type": "knowledge", "text": "found"}],
                    },
                    {"type": "text", "text": "after"},
                ],
            }
        ],
    }
    result = _claude_raw_trajectory(conversation)
    assert [v["class_"] for v in result["content"]] == [
        "message_action",
        "api_action",
        "text_observation",
        "message_action",
    ]
    assert result["content"][2]["content"] == "found"
    assert result["details"]["tool_links"][0]["observation_index"] == 2


@pytest.mark.asyncio
async def test_imported_deepseek_validate_consent_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    rows = valid_rows()
    rows[1]["data"]["content"][0]["text"] = "password=unique_secret_value"
    raw = b"".join(
        (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows
    )
    async with app.router.lifespan_context(app):
        service = app.state.import_service
        preview = service.preview(
            "session.jsonl", "deepseek_harness", raw, project_id="personal"
        )
        service.commit(preview["preview_id"], "personal")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": "Bearer " + app.state.owner_security.secret},
        ) as client:
            traces = (await client.get("/api/v1/adp/traces")).json()["data"]
            assert len(traces) == 1
            trace_id = traces[0]["id"]
            assert traces[0]["tool_call_count"] == 1
            assert traces[0]["consent"] is False
            assert (
                await client.post(
                    "/api/v1/adp/consent",
                    json={"trace_ids": [trace_id], "consent": True},
                )
            ).status_code == 200
            response = await client.post(
                "/api/v1/adp/validate", json={"trace_ids": [trace_id]}
            )
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["valid"] is True
            assert "unique_secret_value" not in json.dumps(data, ensure_ascii=False)
            assert data["traces"][0]["redaction_count"] == len(
                data["traces"][0]["redactions"]
            )
            assert any(
                "[REDACTED]" in entry["snippet"]
                for entry in data["traces"][0]["redactions"]
            )
            assert (
                data["traces"][0]["trajectory"]["details"]["training_consent"] is True
            )
            output = await client.post(
                "/api/v1/adp/export",
                json={
                    "trace_ids": [trace_id],
                    "preview_sha256": data["preview_sha256"],
                },
            )
            assert output.status_code == 200
            assert json.loads(output.text)["details"]["training_consent"] is True
            assert "unique_secret_value" not in output.text
            atif_check = await client.post(
                "/api/v1/adp/validate", json={"trace_ids": [trace_id], "format": "atif"}
            )
            atif = atif_check.json()["data"]
            assert atif["valid"] is True
            assert atif["traces"][0]["trajectory"]["schema_version"] == "ATIF-v1.7"
            assert "unique_secret_value" not in json.dumps(atif, ensure_ascii=False)
            atif_output = await client.post(
                "/api/v1/adp/export",
                json={
                    "trace_ids": [trace_id],
                    "format": "atif",
                    "preview_sha256": atif["preview_sha256"],
                },
            )
            assert atif_output.status_code == 200
            assert json.loads(atif_output.text)["extra"]["training_consent"] is True
            assert (
                await client.post(
                    "/api/v1/adp/export",
                    json={
                        "trace_ids": [trace_id],
                        "format": "atif",
                        "preview_sha256": data["preview_sha256"],
                    },
                )
            ).status_code == 409
            await client.post(
                "/api/v1/adp/consent", json={"trace_ids": [trace_id], "consent": False}
            )
            stale = await client.post(
                "/api/v1/adp/export",
                json={
                    "trace_ids": [trace_id],
                    "preview_sha256": data["preview_sha256"],
                },
            )
            assert stale.status_code == 409
            with app.state.vault_store.connect() as db:
                archived_path = db.execute(
                    "SELECT raw_relpath FROM import_batches"
                ).fetchone()[0]
            (app.state.vault_store.data_dir / archived_path).write_bytes(b"changed")
            invalid = await client.post(
                "/api/v1/adp/validate", json={"trace_ids": [trace_id]}
            )
            assert invalid.json()["data"]["valid"] is False
            assert "哈希" in invalid.json()["data"]["traces"][0]["error"]
            assert (
                await client.post(
                    "/api/v1/adp/validate", json={"trace_ids": [trace_id, trace_id]}
                )
            ).status_code == 422


@pytest.mark.asyncio
async def test_imported_claude_keeps_interleaved_source_order(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    conversation = {
        "uuid": "claude-session",
        "name": "session",
        "chat_messages": [
            {
                "uuid": "m1",
                "sender": "human",
                "content": [{"type": "text", "text": "question"}],
            },
            {
                "uuid": "m2",
                "sender": "assistant",
                "content": [
                    {"type": "text", "text": "before"},
                    {
                        "type": "tool_use",
                        "id": "x",
                        "name": "search",
                        "input": {"query": "test"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "x",
                        "content": [{"type": "text", "text": "result"}],
                    },
                    {"type": "text", "text": "after"},
                ],
            },
        ],
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("conversations.json", json.dumps([conversation]))
    async with app.router.lifespan_context(app):
        service = app.state.import_service
        preview = service.preview(
            "claude.zip", "claude", stream.getvalue(), project_id="personal"
        )
        service.commit(preview["preview_id"], "personal")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": "Bearer " + app.state.owner_security.secret},
        ) as client:
            traces = (await client.get("/api/v1/adp/traces")).json()["data"]
            assert len(traces) == 1 and traces[0]["source"] == "claude"
            assert traces[0]["title"] == "session"
            validation = (
                await client.post(
                    "/api/v1/adp/validate", json={"trace_ids": [traces[0]["id"]]}
                )
            ).json()["data"]
            assert validation["valid"]
            items = validation["traces"][0]["trajectory"]["content"]
            assert [item["class_"] for item in items] == [
                "text_observation",
                "message_action",
                "api_action",
                "text_observation",
                "message_action",
            ]
            assert items[-1]["content"] == "after"
            validation = (
                await client.post(
                    "/api/v1/adp/validate",
                    json={"trace_ids": [traces[0]["id"]], "format": "atif"},
                )
            ).json()["data"]
            assert validation["valid"]
            steps = validation["traces"][0]["trajectory"]["steps"]
            assert [step["message"] for step in steps] == [
                "question",
                "before",
                "",
                "after",
            ]
            assert steps[2]["tool_calls"][0]["tool_call_id"] == "x"
            assert steps[2]["observation"]["results"][0]["source_call_id"] == "x"
            result = await client.post(
                "/api/v1/adp/export",
                json={
                    "trace_ids": [traces[0]["id"]],
                    "format": "atif",
                    "preview_sha256": validation["preview_sha256"],
                },
            )
            assert result.status_code == 200
            assert json.loads(result.text)["agent"]["name"] == "claude"
