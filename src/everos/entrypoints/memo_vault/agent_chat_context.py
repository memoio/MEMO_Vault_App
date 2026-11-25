"""Unified project-scoped personalized RAG context builder for Agent Chat."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .adapter import EverOSAdapter
from .storage import VaultStore

ContextStatus = Literal["disabled", "ready", "empty", "partial_failure", "failed"]
SourceStatus = Literal["ok", "partial", "failed", "skipped"]

MAX_QUERY_CHARS = 400
MAX_MEMORY_ITEMS = 3
MAX_KNOWLEDGE_ITEMS = 2
MAX_TOTAL_ITEMS = 5
MAX_SNIPPET_CHARS = 220
MAX_CONTEXT_CHARS = 1200
MAX_HISTORY_CHARS = 1400
MAX_ATTACHED_MATERIAL_CHARS = 1800
MAX_TURN_INPUT_CHARS = 2400

_FOLLOWUP_RE = re.compile(
    r"(它|它们|这个|那个|这些|那些|上面|前面|刚才|之前|继续|展开|详细|具体|为什么|怎么|来源|依据|吗|呢|this|that|it|they|above|previous|more|why|how)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ContextSource:
    """One verified reference item provided (or filtered out) for a chat turn."""

    id: str
    source_type: Literal["memory", "knowledge"]
    title: str
    snippet: str
    project_id: str
    project_name: str
    score: float
    kind: str = ""
    memory_type: str | None = None
    revision: int | None = None
    doc_id: str | None = None
    topic_id: str | None = None
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "title": self.title,
            "snippet": self.snippet,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "score": round(float(self.score), 4),
            "kind": self.kind,
            "memory_type": self.memory_type,
            "revision": self.revision,
            "doc_id": self.doc_id,
            "topic_id": self.topic_id,
            "available": self.available,
        }


@dataclass(slots=True)
class ChatContextResult:
    """Structured output of the unified RAG retrieval and context builder."""

    status: ContextStatus
    project_id: str
    subtree_project_ids: list[str]
    query: str
    memory_status: SourceStatus
    knowledge_status: SourceStatus
    sources: list[ContextSource] = field(default_factory=list)
    context_prompt: str = ""
    trimmed: bool = False
    error_message: str | None = None

    def to_meta_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "project_id": self.project_id,
            "subtree_project_ids": self.subtree_project_ids,
            "query": self.query,
            "memory_status": self.memory_status,
            "knowledge_status": self.knowledge_status,
            "sources": [source.to_dict() for source in self.sources],
            "trimmed": self.trimmed,
            "error_message": self.error_message,
        }


def build_retrieval_query(history: list[dict[str, str]]) -> str:
    """Build a compact retrieval query from the current user turn (and recent turns for short follow-ups)."""
    user_turns = [
        str(m.get("content", "")).strip()
        for m in history
        if m.get("role") == "user" and str(m.get("content", "")).strip()
    ]
    if not user_turns:
        return ""
    current = user_turns[-1]
    if len(user_turns) > 1 and (len(current) <= 30 or _FOLLOWUP_RE.search(current)):
        recent = [turn[:120] for turn in user_turns[-3:-1]]
        combined = " ".join([*recent, current])
        return combined[:MAX_QUERY_CHARS].strip()
    return current[:MAX_QUERY_CHARS].strip()


def _is_expired(valid_until: str | None, now: datetime) -> bool:
    if not valid_until:
        return False
    try:
        parsed = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= now


def _load_active_confirmed_memories(
    vault_store: VaultStore,
    subtree_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Load Vault-authoritative active, confirmed, non-expired memories in the project subtree."""
    if not subtree_ids:
        return {}
    placeholders = ",".join("?" for _ in subtree_ids)
    now = datetime.now(UTC)
    with vault_store.connect() as conn:
        rows = conn.execute(
            f"""SELECT id, everos_memory_id, namespace, kind, memory_type,
                       revision, valid_until, lifecycle_status, confirmed_at
                FROM memories
                WHERE status = 'confirmed'
                  AND lifecycle_status = 'active'
                  AND namespace IN ({placeholders})""",
            tuple(subtree_ids),
        ).fetchall()
    active: dict[str, dict[str, Any]] = {}
    for row in rows:
        if _is_expired(row["valid_until"], now):
            continue
        active[str(row["everos_memory_id"])] = dict(row)
    return active


def _clean_snippet(text: str, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def _format_profile_data(profile_data: object) -> str:
    if isinstance(profile_data, str):
        return profile_data.strip()
    if isinstance(profile_data, dict):
        parts: list[str] = []
        for k, v in profile_data.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            val = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            parts.append(f"{k}: {val}")
        return "; ".join(parts)
    return ""


def _profile_query_score(query: str, text: str) -> float:
    """Compute lightweight relevance score for unranked user profile items against the query."""
    q_clean = re.sub(r"[\s\W_]+", "", query.casefold())
    t_clean = re.sub(r"[\s\W_]+", "", text.casefold())
    if not q_clean or not t_clean:
        return 0.2
    # Strip common Chinese question stopwords so core topic characters drive overlap
    for stop in ("我是", "我的", "什么", "哪些", "怎么", "如何", "请问", "告诉我", "知道", "吗", "呢", "的", "是"):
        q_clean = q_clean.replace(stop, "")
    if not q_clean:
        return 0.35
    matched = sum(1 for ch in set(q_clean) if ch in t_clean)
    ratio = matched / max(len(set(q_clean)), 1)
    if ratio <= 0:
        return 0.02
    return min(0.95, 0.45 + 0.5 * ratio)


def _extract_verified_memory_sources(
    data: dict[str, Any],
    *,
    query: str,
    user_id: str,
    project_id: str,
    project_name: str,
    active_by_everos_id: dict[str, dict[str, Any]],
) -> list[ContextSource]:
    """Filter EverOS memory search hits through Vault's authoritative confirmed registry."""
    found: list[ContextSource] = []

    episodes = data.get("episodes")
    if isinstance(episodes, list):
        for ep in episodes:
            if not isinstance(ep, dict):
                continue
            ep_id = str(ep.get("id") or "")
            ep_row = active_by_everos_id.get(ep_id)
            if ep_row and str(ep_row["namespace"]) == project_id:
                body = str(ep.get("summary") or ep.get("episode") or "").strip()
                if body:
                    title = str(ep.get("subject") or "项目记忆").strip() or "项目记忆"
                    found.append(
                        ContextSource(
                            id=str(ep_row["id"]),
                            source_type="memory",
                            title=title[:80],
                            snippet=_clean_snippet(body),
                            project_id=project_id,
                            project_name=project_name,
                            score=float(ep.get("score") or 0.0),
                            kind=str(ep_row.get("kind") or "episode"),
                            memory_type=str(ep_row.get("memory_type") or "episode"),
                            revision=int(ep_row.get("revision") or 1),
                        )
                    )
            facts = ep.get("atomic_facts")
            if isinstance(facts, list):
                for fact in facts:
                    if not isinstance(fact, dict):
                        continue
                    fact_id = str(fact.get("id") or "")
                    fact_row = active_by_everos_id.get(fact_id)
                    if not fact_row or str(fact_row["namespace"]) != project_id:
                        continue
                    content = str(fact.get("content") or "").strip()
                    if not content:
                        continue
                    found.append(
                        ContextSource(
                            id=str(fact_row["id"]),
                            source_type="memory",
                            title=f"正式记忆（{fact_row.get('kind') or 'fact'}）",
                            snippet=_clean_snippet(content),
                            project_id=project_id,
                            project_name=project_name,
                            score=float(fact.get("score") or ep.get("score") or 0.0),
                            kind=str(fact_row.get("kind") or "fact"),
                            memory_type=str(fact_row.get("memory_type") or "atomic_fact"),
                            revision=int(fact_row.get("revision") or 1),
                        )
                    )

    profiles = data.get("profiles")
    if isinstance(profiles, list):
        for prof in profiles:
            if not isinstance(prof, dict):
                continue
            profile_data = prof.get("profile_data")
            # Canonical Vault profile items live inside explicit_info / implicit_traits
            unpacked_any = False
            if isinstance(profile_data, dict):
                for section in ("explicit_info", "implicit_traits"):
                    entries = profile_data.get(section)
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        cand_id = entry.get("memo_vault_candidate_id")
                        desc = entry.get("description")
                        if not isinstance(cand_id, str) or not cand_id or not isinstance(desc, str):
                            continue
                        unpacked_any = True
                        ev_id = f"profile_{user_id}_{cand_id}"
                        prof_row = active_by_everos_id.get(ev_id)
                        if not prof_row or str(prof_row["namespace"]) != project_id:
                            continue
                        text = desc.strip()
                        if not text:
                            continue
                        found.append(
                            ContextSource(
                                id=str(prof_row["id"]),
                                source_type="memory",
                                title="个人画像",
                                snippet=_clean_snippet(text),
                                project_id=project_id,
                                project_name=project_name,
                                score=_profile_query_score(query, text),
                                kind=str(prof_row.get("kind") or "profile"),
                                memory_type="profile",
                                revision=int(prof_row.get("revision") or 1),
                            )
                        )
            if not unpacked_any:
                prof_id = str(prof.get("id") or "")
                prof_row = active_by_everos_id.get(prof_id)
                if prof_row and str(prof_row["namespace"]) == project_id:
                    text = _format_profile_data(profile_data)
                    if text:
                        found.append(
                            ContextSource(
                                id=str(prof_row["id"]),
                                source_type="memory",
                                title="个人画像",
                                snippet=_clean_snippet(text),
                                project_id=project_id,
                                project_name=project_name,
                                score=_profile_query_score(query, text),
                                kind=str(prof_row.get("kind") or "profile"),
                                memory_type="profile",
                                revision=int(prof_row.get("revision") or 1),
                            )
                        )

    return found


def _extract_knowledge_sources(
    hits: list[dict[str, Any]],
    *,
    project_id: str,
    project_name: str,
) -> list[ContextSource]:
    found: list[ContextSource] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        topic_id = str(hit.get("topic_id") or "").strip()
        doc = hit.get("document") if isinstance(hit.get("document"), dict) else {}
        doc_id = str(doc.get("doc_id") or "").strip() or None
        doc_title = str(doc.get("title") or "").strip()
        topic_name = str(hit.get("topic_name") or "").strip()
        title = (
            f"{doc_title} · {topic_name}"
            if doc_title and topic_name and doc_title != topic_name
            else (topic_name or doc_title or "Knowledge 文档")
        )
        raw_text = str(hit.get("content") or hit.get("summary") or doc.get("summary") or "").strip()
        if not raw_text:
            continue
        found.append(
            ContextSource(
                id=topic_id or doc_id or f"knowledge:{len(found)}",
                source_type="knowledge",
                title=title[:100],
                snippet=_clean_snippet(raw_text),
                project_id=project_id,
                project_name=project_name,
                score=float(hit.get("score") or 0.0),
                kind="knowledge",
                doc_id=doc_id,
                topic_id=topic_id or None,
            )
        )
    return found


def _select_and_budget_sources(
    memory_sources: list[ContextSource],
    knowledge_sources: list[ContextSource],
) -> tuple[list[ContextSource], bool]:
    """Deduplicate, rank, and fit selected sources inside the strict model context budget."""
    dedup_mem: dict[str, ContextSource] = {}
    for item in sorted(memory_sources, key=lambda x: x.score, reverse=True):
        dedup_mem.setdefault(item.id, item)
    dedup_kn: dict[str, ContextSource] = {}
    for item in sorted(knowledge_sources, key=lambda x: x.score, reverse=True):
        key = item.topic_id or item.id
        dedup_kn.setdefault(key, item)

    mem_ranked = list(dedup_mem.values())
    kn_ranked = list(dedup_kn.values())
    trimmed = False

    selected: list[ContextSource] = []
    # Reserve balanced slots: up to MAX_MEMORY_ITEMS memories + MAX_KNOWLEDGE_ITEMS knowledge,
    # then backfill remaining capacity up to MAX_TOTAL_ITEMS.
    selected.extend(mem_ranked[:MAX_MEMORY_ITEMS])
    selected.extend(kn_ranked[:MAX_KNOWLEDGE_ITEMS])
    if len(selected) < MAX_TOTAL_ITEMS:
        leftovers = sorted(
            [*mem_ranked[MAX_MEMORY_ITEMS:], *kn_ranked[MAX_KNOWLEDGE_ITEMS:]],
            key=lambda x: x.score,
            reverse=True,
        )
        selected.extend(leftovers[: MAX_TOTAL_ITEMS - len(selected)])
    if len(mem_ranked) + len(kn_ranked) > len(selected):
        trimmed = True

    budgeted: list[ContextSource] = []
    used_chars = 0
    for item in selected:
        entry_len = len(item.title) + len(item.project_name) + len(item.snippet) + 32
        if budgeted and used_chars + entry_len > MAX_CONTEXT_CHARS:
            trimmed = True
            break
        budgeted.append(item)
        used_chars += entry_len
    return budgeted, trimmed


_LEADING_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s*UTC(?:\s*\([^)]+\))?[，,\s]*"
)


def render_context_prompt(sources: list[ContextSource]) -> str:
    """Format verified project memory/knowledge snippets with first-person mapping."""
    available = [s for s in sources if s.available]
    if not available:
        return ""
    lines = ["【历史项目记忆（关于当前提问者“我”的个人背景，仅作参考事实）】"]
    for src in available:
        body = _LEADING_TS_RE.sub("", src.snippet).strip() or src.snippet
        if src.source_type == "memory":
            body = body.replace("用户", "我")
        lines.append(f"- {src.title}：{body}")
    return "\n".join(lines)


def revalidate_context_sources(
    vault_store: VaultStore,
    result: ChatContextResult,
) -> ChatContextResult:
    """Re-verify selected memory sources against Vault SQLite right before model inference."""
    if not result.sources:
        return result
    active = _load_active_confirmed_memories(vault_store, result.subtree_project_ids)
    active_ids = {str(row["id"]) for row in active.values()}
    changed = False
    for src in result.sources:
        if src.source_type == "memory" and src.id not in active_ids:
            src.available = False
            changed = True
    if changed:
        result.context_prompt = render_context_prompt(result.sources)
        if result.status == "ready" and not any(s.available for s in result.sources):
            result.status = "empty"
    return result


def render_attached_materials_prompt(materials: list[dict[str, Any]]) -> str:
    """Format user-attached capture materials or files with numbered [M1]/[M2] source refs."""
    if not materials:
        return ""
    lines = ["【本次附带内容（当前主要讨论对象，引用时可标注对应编号如 [M1]）】"]
    for item in materials:
        ref_id = str(item.get("ref_id") or "").strip()
        title = str(item.get("title") or "").strip()
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        meta_parts = [p for p in (title, role) if p]
        meta_str = f" ({' · '.join(meta_parts)})" if meta_parts else ""
        prefix = f"[{ref_id}]{meta_str} " if ref_id else f"{meta_str.strip()} "
        lines.append(f"{prefix}{content}")
    return "\n".join(lines) if len(lines) > 1 else ""


def summarize_attached_materials(materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build compact metadata for context_meta_json and citation validation."""
    result: list[dict[str, Any]] = []
    for item in materials:
        ref_id = str(item.get("ref_id") or "").strip()
        content = str(item.get("content") or "").strip()
        if not ref_id or not content:
            continue
        result.append(
            {
                "ref_id": ref_id,
                "source_type": str(item.get("source_type") or "web_page"),
                "title": str(item.get("title") or "")[:120],
                "source_url": str(item.get("source_url") or "")[:500],
                "role": item.get("role"),
                "snippet": _clean_snippet(content, 240),
            }
        )
    return result


def build_model_messages(
    history: list[dict[str, str]],
    context_prompt: str,
    attached_prompt: str = "",
) -> tuple[list[dict[str, str]], bool]:
    """Trim older conversation turns if needed and inject attached materials + RAG context."""
    if not history:
        return [], False
    current = dict(history[-1])
    prior = [dict(m) for m in history[:-1]]
    trimmed = False

    ctx_block = context_prompt.strip()
    att_block = attached_prompt.strip()
    if att_block and ctx_block:
        combined_prefix = (
            f"{ctx_block}\n\n{att_block}\n\n"
            "请优先基于「本次附带内容」回答下面的问题；仅在需要个人背景时参考「历史项目记忆」："
        )
    elif att_block:
        combined_prefix = (
            f"{att_block}\n\n"
            "请基于「本次附带内容」回答下面的问题（若问题与附带内容无关，请直接正常回答）："
        )
    elif ctx_block:
        combined_prefix = (
            f"{ctx_block}\n\n"
            "请优先结合上述「历史项目记忆」回答下面的问题（若记忆与问题无关，请直接正常回答）："
        )
    else:
        combined_prefix = ""

    # Reserve room for prefix and current question within MAX_HISTORY_CHARS.
    budget = max(300, MAX_HISTORY_CHARS - len(combined_prefix) - len(current.get("content", "")))
    kept_prior: list[dict[str, str]] = []
    used = 0
    # Walk backwards so most recent turns win.
    for msg in reversed(prior):
        msg_len = len(str(msg.get("content", ""))) + 16
        if used + msg_len > budget:
            trimmed = True
            break
        kept_prior.append(msg)
        used += msg_len
    kept_prior.reverse()
    # Ensure prior history starts on a user turn (never orphaned assistant).
    while kept_prior and kept_prior[0].get("role") != "user":
        kept_prior.pop(0)
        trimmed = True

    if combined_prefix:
        current["content"] = f"{combined_prefix}\n{current['content']}"
    return [*kept_prior, current], trimmed


async def build_chat_context(
    *,
    vault_store: VaultStore,
    adapter: EverOSAdapter,
    project_id: str,
    history: list[dict[str, str]],
) -> ChatContextResult:
    """Retrieve and verify project-subtree formal memories and Knowledge for one chat turn."""
    projects = vault_store.list_projects()
    project_names = {
        str(p["project_id"]): str(p.get("full_path") or p.get("name") or p["project_id"])
        for p in projects
    }
    subtree_ids = vault_store.expand_project_scope([project_id])
    if not subtree_ids:
        subtree_ids = [project_id]

    query = build_retrieval_query(history)
    if not query:
        return ChatContextResult(
            status="empty",
            project_id=project_id,
            subtree_project_ids=subtree_ids,
            query="",
            memory_status="skipped",
            knowledge_status="skipped",
        )

    identity = vault_store.get_identity()
    app_id = identity["app_id"]
    user_id = identity["user_id"]
    active_by_everos_id = _load_active_confirmed_memories(vault_store, subtree_ids)

    async def _search_mem(pid: str) -> tuple[str, dict[str, Any]]:
        res = await adapter.search_memories(
            query=query,
            app_id=app_id,
            project_id=pid,
            user_id=user_id,
            top_k=8,
            method="hybrid",
            include_profile=True,
        )
        return pid, res

    async def _search_kn(pid: str) -> tuple[str, list[dict[str, Any]]]:
        res = await adapter.search_knowledge(
            query=query,
            app_id=app_id,
            project_id=pid,
            top_k=6,
            method="hybrid",
            include_content=True,
        )
        return pid, res

    mem_pids = [
        pid
        for pid in subtree_ids
        if any(str(r.get("namespace")) == pid for r in active_by_everos_id.values())
    ]
    mem_tasks = [_search_mem(pid) for pid in mem_pids]
    kn_tasks = [_search_kn(pid) for pid in subtree_ids]
    results = await asyncio.gather(*mem_tasks, *kn_tasks, return_exceptions=True)
    mem_results = results[: len(mem_tasks)]
    kn_results = results[len(mem_tasks) :]

    memory_sources: list[ContextSource] = []
    mem_ok = 0
    mem_err = 0
    for res in mem_results:
        if isinstance(res, BaseException):
            mem_err += 1
            continue
        mem_ok += 1
        pid, data = res
        memory_sources.extend(
            _extract_verified_memory_sources(
                data,
                query=query,
                user_id=user_id,
                project_id=pid,
                project_name=project_names.get(pid, pid),
                active_by_everos_id=active_by_everos_id,
            )
        )

    knowledge_sources: list[ContextSource] = []
    kn_ok = 0
    kn_err = 0
    for res in kn_results:
        if isinstance(res, BaseException):
            kn_err += 1
            continue
        kn_ok += 1
        pid, hits = res
        knowledge_sources.extend(
            _extract_knowledge_sources(
                hits,
                project_id=pid,
                project_name=project_names.get(pid, pid),
            )
        )

    memory_status: SourceStatus = (
        "skipped"
        if not mem_pids
        else ("failed" if mem_ok == 0 and mem_err > 0 else ("partial" if mem_err > 0 else "ok"))
    )
    knowledge_status: SourceStatus = (
        "failed" if kn_ok == 0 and kn_err > 0 else ("partial" if kn_err > 0 else "ok")
    )

    if knowledge_status == "failed" and memory_status in ("failed", "skipped"):
        return ChatContextResult(
            status="failed",
            project_id=project_id,
            subtree_project_ids=subtree_ids,
            query=query,
            memory_status=memory_status,
            knowledge_status=knowledge_status,
            error_message="项目记忆与 Knowledge 检索均失败，已中止本轮回答。请检查 EverOS 服务状态后重试，或关闭「使用项目记忆」继续对话。",
        )

    selected, trimmed = _select_and_budget_sources(memory_sources, knowledge_sources)
    context_prompt = render_context_prompt(selected)

    mem_degraded = memory_status in ("failed", "partial")
    kn_degraded = knowledge_status in ("failed", "partial")
    if mem_degraded or kn_degraded:
        overall: ContextStatus = "partial_failure"
        failed_parts = []
        if mem_degraded:
            failed_parts.append("正式记忆")
        if kn_degraded:
            failed_parts.append("Knowledge")
        err_msg = f"{'、'.join(failed_parts)}检索部分失败，已使用其余可用参考资料。"
    elif not selected:
        overall = "empty"
        err_msg = None
    else:
        overall = "ready"
        err_msg = None

    return ChatContextResult(
        status=overall,
        project_id=project_id,
        subtree_project_ids=subtree_ids,
        query=query,
        memory_status=memory_status,
        knowledge_status=knowledge_status,
        sources=selected,
        context_prompt=context_prompt,
        trimmed=trimmed,
        error_message=err_msg,
    )
