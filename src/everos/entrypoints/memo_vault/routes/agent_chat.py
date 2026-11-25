"""Owner-local and plugin agent chat API."""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..agent_chat_attachments import AttachmentParseError, parse_chat_attachment
from ..agent_chat_context import (
    MAX_ATTACHED_MATERIAL_CHARS,
    MAX_TURN_INPUT_CHARS,
    build_chat_context,
    build_model_messages,
    render_attached_materials_prompt,
    revalidate_context_sources,
    summarize_attached_materials,
)
from ..agent_chat_service import MODELS, resolve_model

router = APIRouter(prefix="/agent-chat", tags=["agent-chat"])
plugin_router = APIRouter(prefix="/plugin/agent-chat", tags=["plugin-agent-chat"])


class NewSession(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)


class RenameSession(BaseModel):
    title: str = Field(max_length=100)


class AttachedMaterial(BaseModel):
    ref_id: str = Field(min_length=1, max_length=16)
    source_type: Literal["web_page", "agent_conversation", "file_attachment"] = "web_page"
    title: str = Field(default="", max_length=200)
    source_url: str = Field(default="", max_length=1000)
    role: str | None = Field(default=None, max_length=32)
    content: str = Field(min_length=1, max_length=4000)


class NewMessage(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    model_id: str
    agent_id: str
    request_id: str = Field(min_length=1, max_length=128)
    use_project_context: bool = True
    attached_materials: list[AttachedMaterial] = Field(default_factory=list, max_length=30)


def envelope(data):
    return {"request_id": uuid.uuid4().hex, "data": data}


def _task_map(request: Request) -> dict[str, asyncio.Task]:
    mapping = getattr(request.app.state, "agent_chat_task_map", None)
    if mapping is None:
        mapping = {}
        request.app.state.agent_chat_task_map = mapping
    return mapping


def _stop_requests(request: Request) -> set[str]:
    stops = getattr(request.app.state, "agent_chat_stop_requests", None)
    if stops is None:
        stops = set()
        request.app.state.agent_chat_stop_requests = stops
    return stops


@router.get("/models")
@plugin_router.get("/models")
async def models():
    return envelope(
        {
            "models": [
                {"id": k, "agent_id": v[0], "name": v[1], "agent_name": v[2]}
                for k, v in MODELS.items()
            ]
        }
    )


@router.post("/attachments")
@plugin_router.post("/attachments")
async def upload_attachment(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        parsed = parse_chat_attachment(
            filename=file.filename or "attachment.txt",
            media_type=file.content_type or "application/octet-stream",
            raw_bytes=raw,
        )
    except AttachmentParseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return envelope(parsed)


@router.get("/sessions")
@plugin_router.get("/sessions")
async def sessions(request: Request, project_id: str | None = None, q: str = ""):
    return envelope(
        {
            "sessions": request.app.state.agent_chat_store.list_sessions(
                project_id, q[:200]
            )
        }
    )


@router.post("/sessions", status_code=201)
@plugin_router.post("/sessions", status_code=201)
async def create(body: NewSession, request: Request):
    try:
        return envelope(
            request.app.state.agent_chat_store.create_session(body.project_id)
        )
    except KeyError:
        raise HTTPException(404, "项目不存在") from None


@router.get("/sessions/{sid}")
@plugin_router.get("/sessions/{sid}")
async def detail(sid: str, request: Request):
    try:
        return envelope(request.app.state.agent_chat_store.get_session(sid))
    except KeyError:
        raise HTTPException(404, "会话不存在") from None


@router.delete("/sessions/{sid}")
@plugin_router.delete("/sessions/{sid}")
async def delete(sid: str, request: Request):
    try:
        request.app.state.agent_chat_store.delete_session(sid)
        return envelope({"deleted": True, "session_id": sid})
    except KeyError:
        raise HTTPException(404, "会话不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/sessions/{sid}")
@plugin_router.patch("/sessions/{sid}")
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
@plugin_router.post("/sessions/{sid}/messages")
async def send(sid: str, body: NewMessage, request: Request):
    store = request.app.state.agent_chat_store
    attached_dicts = [m.model_dump() for m in body.attached_materials]
    if attached_dicts:
        attached_chars = sum(len(m["content"]) for m in attached_dicts)
        total_chars = attached_chars + len(body.content)
        if attached_chars > MAX_ATTACHED_MATERIAL_CHARS or total_chars > MAX_TURN_INPUT_CHARS:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "CONTEXT_LIMIT_EXCEEDED",
                    "message": "所选材料与问题超出当前模型上下文上限，请删减部分条目后重试。",
                    "current_chars": attached_chars,
                    "max_chars": MAX_ATTACHED_MATERIAL_CHARS,
                },
            )
    try:
        agent = resolve_model(body.model_id, body.agent_id)
        started = store.begin_message(
            sid,
            body.content,
            body.model_id,
            body.agent_id,
            body.request_id,
            use_project_context=body.use_project_context,
        )
    except KeyError:
        raise HTTPException(404, "会话不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if started["duplicate"]:
        return envelope(store.get_session(sid))
    session = store.get_session(sid)
    messages = session["messages"]
    # Only completed user/assistant pairs plus the current user message belong
    # in the model context. Failed or stopped attempts must not form adjacent user turns.
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
    runtime = (
        request.app.state.openhands_runtime
        if body.agent_id == "openhands"
        else request.app.state.swe_runtime
        if body.agent_id == "swe"
        else request.app.state.agentlab_runtime
        if body.agent_id == "agentlab"
        else request.app.state.agent_runtime
    )
    adapter = getattr(request.app.state, "everos_adapter", None)
    assistant_id = started["assistant_id"]
    task_map = _task_map(request)
    stop_requests = _stop_requests(request)
    task = asyncio.create_task(
        _generate_reply(
            store,
            runtime,
            assistant_id,
            agent,
            history,
            project_id=session["project_id"],
            use_project_context=body.use_project_context,
            adapter=adapter,
            attached_materials=attached_dicts,
            stop_requests=stop_requests,
        )
    )
    tasks = request.app.state.agent_chat_tasks
    tasks.add(task)
    task_map[assistant_id] = task

    def _cleanup(done_task: asyncio.Task) -> None:
        tasks.discard(done_task)
        task_map.pop(assistant_id, None)
        stop_requests.discard(assistant_id)

    task.add_done_callback(_cleanup)
    return envelope(store.get_session(sid))


@router.post("/sessions/{sid}/messages/{mid}/cancel")
@plugin_router.post("/sessions/{sid}/messages/{mid}/cancel")
async def cancel_message(sid: str, mid: str, request: Request):
    store = request.app.state.agent_chat_store
    try:
        store.get_session(sid)
    except KeyError:
        raise HTTPException(404, "会话不存在") from None
    msg = store.get_message(sid, mid)
    if msg is None:
        raise HTTPException(404, "消息不存在")
    if msg["status"] != "pending":
        return envelope(store.get_session(sid))

    stop_requests = _stop_requests(request)
    stop_requests.add(mid)
    task = _task_map(request).get(mid)
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, TimeoutError, Exception):
            pass
    # Ensure message is marked stopped even if task hadn't started or already exited.
    latest = store.get_message(sid, mid)
    if latest and latest["status"] == "pending":
        store.add_event(mid, "generation_stopped", "用户停止了本次生成")
        store.finish_message(mid, "已停止生成。", status="stopped")
    return envelope(store.get_session(sid))


async def _generate_reply(
    store,
    runtime,
    assistant_id,
    agent,
    history,
    *,
    project_id: str = "",
    use_project_context: bool = True,
    adapter=None,
    attached_materials: list[dict] | None = None,
    stop_requests: set[str] | None = None,
):
    attached_list = attached_materials or []
    attached_prompt = render_attached_materials_prompt(attached_list)
    attached_summary = summarize_attached_materials(attached_list)
    try:
        model_messages = history
        if use_project_context and adapter is not None and project_id:
            store.add_event(
                assistant_id,
                "retrieving_context",
                "正在检索当前项目及子项目的正式记忆与 Knowledge...",
            )
            ctx = await build_chat_context(
                vault_store=store.vault,
                adapter=adapter,
                project_id=project_id,
                history=history,
            )
            meta = ctx.to_meta_dict()
            if attached_summary:
                meta["attached_materials"] = attached_summary
            store.update_message_context(assistant_id, ctx.status, meta)
            if ctx.status == "failed":
                err = ctx.error_message or "项目记忆检索失败，请重试。"
                store.add_event(assistant_id, "context_failed", err)
                store.finish_message(assistant_id, err, failed=True)
                return
            ctx = revalidate_context_sources(store.vault, ctx)
            meta = ctx.to_meta_dict()
            if attached_summary:
                meta["attached_materials"] = attached_summary
            store.update_message_context(assistant_id, ctx.status, meta)
            mem_count = sum(
                1 for s in ctx.sources if s.available and s.source_type == "memory"
            )
            kn_count = sum(
                1 for s in ctx.sources if s.available and s.source_type == "knowledge"
            )
            if ctx.status == "partial_failure":
                store.add_event(
                    assistant_id,
                    "context_partial",
                    f"{ctx.error_message or '部分来源检索失败'}（可用：正式记忆 {mem_count} 条，Knowledge {kn_count} 条）",
                )
            elif mem_count + kn_count > 0:
                store.add_event(
                    assistant_id,
                    "context_ready",
                    f"已整理 {mem_count} 条正式记忆、{kn_count} 条 Knowledge（覆盖 {len(ctx.subtree_project_ids)} 个项目空间）",
                )
            else:
                store.add_event(
                    assistant_id,
                    "context_ready",
                    "当前项目及子项目暂无相关参考资料，将直接回答",
                )
            model_messages, _ = build_model_messages(
                history, ctx.context_prompt, attached_prompt=attached_prompt
            )
        else:
            if not use_project_context:
                meta_disabled = {
                    "status": "disabled",
                    "project_id": project_id,
                    "subtree_project_ids": [project_id] if project_id else [],
                    "query": "",
                    "memory_status": "skipped",
                    "knowledge_status": "skipped",
                    "sources": [],
                    "trimmed": False,
                    "error_message": None,
                }
                if attached_summary:
                    meta_disabled["attached_materials"] = attached_summary
                store.update_message_context(assistant_id, "disabled", meta_disabled)
            elif attached_summary:
                store.update_message_context(
                    assistant_id,
                    "ready",
                    {
                        "status": "ready",
                        "project_id": project_id,
                        "subtree_project_ids": [project_id] if project_id else [],
                        "query": "",
                        "memory_status": "skipped",
                        "knowledge_status": "skipped",
                        "sources": [],
                        "attached_materials": attached_summary,
                        "trimmed": False,
                        "error_message": None,
                    },
                )
            if attached_prompt:
                model_messages, _ = build_model_messages(
                    history, "", attached_prompt=attached_prompt
                )

        if getattr(runtime, "framework_version", None):
            output = await runtime.generate(
                agent,
                model_messages,
                emit=lambda kind, content: store.add_event(assistant_id, kind, content),
            )
        else:
            output = await runtime.generate(agent, model_messages)
        store.finish_message(
            assistant_id,
            output,
            framework_version=getattr(runtime, "framework_version", None),
        )
    except asyncio.CancelledError:
        if stop_requests is not None and assistant_id in stop_requests:
            store.add_event(assistant_id, "generation_stopped", "用户停止了本次生成")
            store.finish_message(assistant_id, "已停止生成。", status="stopped")
            return
        store.finish_message(assistant_id, "服务关闭导致请求中断，请重试。", failed=True)
        raise
    except Exception as exc:
        store.finish_message(assistant_id, f"模型推理失败：{exc}", failed=True)
