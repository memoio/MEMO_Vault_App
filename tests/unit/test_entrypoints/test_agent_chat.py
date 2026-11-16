import asyncio

import pytest

from everos.entrypoints.memo_vault.agent_chat_service import (
    AgentChatStore,
    resolve_model,
)
from everos.entrypoints.memo_vault.storage import VaultStore


def test_model_pair_validation():
    assert resolve_model("qwen-1", "openhands") == "openhands"
    assert resolve_model("qwen-1", "swe") == "openhands"
    with pytest.raises(ValueError):
        resolve_model("../../bad", "default")


def test_session_project_history_and_retry_identity(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    session = store.create_session("personal")
    request = store.begin_message(
        session["id"], "hello world", "qwen-1", "openhands", "request-1"
    )
    assert store.begin_message(
        session["id"], "hello world", "qwen-1", "openhands", "request-1"
    )["duplicate"]
    store.finish_message(request["assistant_id"], "reply")
    detail = store.get_session(session["id"])
    assert [x["content"] for x in detail["messages"]] == ["hello world", "reply"]
    assert detail["messages"][1]["model_id"] == "qwen-1"
    assert store.list_sessions("personal", "hello")[0]["id"] == session["id"]
    with pytest.raises(KeyError):
        store.create_session("no-such-project")


@pytest.mark.asyncio
async def test_message_route_saves_actual_routing_and_output(tmp_path):
    from types import SimpleNamespace

    from everos.entrypoints.memo_vault.routes.agent_chat import NewMessage, send

    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    sid = store.create_session("personal")["id"]

    class Runtime:
        async def generate(self, agent, messages):
            assert agent == "swe" and messages[-1]["content"] == "inspect code"
            return "<function=bash>text only</function>"

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_chat_store=store, agent_runtime=Runtime(), agent_chat_tasks=set())
        )
    )
    result = await send(
        sid,
        NewMessage(
            content="inspect code", model_id="qwen-2", agent_id="default", request_id="a"
        ),
        request,
    )
    await asyncio.gather(*list(request.app.state.agent_chat_tasks))
    result = {"data": store.get_session(sid)}
    assert (
        result["data"]["messages"][-1]["content"]
        == "<function=bash>text only</function>"
    )
    assert result["data"]["messages"][-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_owner_http_auth_and_route_contract(tmp_path):
    import httpx
    from fastapi import FastAPI
    from everos.entrypoints.memo_vault.auth import OwnerSecurity, install_owner_security
    from everos.entrypoints.memo_vault.routes.agent_chat import router

    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    app = FastAPI()
    app.state.agent_chat_store = store
    app.state.owner_security = OwnerSecurity(
        tmp_path, allowed_origins=["http://vault.test"]
    )

    class Runtime:
        async def generate(self, agent, messages):
            return "generated:" + agent

    app.state.agent_runtime = Runtime()
    app.state.openhands_runtime = Runtime()
    app.state.agent_chat_tasks = set()
    app.include_router(router, prefix="/api/v1")
    install_owner_security(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://vault.test"
    ) as client:
        assert (await client.get("/api/v1/agent-chat/sessions")).status_code == 401
        token = app.state.owner_security.bootstrap_path.read_text().strip()
        login = await client.post(
            "/api/v1/auth/session",
            json={"token": token},
            headers={"Origin": "http://vault.test"},
        )
        csrf = {"X-Vault-CSRF": login.json()["csrf_token"]}
        assert (
            await client.post(
                "/api/v1/agent-chat/sessions", json={"project_id": "personal"}
            )
        ).status_code == 403
        created = await client.post(
            "/api/v1/agent-chat/sessions", json={"project_id": "personal"}, headers=csrf
        )
        assert created.status_code == 201
        sid = created.json()["data"]["id"]
        assert (
            await client.patch(
                f"/api/v1/agent-chat/sessions/{sid}", json={"title": "重命名"}
            )
        ).status_code == 403
        renamed = await client.patch(
            f"/api/v1/agent-chat/sessions/{sid}", json={"title": "重命名"}, headers=csrf
        )
        assert renamed.status_code == 200
        assert renamed.json()["data"]["title"] == "重命名"
        assert (
            await client.patch(
                f"/api/v1/agent-chat/sessions/{sid}", json={"title": "  "}, headers=csrf
            )
        ).status_code == 422
        request = {
            "content": "hello",
            "model_id": "qwen-1",
            "agent_id": "openhands",
            "request_id": "r1",
        }
        reply = await client.post(
            f"/api/v1/agent-chat/sessions/{sid}/messages", json=request, headers=csrf
        )
        assert reply.json()["data"]["messages"][-1]["status"] == "pending"
        await asyncio.gather(*list(app.state.agent_chat_tasks))
        assert store.get_session(sid)["messages"][-1]["content"] == "generated:openhands"
        assert (
            await client.post(
                f"/api/v1/agent-chat/sessions/{sid}/messages",
                json=request | {"request_id": "r2", "agent_id": "unknown"},
                headers=csrf,
            )
        ).status_code == 409
        assert (
            len(
                (await client.get(f"/api/v1/agent-chat/sessions/{sid}")).json()["data"][
                    "messages"
                ]
            )
            == 2
        )
        assert (
            await client.delete(f"/api/v1/agent-chat/sessions/{sid}")
        ).status_code == 403
        deleted = await client.delete(
            f"/api/v1/agent-chat/sessions/{sid}", headers=csrf
        )
        assert deleted.status_code == 200
        assert deleted.json()["data"]["deleted"] is True
        assert (
            await client.get(f"/api/v1/agent-chat/sessions/{sid}")
        ).status_code == 404
        assert (
            await client.delete(f"/api/v1/agent-chat/sessions/{sid}", headers=csrf)
        ).status_code == 404
        with vault.connect() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM agent_chat_messages WHERE session_id=?",
                    (sid,),
                ).fetchone()[0]
                == 0
            )


@pytest.mark.asyncio
async def test_failed_attempt_does_not_pollute_later_context(tmp_path):
    from types import SimpleNamespace
    from everos.entrypoints.memo_vault.routes.agent_chat import NewMessage, send

    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    sid = store.create_session("personal")["id"]

    class Runtime:
        async def generate(self, agent, messages):
            if messages[-1]["content"] == "bad":
                raise RuntimeError("offline")
            assert messages == [{"role": "user", "content": "good"}]
            return "ok"

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_chat_store=store, agent_runtime=Runtime(), agent_chat_tasks=set())
        )
    )
    for index, content in enumerate(("bad", "good")):
        result = await send(
            sid,
            NewMessage(
                content=content,
                model_id="qwen",
                agent_id="default",
                request_id=str(index),
            ),
            request,
        )
        await asyncio.gather(*list(request.app.state.agent_chat_tasks))
    result = {"data": store.get_session(sid)}
    assert result["data"]["messages"][1]["status"] == "failed"
    assert result["data"]["messages"][-1]["content"] == "ok"


def test_duplicate_request_rejects_changed_input(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    sid = store.create_session("personal")["id"]
    store.begin_message(sid, "one", "qwen", "default", "same-id")
    with pytest.raises(ValueError):
        store.begin_message(sid, "different", "qwen", "default", "same-id")


def test_rename_session_persists_and_validates_title(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    sid = store.create_session("personal")["id"]
    assert store.rename_session(sid, "  项目讨论  ")["title"] == "项目讨论"
    store.begin_message(sid, "第一条消息", "qwen", "default", "rename-test")
    assert store.get_session(sid)["title"] == "项目讨论"
    assert store.list_sessions("personal", "项目")[0]["id"] == sid
    with pytest.raises(ValueError):
        store.rename_session(sid, "   ")
    with pytest.raises(ValueError):
        store.rename_session(sid, "x" * 101)
    with pytest.raises(KeyError):
        store.rename_session("missing", "名称")


def test_delete_session_removes_messages_and_preserves_other_sessions(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    first = store.create_session("personal")["id"]
    second = store.create_session("personal")["id"]
    pending = store.begin_message(first, "one", "qwen", "default", "delete-r1")
    with pytest.raises(ValueError):
        store.delete_session(first)
    store.finish_message(pending["assistant_id"], "reply")
    other = store.begin_message(second, "two", "qwen", "default", "delete-r2")
    store.finish_message(other["assistant_id"], "other reply")
    store.delete_session(first)
    with pytest.raises(KeyError):
        store.get_session(first)
    assert len(store.get_session(second)["messages"]) == 2
    with vault.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_chat_messages WHERE session_id=?", (first,)
            ).fetchone()[0]
            == 0
        )
    with pytest.raises(KeyError):
        store.delete_session(first)

@pytest.mark.asyncio
async def test_submission_returns_before_background_generation_completes(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from everos.entrypoints.memo_vault.routes.agent_chat import NewMessage, send

    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    sid = store.create_session('personal')['id']
    release = asyncio.Event()
    class SlowRuntime:
        async def generate(self, agent, history):
            await release.wait()
            return '后台完成'
    state = SimpleNamespace(agent_chat_store=store, agent_runtime=SlowRuntime(), agent_chat_tasks=set())
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    submitted = await asyncio.wait_for(send(sid, NewMessage(content='hello', model_id='qwen', agent_id='default', request_id='background-1'), request), timeout=0.5)
    assert submitted['data']['messages'][-1]['status'] == 'pending'
    assert state.agent_chat_tasks
    assert store.list_sessions('personal')[0]['generating'] == 1
    duplicate = await send(sid, NewMessage(content='hello', model_id='qwen', agent_id='default', request_id='background-1'), request)
    assert duplicate['data']['messages'][-1]['status'] == 'pending'
    assert len(state.agent_chat_tasks) == 1
    with pytest.raises(ValueError):
        store.delete_session(sid)
    assert store.create_session('personal')['id'] != sid
    release.set()
    await asyncio.gather(*list(state.agent_chat_tasks))
    assert store.get_session(sid)['messages'][-1]['content'] == '后台完成'
    assert store.get_session(sid)['messages'][-1]['status'] == 'completed'
    assert next(s for s in store.list_sessions('personal') if s['id'] == sid)['generating'] == 0
