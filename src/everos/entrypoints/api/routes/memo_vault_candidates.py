"""MEMO Vault candidate extraction and approved-memory publishing endpoints.

Unlike ``/memory/flush``, extraction runs only the chat extraction algorithms
and returns drafts. A separate publish endpoint writes only user-approved text
to EverOS' existing user atomic-fact store.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from everalgo.llm import LLMError

from everos.component.llm import LLMNotConfiguredError, get_llm_client
from everos.config import load_settings
from everos.core.errors import ExternalServiceError, ProviderNotConfiguredError
from everos.core.observability.logging import get_logger
from everos.core.persistence import MemoryRoot
from everos.entrypoints.api.routes.memorize import PathSafeId
from everos.entrypoints.api.utils import extract_request_id
from everos.infra.persistence.markdown import (
    AtomicFactWriter, EpisodeWriter, ProfileReader, ProfileWriter,
    UserProfileFrontmatter,
)
from everos.memory.prompt_slots import PromptLoader
from everos.service.memo_vault_candidates import (
    CandidateExtractionFormatError,
    extract_chat_candidates,
)
from everos.service.memo_vault_lifecycle import (
    LifecycleConflict,
    LifecycleNotFound,
    mutate_published_memory,
)

router = APIRouter(prefix="/memo-vault/candidates", tags=["memo-vault-candidates"])
logger = get_logger(__name__)


class CandidateMessageDTO(BaseModel):
    id: PathSafeId = Field(min_length=1, max_length=128)
    role: Literal["user", "assistant"]
    # Long exports are split into bounded MemCell inputs by the extraction service.
    content: str = Field(max_length=1_000_000)
    timestamp_ms: int = Field(gt=0)


class CandidateExtractionRequest(BaseModel):
    batch_id: PathSafeId = Field(min_length=1, max_length=128)
    session_id: PathSafeId = Field(min_length=1, max_length=128)
    app_id: PathSafeId = Field(min_length=1, max_length=128)
    project_id: PathSafeId = Field(min_length=1, max_length=128)
    user_id: PathSafeId = Field(min_length=1, max_length=128)
    # Reserved for future structured tool-trajectory extraction. This chat-only
    # route never uses it as a memory owner or sends it to AgentMemoryPipeline.
    agent_id: PathSafeId = Field(min_length=1, max_length=128)
    messages: list[CandidateMessageDTO] = Field(min_length=1, max_length=500)


class CandidateExtractionResponse(BaseModel):
    status: Literal["extracted"] = "extracted"
    method: Literal["local"] = "local"
    candidate_count: int
    candidates: list[dict[str, object]]
    candidate_scope: dict[str, str]


class ApprovedCandidateRequest(BaseModel):
    candidate_id: PathSafeId = Field(min_length=1, max_length=128)
    app_id: PathSafeId = Field(min_length=1, max_length=128)
    project_id: PathSafeId = Field(min_length=1, max_length=128)
    user_id: PathSafeId = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=4000)
    memory_type: Literal["atomic_fact", "episode", "profile"] = "atomic_fact"
    metadata: dict[str, object] | None = None


class ApprovedCandidateData(BaseModel):
    memory_id: str
    scope: str
    content: str
    revision: int = 1


class MemoryLifecycleRequest(BaseModel):
    candidate_id: PathSafeId = Field(min_length=1, max_length=128)
    memory_id: PathSafeId = Field(min_length=1, max_length=180)
    app_id: PathSafeId = Field(min_length=1, max_length=128)
    project_id: PathSafeId = Field(min_length=1, max_length=128)
    user_id: PathSafeId = Field(min_length=1, max_length=128)
    memory_type: Literal["atomic_fact", "episode", "profile"]
    action: Literal["correct", "withdraw", "delete"]
    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=4000)


class MemoryLifecycleData(BaseModel):
    memory_id: str
    lifecycle_status: Literal["active", "withdrawn", "deleted"]
    revision: int
    content: str


class SuccessEnvelope[T](BaseModel):
    request_id: str
    data: T


@router.post("/extract", response_model=SuccessEnvelope[CandidateExtractionResponse])
async def extract_candidates(
    body: CandidateExtractionRequest, request: Request
) -> SuccessEnvelope[CandidateExtractionResponse]:
    settings = load_settings()
    try:
        candidates = await extract_chat_candidates(
            batch_id=body.batch_id,
            session_id=body.session_id,
            user_id=body.user_id,
            messages=[message.model_dump() for message in body.messages],
            llm_client=get_llm_client(),
            prompt_loader=PromptLoader(Path(__file__).resolve().parents[3] / "config"),
            hard_token_limit=settings.boundary_detection.hard_token_limit,
            hard_msg_limit=settings.boundary_detection.hard_msg_limit,
        )
    except LLMNotConfiguredError as exc:
        raise ProviderNotConfiguredError(
            provider="llm", feature="memo_vault_candidate_extraction"
        ) from exc
    except CandidateExtractionFormatError as exc:
        stage_label = {
            "boundary": "会话切分",
            "episode": "情节",
            "atomic_fact": "事实",
            "preference": "画像",
            "profile": "画像",
        }[exc.stage]
        message = (
            f"候选抽取的{stage_label}输出多次达到长度上限，结构化结果被截断；"
            "本次结果未保存，可以调整模型或缩短输入后重试。"
            if exc.reason == "output_truncated"
            else f"候选抽取模型多次返回无效的{stage_label}格式；本次结果未保存，可以稍后重试。"
        )
        error = ExternalServiceError(message)
        error.stage = {"boundary": "model_boundary", "episode": "model_episode", "atomic_fact": "model_atomic_fact", "preference": "model_preference", "profile": "model_profile"}[exc.stage]
        raise error from exc
    except LLMError as exc:
        logger.warning(
            "memo_vault_candidate_model_call_failed",
            error_type=type(exc).__name__,
            cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
        )
        error = ExternalServiceError(
            "候选抽取模型调用失败；请检查本地 Ollama 的结构化输出支持与运行日志。"
        )
        error.stage = "model_call"
        raise error from exc

    return SuccessEnvelope(
        request_id=extract_request_id(request),
        data=CandidateExtractionResponse(
            candidate_count=len(candidates),
            candidates=candidates,
            candidate_scope={
                "app_id": body.app_id,
                "project_id": body.project_id,
                "user_id": body.user_id,
            },
        ),
    )


@router.post(
    "/{candidate_id}/publish", response_model=SuccessEnvelope[ApprovedCandidateData]
)
async def publish_approved_candidate(
    candidate_id: str, body: ApprovedCandidateRequest, request: Request
) -> SuccessEnvelope[ApprovedCandidateData]:
    """Store approved text in its EverOS user-memory type; retries are idempotent."""
    if candidate_id != body.candidate_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CANDIDATE_ID_MISMATCH",
                "message": "URL and body candidate IDs must match.",
            },
        )
    content = body.content.strip()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "EMPTY_APPROVED_MEMORY",
                "message": "Approved memory content cannot be empty.",
            },
        )
    root = MemoryRoot.resolve()
    scope = f"{body.app_id}/{body.project_id}/users/{body.user_id}"
    ledger_path = root.root / ".index" / "sqlite" / "memo_vault_published.db"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path, timeout=30) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS published_candidates ("
            "candidate_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, "
            "scope TEXT NOT NULL, content TEXT NOT NULL, "
            "memory_type TEXT NOT NULL DEFAULT 'atomic_fact', "
            "lifecycle_status TEXT NOT NULL DEFAULT 'active', "
            "revision INTEGER NOT NULL DEFAULT 1, operation_action TEXT)"
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(published_candidates)")}
        for column, definition in {
            "memory_type": "TEXT NOT NULL DEFAULT 'atomic_fact'",
            "lifecycle_status": "TEXT NOT NULL DEFAULT 'active'",
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "operation_action": "TEXT",
        }.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE published_candidates ADD COLUMN {column} {definition}")
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
                "SELECT memory_id, scope, content, memory_type, lifecycle_status, revision FROM published_candidates "
                "WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if previous is not None:
            if previous[4] != "active":
                raise HTTPException(status_code=409, detail={"code": "MEMORY_NOT_ACTIVE", "message": "This memory has already been withdrawn or deleted."})
            if previous[1] != scope or previous[2] != content or previous[3] != body.memory_type:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "CANDIDATE_ALREADY_PUBLISHED",
                        "message": (
                            "This candidate was already published with different "
                            "content or scope."
                        ),
                    },
                )
            data = ApprovedCandidateData(
                memory_id=previous[0], scope=previous[1], content=previous[2], revision=previous[5]
            )
        else:
            metadata = body.metadata or {}
            inline = {
                "owner_id": body.user_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": f"memo_vault_{candidate_id}",
                "memo_vault_candidate_id": candidate_id,
                "parent_type": "memcell",
                "parent_id": "",
            }
            if body.memory_type == "atomic_fact":
                entry_id = await AtomicFactWriter(root=root).append_entry(
                    body.user_id, inline=inline, sections={"Fact": content},
                    app_id=body.app_id, project_id=body.project_id,
                )
                memory_id = f"{body.user_id}_{entry_id.format()}"
            elif body.memory_type == "episode":
                title = metadata.get("title")
                timestamp_ms = metadata.get("timestamp_ms")
                if not isinstance(title, str) or not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
                    raise HTTPException(status_code=422, detail={"code": "INVALID_CANDIDATE_METADATA", "message": "Episode requires a title and timestamp."})
                inline["timestamp"] = datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat()
                inline["parent_id"] = f"memo_vault_{candidate_id}"
                inline["sender_ids"] = [body.user_id]
                entry_id = await EpisodeWriter(root=root).append_entry(
                    body.user_id, inline=inline,
                    sections={"Subject": title.strip() or "导入情节", "Content": content},
                    app_id=body.app_id, project_id=body.project_id,
                )
                memory_id = f"{body.user_id}_{entry_id.format()}"
            else:
                section = metadata.get("section")
                item = metadata.get("item")
                timestamp_ms = metadata.get("timestamp_ms")
                if section not in {"explicit_info", "implicit_traits"} or not isinstance(item, dict) or not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
                    raise HTTPException(status_code=422, detail={"code": "INVALID_CANDIDATE_METADATA", "message": "Profile requires a section, item, and timestamp."})
                item = {**item, "description": content, "memo_vault_candidate_id": candidate_id}
                reader = ProfileReader(root=root)
                existing = await reader.read(body.user_id, schema=UserProfileFrontmatter, app_id=body.app_id, project_id=body.project_id)
                if existing:
                    frontmatter, _ = existing
                else:
                    frontmatter = UserProfileFrontmatter(id=f"profile_{body.user_id}", user_id=body.user_id)
                items = list(getattr(frontmatter, section))
                if not any(isinstance(value, dict) and value.get("memo_vault_candidate_id") == candidate_id for value in items):
                    items.append(item)
                frontmatter = frontmatter.model_copy(update={
                    section: items,
                    "profile_timestamp_ms": max(frontmatter.profile_timestamp_ms, timestamp_ms),
                })
                summary = "\n".join(str(value.get("description", "")) for bucket in (frontmatter.explicit_info, frontmatter.implicit_traits) for value in bucket if isinstance(value, dict))
                frontmatter = frontmatter.model_copy(update={"summary": summary})
                await ProfileWriter(root=root).write(
                    body.user_id, frontmatter=frontmatter, body=summary,
                    app_id=body.app_id, project_id=body.project_id,
                )
                memory_id = f"profile_{body.user_id}_{candidate_id}"
            connection.execute(
                "INSERT INTO published_candidates "
                "(candidate_id, memory_id, scope, content, memory_type, lifecycle_status, revision) "
                "VALUES (?, ?, ?, ?, ?, 'active', 1)",
                (candidate_id, memory_id, scope, content, body.memory_type),
            )
            data = ApprovedCandidateData(
                memory_id=memory_id, scope=scope, content=content
            )
    return SuccessEnvelope(request_id=extract_request_id(request), data=data)


@router.post("/{candidate_id}/lifecycle", response_model=SuccessEnvelope[MemoryLifecycleData])
async def apply_memory_lifecycle(
    candidate_id: str, body: MemoryLifecycleRequest, request: Request
) -> SuccessEnvelope[MemoryLifecycleData]:
    """Correct, withdraw, or delete one exact Vault-published native record."""
    if candidate_id != body.candidate_id:
        raise HTTPException(status_code=422, detail={"code": "CANDIDATE_ID_MISMATCH", "message": "URL and body candidate IDs must match."})
    root = MemoryRoot.resolve()
    ledger_path = root.root / ".index" / "sqlite" / "memo_vault_published.db"
    try:
        result = await mutate_published_memory(
            root=root,
            ledger_path=ledger_path,
            candidate_id=body.candidate_id,
            memory_id=body.memory_id,
            app_id=body.app_id,
            project_id=body.project_id,
            user_id=body.user_id,
            memory_type=body.memory_type,
            action=body.action,
            expected_revision=body.expected_revision,
            content=body.content.strip() if body.content else None,
            request=request,
        )
    except LifecycleNotFound as exc:
        raise HTTPException(status_code=404, detail={"code": "MEMORY_NOT_FOUND", "message": str(exc)}) from exc
    except LifecycleConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "MEMORY_LIFECYCLE_CONFLICT", "message": str(exc)}) from exc
    return SuccessEnvelope(request_id=extract_request_id(request), data=MemoryLifecycleData.model_validate(result))
