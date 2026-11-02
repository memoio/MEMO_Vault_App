from __future__ import annotations

import asyncio
import httpx


async def test_background_knowledge_zip_returns_early_and_can_be_downloaded(tmp_path, monkeypatch):
    from everos.entrypoints.memo_vault.app import app
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    started, release = asyncio.Event(), asyncio.Event()
    async def remote(request):
        started.set()
        await release.wait()
        return httpx.Response(200, content=b"zip test bytes", headers={"content-type": "application/zip"})
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer " + app.state.owner_security.secret
        app.state.everos_adapter.transport = httpx.MockTransport(remote)
        response = await asyncio.wait_for(client.post('/api/v1/knowledge-export/zip/start', json={"project_id":"personal","doc_ids":["d_" + "a" * 12]}), 2)
        assert response.status_code == 202
        job = response.json()['data']['job_id']
        await asyncio.wait_for(started.wait(), 2)
        release.set()
        for _ in range(100):
            status = (await client.get(f'/api/v1/knowledge-export/zip/jobs/{job}')).json()['data']
            if status['status'] == 'completed': break
            await asyncio.sleep(0.01)
        assert status['status'] == 'completed'
        result = await client.get(f'/api/v1/knowledge-export/zip/jobs/{job}/download')
        assert result.status_code == 200
        assert result.content == b'zip test bytes'


async def test_background_export_restart_marks_running_job_failed(tmp_path, monkeypatch):
    from everos.entrypoints.memo_vault.app import app
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    started = asyncio.Event()
    async def remote(request):
        started.set()
        await asyncio.Event().wait()
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer " + app.state.owner_security.secret
        app.state.everos_adapter.transport = httpx.MockTransport(remote)
        response = await client.post('/api/v1/knowledge-export/zip/start', json={"project_id":"personal","doc_ids":["d_" + "a" * 12]})
        job = response.json()['data']['job_id']
        await asyncio.wait_for(started.wait(), 2)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer " + app.state.owner_security.secret
        assert (await client.get(f'/api/v1/knowledge-export/zip/jobs/{job}')).json()['data']['status'] == 'failed'
        assert (await client.get(f'/api/v1/knowledge-export/zip/jobs/{job}/download')).status_code == 404


async def test_background_export_failure_is_reported(tmp_path, monkeypatch):
    from everos.entrypoints.memo_vault.app import app
    monkeypatch.setenv("MEMO_VAULT_DATA_DIR", str(tmp_path / "vault"))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer " + app.state.owner_security.secret
        app.state.everos_adapter.transport = httpx.MockTransport(lambda _: httpx.Response(503))
        response = await client.post('/api/v1/knowledge-export/zip/start', json={"project_id":"personal","doc_ids":["d_" + "a" * 12]})
        job = response.json()['data']['job_id']
        for _ in range(100):
            status = (await client.get(f'/api/v1/knowledge-export/zip/jobs/{job}')).json()['data']
            if status['status'] == 'failed': break
            await asyncio.sleep(.01)
        assert status['status'] == 'failed'
        assert status['error_message']
        assert (await client.get(f'/api/v1/knowledge-export/zip/jobs/{job}/download')).status_code == 404
