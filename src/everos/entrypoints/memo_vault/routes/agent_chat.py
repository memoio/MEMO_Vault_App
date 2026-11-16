"""Owner-local agent chat API."""

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..agent_chat_service import MODELS, resolve_model

router = APIRouter(prefix="/agent-chat", tags=["agent-chat"])


class NewSession(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)


class RenameSession(BaseModel):
    title: str = Field(max_length=100)


class NewMessage(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    model_id: str
    agent_id: str
    request_id: str = Field(min_length=1, max_length=128)


def envelope(data):
    return {"request_id": uuid.uuid4().hex, "data": data}


@router.get("/models")
async def models():
    return envelope(
        {
            "models": [
                {"id": k, "agent_id": v[0], "name": v[1], "agent_name": v[2]}
                for k, v in MODELS.items()
            ]
        }
    )


@router.get("/sessions")
async def sessions(request: Request, project_id: str | None = None, q: str = ""):
    return envelope(
        {
            "sessions": request.app.state.agent_chat_store.list_sessions(
                project_id, q[:200]
            )
        }
    )


@router.post("/sessions", status_code=201)
async def create(body: NewSession, request: Request):
    try:
        return envelope(
            request.app.state.agent_chat_store.create_session(body.project_id)
        )
    except KeyError:
        raise HTTPException(404, "项目不存在") from None


@router.get("/sessions/{sid}")
async def detail(sid: str, request: Request):
    try:
        return envelope(request.app.state.agent_chat_store.get_session(sid))
    except KeyError:
        raise HTTPException(404, "会话不存在") from None


@router.delete("/sessions/{sid}")
async def delete(sid: str, request: Request):
    try:
        request.app.state.agent_chat_store.delete_session(sid)
        return envelope({"deleted": True, "session_id": sid})
    except KeyError:
        raise HTTPException(404, "会话不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/sessions/{sid}")
async def rename(sid: str, body: RenameSession, request: Request):
    try:
        return envelope(
            request.app.state.agent_chat_store.rename_session(sid, body.title)
        )
    except KeyError:
        raise HTTPException(404, "会话不存在") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/sessions/{sid}/messages")
async def send(sid: str, body: NewMessage, request: Request):
    store = request.app.state.agent_chat_store
    try:
        agent = resolve_model(body.model_id, body.agent_id)
        started = store.begin_message(
            sid, body.content, body.model_id, body.agent_id, body.request_id
        )
    except KeyError:
        raise HTTPException(404, "会话不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if started["duplicate"]:
        return envelope(store.get_session(sid))
    messages = store.get_session(sid)["messages"]
    # Only completed user/assistant pairs plus the current user message belong
    # in the model context. Failed attempts must not form adjacent user turns.
    completed = {
        m["request_id"]
        for m in messages
        if m["role"] == "assistant" and m["status"] == "completed"
    }
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m["status"] == "completed"
        and (
            m["request_id"] in completed
            or (m["request_id"] == body.request_id and m["role"] == "user")
        )
    ]
    runtime = (request.app.state.openhands_runtime if body.agent_id == "openhands"
               else request.app.state.swe_runtime if body.agent_id == "swe"
               else request.app.state.agentlab_runtime if body.agent_id == "agentlab"
               else request.app.state.agent_runtime)
    task = asyncio.create_task(
        _generate_reply(store, runtime, started["assistant_id"], agent, history)
    )
    tasks = request.app.state.agent_chat_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return envelope(store.get_session(sid))


async def _generate_reply(store, runtime, assistant_id, agent, history):
    try:
        if getattr(runtime, "framework_version", None):
            output = await runtime.generate(agent, history, emit=lambda kind, content: store.add_event(assistant_id, kind, content))
        else:
            output = await runtime.generate(agent, history)
        store.finish_message(assistant_id, output, framework_version=getattr(runtime, "framework_version", None))
    except asyncio.CancelledError:
        store.finish_message(assistant_id, "服务关闭导致请求中断，请重试。", failed=True)
        raise
    except Exception as exc:
        store.finish_message(assistant_id, f"模型推理失败：{exc}", failed=True)
