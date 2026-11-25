"""Unit tests for Data Vault Web archive listing, HTML download, single delete, and batch ZIP export."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from everos.entrypoints.memo_vault.app import app


def _web_payload(
    request_id: str,
    title: str,
    url: str,
    domain: str,
    mode: str = "page",
) -> dict:
    return {
        "schema_version": "memo-plugins.capture/1",
        "request_id": request_id,
        "record_type": "web_page",
        "captured_at": "2026-11-24T02:00:00.000Z",
        "client": {"name": "memo-plugins", "version": "0.1.0"},
        "source": {
            "kind": "web",
            "url": url,
            "domain": domain,
            "title": title,
            "lang": "zh-CN",
        },
        "capture": {
            "mode": mode,
            "scope": "当前页已加载内容：1 个标题 · 1 张图片",
            "status": "complete",
            "truncated": False,
            "extraction": "structured",
            "total_units": 2,
            "selected_units": 2,
        },
        "blocks": [
            {"id": "b1", "order": 1, "type": "heading", "level": 1, "text": title},
            {
                "id": "b2",
                "order": 2,
                "type": "image",
                "src": "https://cdn.example.com/arch.png",
                "alt": "Architecture",
                "caption": "Fig 1",
                "width": 640,
                "height": 360,
            },
        ],
    }


def test_data_vault_export_list_download_zip_and_delete(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        owner_headers = {"Authorization": f"Bearer {app.state.owner_security.secret}"}

        r1 = client.post(
            "/api/v1/plugin/data-vault/records",
            json={
                "project_id": "personal",
                "payload": _web_payload("req-1", "Hugging Face Papers", "https://huggingface.co/papers", "huggingface.co"),
            },
        )
        assert r1.status_code == 201, r1.text
        rec1 = r1.json()["data"]["record_id"]

        r2 = client.post(
            "/api/v1/plugin/data-vault/records",
            json={
                "project_id": "personal",
                "payload": _web_payload("req-2", "Selected Excerpt", "https://docs.python.org/3/", "docs.python.org", mode="selection"),
            },
        )
        assert r2.status_code == 201, r2.text
        rec2 = r2.json()["data"]["record_id"]

        # List all records
        list_res = client.get("/api/v1/data-vault/records?project_id=all", headers=owner_headers)
        assert list_res.status_code == 200, list_res.text
        body = list_res.json()["data"]
        assert body["total"] == 2
        assert {item["record_id"] for item in body["records"]} == {rec1, rec2}

        # Filter by domain & capture_mode
        filtered = client.get(
            "/api/v1/data-vault/records?domain=python&capture_mode=selection",
            headers=owner_headers,
        )
        assert filtered.status_code == 200
        assert filtered.json()["data"]["total"] == 1
        assert filtered.json()["data"]["records"][0]["record_id"] == rec2

        # Detail endpoint returns block counts including image
        detail = client.get(f"/api/v1/data-vault/records/{rec1}", headers=owner_headers)
        assert detail.status_code == 200
        assert detail.json()["data"]["block_counts"] == {"heading": 1, "image": 1}

        # HTML preview & download includes rendered <figure class="memo-image">
        html_res = client.get(f"/api/v1/data-vault/records/{rec1}/html?download=1", headers=owner_headers)
        assert html_res.status_code == 200
        assert 'attachment; filename="' in html_res.headers.get("content-disposition", "")
        assert 'class="memo-image"' in html_res.text
        assert "https://cdn.example.com/arch.png" in html_res.text

        # Batch ZIP export
        zip_res = client.post(
            "/api/v1/data-vault/records/export-zip",
            headers=owner_headers,
            json={"record_ids": [rec1, rec2], "include_payload": True},
        )
        assert zip_res.status_code == 200, zip_res.text
        assert zip_res.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(zip_res.content)) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert f"{rec1}/page.html" in names
            assert f"{rec1}/payload.json" in names
            assert f"{rec2}/page.html" in names
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            assert manifest["record_count"] == 2

        # Delete single record removes both DB row and disk folder
        assert (tmp_path / "vault" / "data_vault" / rec1).is_dir()
        del_res = client.delete(f"/api/v1/data-vault/records/{rec1}", headers=owner_headers)
        assert del_res.status_code == 200
        assert del_res.json()["data"]["deleted"] is True
        assert not (tmp_path / "vault" / "data_vault" / rec1).exists()

        after_list = client.get("/api/v1/data-vault/records?project_id=all", headers=owner_headers)
        assert after_list.json()["data"]["total"] == 1
        assert after_list.json()["data"]["records"][0]["record_id"] == rec2

        missing_res = client.get(f"/api/v1/data-vault/records/{rec1}", headers=owner_headers)
        assert missing_res.status_code == 404
