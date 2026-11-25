"""Unit tests for Personalized RAG in MEMO Vault Agent Chat."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from everos.entrypoints.memo_vault.agent_chat_context import (
    build_chat_context,
    build_model_messages,
    build_retrieval_query,
    revalidate_context_sources,
)
from everos.entrypoints.memo_vault.agent_chat_service import AgentChatStore
from everos.entrypoints.memo_vault.routes.agent_chat import NewMessage, send
from everos.entrypoints.memo_vault.storage import VaultStore


def _seed_memory(
    vault: VaultStore,
    *,
    mem_id: str,
    everos_memory_id: str,
    project_id: str,
    kind: str = "fact",
    memory_type: str = "atomic_fact",
    status: str = "confirmed",
    lifecycle_status: str = "active",
    external_use: str = "ask",
    valid_until: str | None = None,
) -> None:
    with vault.connect() as conn:
        conn.execute(
            """INSERT INTO memories
               (id, everos_memory_id, namespace, kind, memory_type, status,
                revision, evidence_kind, external_use, origin, valid_until, lifecycle_status)
               VALUES (?, ?, ?, ?, ?, ?, 1, 'direct_quote', ?, 'import', ?, ?)""",
            (
                mem_id,
                everos_memory_id,
                project_id,
                kind,
                memory_type,
                status,
                external_use,
                valid_until,
                lifecycle_status,
            ),
        )


class _StubAdapter:
    def __init__(
        self,
        *,
        memories_by_project: dict[str, dict] | None = None,
        knowledge_by_project: dict[str, list[dict]] | None = None,
        fail_memory: bool = False,
        fail_knowledge: bool = False,
    ) -> None:
        self.memories_by_project = memories_by_project or {}
        self.knowledge_by_project = knowledge_by_project or {}
        self.fail_memory = fail_memory
        self.fail_knowledge = fail_knowledge
        self.memory_calls: list[tuple[str, str]] = []
        self.knowledge_calls: list[tuple[str, str]] = []

    async def search_memories(self, *, query, app_id, project_id, user_id, **kwargs):
        self.memory_calls.append((project_id, query))
        if self.fail_memory:
            raise RuntimeError("everos memory down")
        return self.memories_by_project.get(project_id, {"episodes": [], "profiles": []})

    async def search_knowledge(self, *, query, app_id, project_id, **kwargs):
        self.knowledge_calls.append((project_id, query))
        if self.fail_knowledge:
            raise RuntimeError("everos knowledge down")
        return self.knowledge_by_project.get(project_id, [])


@pytest.mark.asyncio
async def test_subtree_scope_includes_children_and_isolates_parent_and_siblings(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    child = vault.create_project(name="project1", parent_project_id="personal")
    sibling = vault.create_project(name="work", parent_project_id=None)
    child_id = str(child["project_id"])
    sibling_id = str(sibling["project_id"])

    _seed_memory(vault, mem_id="m_root", everos_memory_id="ev_root", project_id="personal")
    _seed_memory(vault, mem_id="m_child", everos_memory_id="ev_child", project_id=child_id)
    _seed_memory(vault, mem_id="m_other", everos_memory_id="ev_other", project_id=sibling_id)

    adapter = _StubAdapter(
        memories_by_project={
            "personal": {
                "episodes": [
                    {
                        "id": "ep_root",
                        "summary": "root ep",
                        "subject": "Root",
                        "score": 0.9,
                        "atomic_facts": [{"id": "ev_root", "content": "个人空间事实", "score": 0.92}],
                    }
                ],
                "profiles": [],
            },
            child_id: {
                "episodes": [
                    {
                        "id": "ep_child",
                        "summary": "child ep",
                        "subject": "Child",
                        "score": 0.85,
                        "atomic_facts": [{"id": "ev_child", "content": "子项目 project1 事实", "score": 0.88}],
                    }
                ],
                "profiles": [],
            },
            sibling_id: {
                "episodes": [
                    {
                        "id": "ep_other",
                        "summary": "other ep",
                        "subject": "Other",
                        "score": 0.99,
                        "atomic_facts": [{"id": "ev_other", "content": "无关项目事实", "score": 0.99}],
                    }
                ],
                "profiles": [],
            },
        },
        knowledge_by_project={
            child_id: [
                {
                    "topic_id": "t_child",
                    "topic_name": "架构说明",
                    "summary": "子项目知识摘要",
                    "content": "project1 使用 FastAPI + SQLite。",
                    "score": 0.81,
                    "document": {"doc_id": "d_child", "title": "设计文档", "summary": "设计"},
                }
            ],
            sibling_id: [
                {
                    "topic_id": "t_other",
                    "topic_name": "其他项目",
                    "content": "不应该被检索到",
                    "score": 0.99,
                    "document": {"doc_id": "d_other", "title": "其他", "summary": ""},
                }
            ],
        },
    )

    # Bound to personal -> includes personal + project1, excludes sibling work
    ctx_root = await build_chat_context(
        vault_store=vault,
        adapter=adapter,
        project_id="personal",
        history=[{"role": "user", "content": "介绍一下我的项目"}],
    )
    assert ctx_root.status == "ready"
    assert set(ctx_root.subtree_project_ids) == {"personal", child_id}
    assert sibling_id not in ctx_root.subtree_project_ids
    source_ids = {s.id for s in ctx_root.sources}
    assert source_ids == {"m_root", "m_child", "t_child"}
    assert "无关项目事实" not in ctx_root.context_prompt
    assert "【历史项目记忆（关于当前提问者“我”的个人背景" in ctx_root.context_prompt

    # Bound to project1 -> includes ONLY project1, excludes parent personal and sibling work
    ctx_child = await build_chat_context(
        vault_store=vault,
        adapter=adapter,
        project_id=child_id,
        history=[{"role": "user", "content": "介绍一下这个子项目"}],
    )
    assert ctx_child.subtree_project_ids == [child_id]
    assert {s.id for s in ctx_child.sources} == {"m_child", "t_child"}


@pytest.mark.asyncio
async def test_filters_unconfirmed_withdrawn_deleted_expired_but_allows_deny_external_use(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=10)).isoformat()

    _seed_memory(vault, mem_id="m_active_deny", everos_memory_id="ev_ok", project_id="personal", external_use="deny", valid_until=future)
    _seed_memory(vault, mem_id="m_withdrawn", everos_memory_id="ev_withdrawn", project_id="personal", lifecycle_status="withdrawn")
    _seed_memory(vault, mem_id="m_deleted", everos_memory_id="ev_deleted", project_id="personal", lifecycle_status="deleted")
    _seed_memory(vault, mem_id="m_expired", everos_memory_id="ev_expired", project_id="personal", valid_until=past)
    _seed_memory(vault, mem_id="m_unconfirmed", everos_memory_id="ev_unconf", project_id="personal", status="pending")

    adapter = _StubAdapter(
        memories_by_project={
            "personal": {
                "episodes": [
                    {
                        "id": "ep_1",
                        "summary": "ep",
                        "subject": "s",
                        "score": 0.9,
                        "atomic_facts": [
                            {"id": "ev_ok", "content": "允许本人对话使用的私有记忆", "score": 0.9},
                            {"id": "ev_withdrawn", "content": "已撤回记忆", "score": 0.95},
                            {"id": "ev_deleted", "content": "已删除记忆", "score": 0.96},
                            {"id": "ev_expired", "content": "已过期记忆", "score": 0.97},
                            {"id": "ev_unconf", "content": "未确认候选", "score": 0.98},
                            {"id": "ev_unknown", "content": "未登记在 Vault 的记忆", "score": 0.99},
                        ],
                    }
                ],
                "profiles": [],
            }
        }
    )

    ctx = await build_chat_context(
        vault_store=vault,
        adapter=adapter,
        project_id="personal",
        history=[{"role": "user", "content": "我的私有设置是什么"}],
    )
    assert [s.id for s in ctx.sources] == ["m_active_deny"]
    assert "允许本人对话使用的私有记忆" in ctx.context_prompt
    for forbidden in ("已撤回记忆", "已删除记忆", "已过期记忆", "未确认候选", "未登记"):
        assert forbidden not in ctx.context_prompt

    # Pre-generation re-validation drops memory if withdrawn right before model call
    with vault.connect() as conn:
        conn.execute("UPDATE memories SET lifecycle_status='withdrawn' WHERE id='m_active_deny'")
    revalidated = revalidate_context_sources(vault, ctx)
    assert revalidated.sources[0].available is False
    assert revalidated.context_prompt == ""
    assert revalidated.status == "empty"


@pytest.mark.asyncio
async def test_partial_failure_continues_and_total_failure_aborts_without_model_call(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    sid = store.create_session("personal")["id"]
    _seed_memory(vault, mem_id="m_1", everos_memory_id="ev_1", project_id="personal")

    calls: list[tuple[str, list[dict]]] = []

    class SpyRuntime:
        async def generate(self, agent, messages):
            calls.append((agent, messages))
            return "回答完成"

    # 1) Partial failure: knowledge fails, memory succeeds -> model IS called
    partial_adapter = _StubAdapter(
        memories_by_project={
            "personal": {
                "episodes": [
                    {
                        "id": "ep_1",
                        "summary": "ep",
                        "subject": "s",
                        "score": 0.8,
                        "atomic_facts": [{"id": "ev_1", "content": "用户偏好简洁回答", "score": 0.9}],
                    }
                ],
                "profiles": [],
            }
        },
        fail_knowledge=True,
    )
    req_partial = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                agent_chat_store=store,
                agent_runtime=SpyRuntime(),
                everos_adapter=partial_adapter,
                agent_chat_tasks=set(),
            )
        )
    )
    await send(
        sid,
        NewMessage(content="我偏好什么风格？", model_id="qwen", agent_id="default", request_id="r-partial"),
        req_partial,
    )
    await asyncio.gather(*list(req_partial.app.state.agent_chat_tasks))
    assert len(calls) == 1
    assert "我偏好简洁回答" in calls[0][1][-1]["content"]
    session = store.get_session(sid)
    assistant = session["messages"][-1]
    assert assistant["status"] == "completed"
    assert assistant["context_status"] == "partial_failure"
    assert any(e["type"] == "context_partial" for e in assistant["events"])
    # Stored user message in SQLite remains clean (no injected RAG block)
    assert session["messages"][-2]["content"] == "我偏好什么风格？"

    # 2) Total failure: both memory and knowledge fail -> model is NOT called
    total_fail_adapter = _StubAdapter(fail_memory=True, fail_knowledge=True)
    req_fail = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                agent_chat_store=store,
                agent_runtime=SpyRuntime(),
                everos_adapter=total_fail_adapter,
                agent_chat_tasks=set(),
            )
        )
    )
    await send(
        sid,
        NewMessage(content="再查一次", model_id="qwen", agent_id="default", request_id="r-fail"),
        req_fail,
    )
    await asyncio.gather(*list(req_fail.app.state.agent_chat_tasks))
    assert len(calls) == 1  # model was NOT invoked
    failed_msg = store.get_session(sid)["messages"][-1]
    assert failed_msg["status"] == "failed"
    assert failed_msg["context_status"] == "failed"
    assert any(e["type"] == "context_failed" for e in failed_msg["events"])


@pytest.mark.asyncio
async def test_disabled_toggle_skips_retrieval_and_all_four_runtimes_receive_context(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    sid = store.create_session("personal")["id"]
    _seed_memory(vault, mem_id="m_1", everos_memory_id="ev_1", project_id="personal")

    adapter = _StubAdapter(
        memories_by_project={
            "personal": {
                "episodes": [
                    {
                        "id": "ep_1",
                        "summary": "ep",
                        "subject": "s",
                        "score": 0.9,
                        "atomic_facts": [{"id": "ev_1", "content": "项目代号 Aurora", "score": 0.95}],
                    }
                ],
                "profiles": [],
            }
        }
    )

    received: dict[str, list[dict]] = {}

    class Runtime:
        def __init__(self, name: str, framework_version: str | None = None) -> None:
            self.name = name
            self.framework_version = framework_version

        async def generate(self, agent, messages, emit=None):
            received[self.name] = messages
            if emit:
                emit("framework_started", self.name)
            return f"ok:{self.name}"

    state = SimpleNamespace(
        agent_chat_store=store,
        agent_runtime=Runtime("default"),
        openhands_runtime=Runtime("openhands", "OpenHands 1.0"),
        swe_runtime=Runtime("swe", "SWE-agent 1.0"),
        agentlab_runtime=Runtime("agentlab", "AgentLab 1.0"),
        everos_adapter=adapter,
        agent_chat_tasks=set(),
    )
    req = SimpleNamespace(app=SimpleNamespace(state=state))

    # Disabled toggle -> no adapter calls
    await send(
        sid,
        NewMessage(
            content="纯通用问题",
            model_id="qwen",
            agent_id="default",
            request_id="r-disabled",
            use_project_context=False,
        ),
        req,
    )
    await asyncio.gather(*list(state.agent_chat_tasks))
    assert adapter.memory_calls == []
    assert adapter.knowledge_calls == []
    assert received["default"][-1]["content"] == "纯通用问题"
    assert store.get_session(sid)["messages"][-1]["context_status"] == "disabled"

    # All 4 runtimes with RAG enabled
    for idx, (model_id, agent_id) in enumerate(
        [("qwen", "default"), ("qwen-1", "openhands"), ("qwen-2", "swe"), ("qwen-3", "agentlab")]
    ):
        await send(
            sid,
            NewMessage(
                content=f"项目代号是什么 {idx}",
                model_id=model_id,
                agent_id=agent_id,
                request_id=f"r-{agent_id}",
                use_project_context=True,
            ),
            req,
        )
        await asyncio.gather(*list(state.agent_chat_tasks))
        assert "项目代号 Aurora" in received[agent_id][-1]["content"]


def test_followup_query_and_history_budget_trimming():
    history = [
        {"role": "user", "content": "帮我看看 MEMO Vault 的本地缓存策略"},
        {"role": "assistant", "content": "它使用 SQLite 存储状态。"},
        {"role": "user", "content": "它为什么这样设计？"},
    ]
    q = build_retrieval_query(history)
    assert "本地缓存策略" in q
    assert "它为什么这样设计" in q

    long_history = []
    for i in range(10):
        long_history.append({"role": "user", "content": f"问题 {i} " + "x" * 180})
        long_history.append({"role": "assistant", "content": f"回答 {i} " + "y" * 180})
    long_history.append({"role": "user", "content": "当前问题"})
    trimmed_msgs, was_trimmed = build_model_messages(long_history, "[项目参考资料]\n1. 事实\n[/项目参考资料]")
    assert was_trimmed is True
    assert trimmed_msgs[-1]["role"] == "user"
    assert "当前问题" in trimmed_msgs[-1]["content"]
    assert trimmed_msgs[0]["role"] == "user"


@pytest.mark.asyncio
async def test_http_adapter_unwraps_knowledge_search_envelope():
    import httpx
    from everos.entrypoints.memo_vault.adapter import EverOSHttpAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/knowledge/search"
        return httpx.Response(
            200,
            json={
                "request_id": "req-kn-1",
                "data": {
                    "hits": [{"topic_id": "t1", "topic_name": "概览", "score": 0.9}],
                    "total": 1,
                    "took_ms": 2.5,
                },
            },
        )

    adapter = EverOSHttpAdapter(
        "http://everos.test",
        transport=httpx.MockTransport(handler),
    )
    hits = await adapter.search_knowledge(
        query="测试", app_id="app1", project_id="personal"
    )
    assert len(hits) == 1
    assert hits[0]["topic_id"] == "t1"


@pytest.mark.asyncio
async def test_profile_explicit_info_unpacking_and_first_person_mapping(tmp_path):
    vault = VaultStore(tmp_path)
    vault.initialize()
    user_id = vault.get_identity()["user_id"]
    cand_id = "cand_major_1"
    _seed_memory(
        vault,
        mem_id=cand_id,
        everos_memory_id=f"profile_{user_id}_{cand_id}",
        project_id="personal",
        kind="preference",
        memory_type="profile",
    )
    adapter = _StubAdapter(
        memories_by_project={
            "personal": {
                "episodes": [],
                "profiles": [
                    {
                        "id": f"profile_row_{user_id}",
                        "profile_data": {
                            "explicit_info": [
                                {
                                    "memo_vault_candidate_id": cand_id,
                                    "description": "用户是一名计算机专业的学生",
                                },
                                {
                                    "memo_vault_candidate_id": "unconfirmed_cand",
                                    "description": "未审核的画像条目",
                                },
                            ],
                            "implicit_traits": [],
                        },
                    }
                ],
            }
        }
    )
    ctx = await build_chat_context(
        vault_store=vault,
        adapter=adapter,
        project_id="personal",
        history=[{"role": "user", "content": "我是什么专业的？"}],
    )
    assert ctx.status == "ready"
    assert [s.id for s in ctx.sources] == [cand_id]
    # UI snippet keeps original text, while model prompt maps third-person 用户 -> 我
    assert ctx.sources[0].snippet == "用户是一名计算机专业的学生"
    assert "我是一名计算机专业的学生" in ctx.context_prompt
    assert "未审核的画像条目" not in ctx.context_prompt
