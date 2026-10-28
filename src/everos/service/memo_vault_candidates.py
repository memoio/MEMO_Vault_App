"""Candidate-only extraction for MEMO Vault imports.

This path deliberately reuses EverOS' chat boundary and episode extractors,
with a Vault-specific evidence-backed preference prompt, without dispatching
the normal memory pipelines. Its output is a draft for the Vault review queue;
it never writes approved memory. The standalone atomic-fact extractor remains
available but is not called here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
import re
from difflib import SequenceMatcher
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError
from openai import LengthFinishReasonError

from everalgo._tokenize import count_tokens
from everalgo.boundary import detect_boundaries
from everalgo.llm import LLMError
from everalgo.llm.types import ChatMessage as LLMChatMessage
from everalgo.prompts import render_prompt
from everalgo.user_memory.prompts.en.profile import PROFILE_INITIAL_EXTRACTION_PROMPT
from everalgo.user_memory.prompts.en.episode import EPISODE_GENERATION_PROMPT, DEFAULT_CUSTOM_INSTRUCTIONS
from everalgo.types import ChatMessage, MemCell
from everalgo.user_memory import AtomicFactExtractor, EpisodeExtractor, ProfileExtractor

from everos.core.observability.logging import get_logger
from everos.service.memo_vault_candidate_prompts import (
    native_prompt_in_chinese,
    PREFERENCE_PROMPT_ZH,
    PROFILE_QUALITY_RULES_ZH,
)

if TYPE_CHECKING:
    from everalgo.llm.protocols import LLMClient
    from everalgo.llm.types import ChatResponse

    from everos.memory.prompt_slots import PromptLoader

logger = get_logger(__name__)
_ATOMIC_FACT_MAX_ATTEMPTS = 3
_STRUCTURED_MAX_ATTEMPTS = _ATOMIC_FACT_MAX_ATTEMPTS
_CHUNK_TOKEN_BUDGET = 2200
_MESSAGE_TOKEN_BUDGET = 1400
_CHUNK_MESSAGE_LIMIT = 20
_OUTPUT_TOKEN_BUDGET = 2048
type ExtractionStage = Literal["boundary", "episode", "atomic_fact", "preference", "profile"]


class _BoundaryOutput(BaseModel):
    reasoning: str
    boundaries: list[int]
    should_wait: bool


class _EpisodeOutput(BaseModel):
    title: str
    content: str


class _AtomicFactsBody(BaseModel):
    time: str
    atomic_fact: list[str]


class _AtomicFactsOutput(BaseModel):
    atomic_facts: _AtomicFactsBody


class _PreferenceItem(BaseModel):
    content: str
    evidence: str


class _PreferenceOutput(BaseModel):
    preferences: list[_PreferenceItem]


@dataclass(frozen=True, slots=True)
class _MessagePiece:
    """One exact text span supplied to EverAlgo from an archived message."""

    message: ChatMessage
    char_start: int
    char_end: int


class _StructuredOutputClient:
    """Constrain only the MEMO Vault extractor calls to a JSON schema."""

    def __init__(self, inner: LLMClient, schema: type[BaseModel]) -> None:
        self._inner = inner
        self._schema = schema

    async def chat(
        self,
        messages: list[LLMChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: type[BaseModel] | None = None,
        **extra: Any,
    ) -> ChatResponse:
        response = await self._inner.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=min(max_tokens or _OUTPUT_TOKEN_BUDGET, _OUTPUT_TOKEN_BUDGET),
            response_format=self._schema,
            **extra,
        )
        if response.parsed is None:
            raise ValueError("Structured response has no parsed JSON object.")
        return response.model_copy(
            update={"content": _json_for_everalgo(response.parsed.model_dump_json())}
        )


def _json_for_everalgo(value: str) -> str:
    """Escape braces inside JSON strings for EverAlgo's brace counter."""
    output: list[str] = []
    in_string = False
    escaped = False
    for character in value:
        if in_string and character in "{}" and not escaped:
            output.append("\\u007b" if character == "{" else "\\u007d")
        else:
            output.append(character)
        if character == '"' and not escaped:
            in_string = not in_string
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return "".join(output)


class CandidateExtractionFormatError(RuntimeError):
    """Raised after a candidate extractor repeatedly returns invalid JSON."""

    def __init__(self, message: str, *, stage: ExtractionStage = "atomic_fact",
                 reason: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.reason = reason


async def extract_chat_candidates(
    *,
    batch_id: str,
    session_id: str,
    user_id: str,
    messages: list[Mapping[str, Any]],
    llm_client: LLMClient,
    prompt_loader: PromptLoader,
    hard_token_limit: int,
    hard_msg_limit: int,
) -> list[dict[str, object]]:
    """Extract Episode and Profile drafts without persisting them.

    Both ``user`` and ``assistant`` turns are assigned to ``user_id``. The
    reserved agent identity is intentionally absent from this function: only a
    future structured tool-trajectory extractor should write to that track.
    """
    chat_messages = [
        ChatMessage(
            id=str(message["id"]),
            role=str(message["role"]),
            sender_id=user_id,
            sender_name=str(message["role"]),
            content=str(message.get("content", "")),
            timestamp=int(message["timestamp_ms"]),
        )
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]
    if not chat_messages:
        return []

    cells: list[tuple[MemCell, list[_MessagePiece]]] = []
    chunks = _chunk_messages(chat_messages)
    logger.info(
        "memo_vault_candidate_input_chunked",
        message_count=len(chat_messages),
        chunk_count=len(chunks),
    )
    for chunk_index, chunk in enumerate(chunks):
        boundary = await _retry_structured_output(
            "boundary",
            lambda: detect_boundaries(
                [piece.message for piece in chunk],
                llm=_StructuredOutputClient(llm_client, _BoundaryOutput),
                is_final=True,
                prompt=prompt_loader.load("boundary_detection"),
                hard_token_limit=min(hard_token_limit, _CHUNK_TOKEN_BUDGET),
                hard_msg_limit=min(hard_msg_limit, _CHUNK_MESSAGE_LIMIT),
            ),
            unit_index=chunk_index,
        )
        cursor = 0
        for cell in boundary.cells:
            source_pieces, cursor = _source_pieces_for_cell(cell, chunk, cursor)
            cells.append((cell, source_pieces))
    episode_extractor = EpisodeExtractor(
        llm=_StructuredOutputClient(llm_client, _EpisodeOutput)
    )
    candidates: list[dict[str, object]] = []
    seen_preferences: list[str] = []

    for cell_index, (cell, source_pieces) in enumerate(cells):
        source_message_ids = list(dict.fromkeys(str(item.id) for item in cell.items if item.id))
        if not source_message_ids or not any(item.role == "user" for item in cell.items):
            continue
        source_segment = _source_segment(source_pieces) if source_pieces else None
        episode = await _retry_structured_output(
            "episode",
            lambda: _extract_episode_checked(episode_extractor, cell),
            unit_index=cell_index,
        )
        episode_content = episode.episode.strip()
        if episode_content:
            candidates.append({
                "candidate_id": _candidate_id(batch_id, session_id, "episode", cell_index, episode_content),
                "memory_type": "episode",
                "kind": "episode",
                "content": episode_content,
                "source_message_ids": source_message_ids,
                **({"source_segment": source_segment} if source_segment else {}),
                "metadata": {"title": str(getattr(episode, "subject", "") or ""), "timestamp_ms": episode.timestamp},
            })
        user_pieces = [piece for piece in source_pieces if piece.message.role == "user"]
        if not user_pieces:
            continue
        user_cell = cell.model_copy(update={"items": [piece.message for piece in user_pieces]})
        try:
            profile = await _retry_structured_output(
                "profile",
                lambda: ProfileExtractor(llm=llm_client).aextract(
                    [user_cell], sender_id=user_id,
                    prompt=native_prompt_in_chinese(PROFILE_INITIAL_EXTRACTION_PROMPT) + PROFILE_QUALITY_RULES_ZH,
                ),
                unit_index=cell_index,
            )
        except (CandidateExtractionFormatError, LLMError) as exc:
            logger.warning("memo_vault_profile_extraction_skipped", unit_index=cell_index,
                           error_type=type(exc).__name__, error_code=_safe_error_code(exc))
            continue
        for section in ("explicit_info", "implicit_traits"):
            for item in getattr(profile, section, []) or []:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("description", "")).strip().replace(user_id, "用户")
                if not content or len(content) > 4000:
                    continue
                evidence = item.get("evidence")
                evidence_pieces = _pieces_matching_evidence(user_pieces, evidence) if isinstance(evidence, str) else []
                if not evidence_pieces or not _contains_han(content) or _is_trivial_profile(content):
                    continue
                if any(profile_contents_duplicate(content, previous) for previous in seen_preferences):
                    continue
                seen_preferences.append(content)
                item = {**item, "description": content}
                candidates.append({
                    "candidate_id": _candidate_id(batch_id, session_id, "profile", cell_index, content),
                    "memory_type": "profile",
                    "kind": "preference",
                    "content": content,
                    "source_message_ids": list(dict.fromkeys(str(piece.message.id) for piece in evidence_pieces)),
                    "source_segment": _source_segment(evidence_pieces),
                    "metadata": {"section": section, "item": item, "timestamp_ms": profile.timestamp},
                })

    return candidates


async def _extract_atomic_facts_with_retry(
    extractor: AtomicFactExtractor,
    text: str,
    *,
    timestamp: int,
    unit_index: int | None = None,
) -> list[Any]:
    return await _retry_structured_output(
        "atomic_fact",
        lambda: extractor.aextract_from_text(text, timestamp=timestamp),
        unit_index=unit_index,
    )


async def _extract_preferences(
    llm_client: LLMClient,
    *,
    user_id: str,
    source_pieces: list[_MessagePiece],
) -> _PreferenceOutput:
    """Extract only evidence-backed preferences from actual user messages."""
    conversation = "\n".join(
        f"用户原文：{''.join(piece.message.content for piece in group).strip()}"
        for group in _contiguous_piece_groups(source_pieces)
        if any(str(piece.message.content).strip() for piece in group)
    )
    rendered = render_prompt(
        PREFERENCE_PROMPT_ZH,
    PROFILE_QUALITY_RULES_ZH,
        None,
        target_user=user_id,
        conversation_text=conversation,
    )
    response = await llm_client.chat(
        messages=[LLMChatMessage(role="user", content=rendered)],
        max_tokens=_OUTPUT_TOKEN_BUDGET,
    )
    raw = response.content
    start = raw.find("{")
    if start < 0:
        raise ValueError("Preference extraction returned no JSON object.")
    payload, _ = json.JSONDecoder().raw_decode(raw[start:])
    return _PreferenceOutput.model_validate(payload)


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _contains_normalized(source: str, excerpt: str) -> bool:
    normalized_source = _normalized_text(source)
    normalized_excerpt = _normalized_text(excerpt)
    return bool(normalized_excerpt and normalized_excerpt in normalized_source)


def _contiguous_piece_groups(pieces: list[_MessagePiece]) -> list[list[_MessagePiece]]:
    groups: list[list[_MessagePiece]] = []
    for piece in pieces:
        if (
            groups
            and groups[-1][-1].message.id == piece.message.id
            and groups[-1][-1].char_end == piece.char_start
        ):
            groups[-1].append(piece)
        else:
            groups.append([piece])
    return groups


def _pieces_matching_evidence(
    pieces: list[_MessagePiece], evidence: str
) -> list[_MessagePiece]:
    return [
        piece
        for group in _contiguous_piece_groups(pieces)
        if _contains_normalized(
            "".join(piece.message.content for piece in group), evidence
        )
        for piece in group
    ]


def _contains_han(value: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in value)


def _has_explicit_preference_signal(evidence: str) -> bool:
    """Reject profile-like statements that lack an explicit preference cue."""
    text = _normalized_text(evidence)
    markers = (
        "我喜欢", "我更喜欢", "我偏好", "我偏爱", "我不喜欢", "我更倾向", "我习惯",
        "请以后", "以后请", "今后请", "后续请", "回答时请", "请用", "请使用",
        "请避免", "不要用", "不要使用", "别用", "尽量用", "我希望你", "我希望回答",
        "i prefer", "i like", "i don't like", "i dislike", "please use", "please always",
        "please avoid", "from now on", "in future", "i'd like you to", "i would like you to",
    )
    return any(marker in text for marker in markers)


def _safe_error_code(exc: Exception) -> str | None:
    value = getattr(exc, "code", None) or getattr(exc, "error_code", None)
    return value[:80] if isinstance(value, str) else None


async def _retry_structured_output[T](
    stage: ExtractionStage,
    operation: Callable[[], Awaitable[T]],
    *,
    unit_index: int | None = None,
) -> T:
    """Retry invalid model output without recording conversation or model text."""
    for attempt in range(1, _STRUCTURED_MAX_ATTEMPTS + 1):
        try:
            return await operation()
        except LLMError as exc:
            if not isinstance(exc.__cause__, LengthFinishReasonError):
                raise
            logger.warning(
                "memo_vault_candidate_output_truncated",
                stage=stage,
                unit_index=unit_index,
                attempt=attempt,
                max_attempts=_STRUCTURED_MAX_ATTEMPTS,
            )
            if attempt == _STRUCTURED_MAX_ATTEMPTS:
                raise CandidateExtractionFormatError(
                    f"{stage} extraction output was repeatedly truncated.",
                    stage=stage,
                    reason="output_truncated",
                ) from None
            await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            logger.warning(
                "memo_vault_candidate_invalid_json",
                stage=stage,
                unit_index=unit_index,
                attempt=attempt,
                max_attempts=_STRUCTURED_MAX_ATTEMPTS,
                error_type=type(exc).__name__,
                error_reason=exc.msg if isinstance(exc, json.JSONDecodeError) else None,
                error_position=getattr(exc, "pos", None),
            )
            if attempt == _STRUCTURED_MAX_ATTEMPTS:
                raise CandidateExtractionFormatError(
                    f"{stage} extraction returned invalid structured output.",
                    stage=stage,
                ) from None
            await asyncio.sleep(0.25 * (2 ** (attempt - 1)))


def _split_message(message: ChatMessage) -> list[_MessagePiece]:
    """Split long text exactly, keeping the archive message ID on every piece."""
    content = message.content
    if not isinstance(content, str) or count_tokens(content) <= _MESSAGE_TOKEN_BUDGET:
        return [_MessagePiece(message, 0, len(str(content)))]
    pieces: list[_MessagePiece] = []
    remaining = content
    char_start = 0
    while remaining:
        if count_tokens(remaining) <= _MESSAGE_TOKEN_BUDGET:
            pieces.append(
                _MessagePiece(
                    message.model_copy(update={"content": remaining}),
                    char_start,
                    char_start + len(remaining),
                )
            )
            break
        low, high, cut = 1, len(remaining), 1
        while low <= high:
            middle = (low + high) // 2
            if count_tokens(remaining[:middle]) <= _MESSAGE_TOKEN_BUDGET:
                cut = middle
                low = middle + 1
            else:
                high = middle - 1
        paragraph_break = remaining.rfind("\n", cut * 3 // 5, cut)
        if paragraph_break > 0:
            cut = paragraph_break + 1
        piece_content = remaining[:cut]
        pieces.append(
            _MessagePiece(
                message.model_copy(update={"content": piece_content}),
                char_start,
                char_start + len(piece_content),
            )
        )
        remaining = remaining[cut:]
        char_start += cut
    return pieces


def _message_tokens(message: ChatMessage) -> int:
    """Allow room for role, timestamp and the extractor's message rendering."""
    return count_tokens(str(message.content)) + 48


def _chunk_messages(messages: list[ChatMessage]) -> list[list[_MessagePiece]]:
    chunks: list[list[_MessagePiece]] = []
    current: list[_MessagePiece] = []
    token_count = 0
    for message in messages:
        for piece in _split_message(message):
            cost = _message_tokens(piece.message)
            if current and (
                token_count + cost > _CHUNK_TOKEN_BUDGET
                or len(current) >= _CHUNK_MESSAGE_LIMIT
            ):
                chunks.append(current)
                current = []
                token_count = 0
            current.append(piece)
            token_count += cost
    if current:
        chunks.append(current)
    return chunks


def _source_pieces_for_cell(
    cell: MemCell,
    chunk: list[_MessagePiece],
    cursor: int,
) -> tuple[list[_MessagePiece], int]:
    """Map a detected cell back to the ordered spans sent to boundary detection."""
    selected: list[_MessagePiece] = []
    for item in cell.items:
        target = _source_piece_key(item)
        while cursor < len(chunk) and _source_piece_key(chunk[cursor].message) != target:
            cursor += 1
        if cursor < len(chunk):
            selected.append(chunk[cursor])
            cursor += 1
    return (selected if len(selected) == len(cell.items) else []), cursor


def _source_piece_key(message: ChatMessage) -> tuple[str, str, int, str]:
    return (str(message.id), str(message.role), int(message.timestamp), str(message.content))


def _source_segment(pieces: list[_MessagePiece]) -> dict[str, object]:
    """Describe the literal ordered spans consumed by one MemCell extractor call."""
    texts = [str(piece.message.content) for piece in pieces]
    encoded_texts = json.dumps(texts, ensure_ascii=False, separators=(",", ":"))
    return {
        "version": 1,
        "precision": "exact_input",
        "input_sha256": hashlib.sha256(encoded_texts.encode("utf-8")).hexdigest(),
        "items": [
            {
                "message_id": str(piece.message.id),
                "char_start": piece.char_start,
                "char_end": piece.char_end,
            }
            for piece in pieces
        ],
    }


def _candidate_id(batch_id: str, session_id: str, kind: str, index: int, content: str) -> str:
    return hashlib.sha256(f"{batch_id}:{session_id}:{kind}:{index}:{content}".encode("utf-8")).hexdigest()


def _is_trivial_profile(content: str) -> bool:
    return any(phrase in content for phrase in (
        "提供了文件内容", "提供了详细信息", "提供了文件", "希望获得帮助", "进行了交流",
    ))


def profile_contents_duplicate(left: str, right: str) -> bool:
    """Conservative text deduplication; preserve polarity, quantities and conditions."""
    def normalize(value: str) -> str:
        return "".join(c for c in _normalized_text(value) if c.isalnum())
    a, b = normalize(left), normalize(right)
    if a == b:
        return True
    markers = ("不", "没", "无", "仅", "只", "如果", "除非", "以前", "现在")
    if any(a.count(marker) != b.count(marker) for marker in markers):
        return False
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    return min(len(a), len(b)) >= 12 and SequenceMatcher(None, a, b).ratio() >= 0.90


async def _extract_episode_checked(extractor: EpisodeExtractor, cell: MemCell):
    episode = await extractor.aextract(
        cell, sender_id=None,
        prompt=native_prompt_in_chinese(EPISODE_GENERATION_PROMPT),
        custom_instructions=DEFAULT_CUSTOM_INSTRUCTIONS + (
            "\n使用中文第三人称陈述实际发生的事件，区分用户需求、助手建议和已确认结果。"
            "不得把助手的问句、服务邀约或未执行的建议原样作为情节；不把建议写成已经完成。"
        ),
    )
    content = episode.episode.strip()
    if not _contains_han(content) or not _contains_han(str(episode.subject)):
        raise ValueError("Episode output must be Chinese")
    normalized = _normalized_text(content)
    if any(normalized == _normalized_text(str(item.content)) for item in cell.items if item.role == "assistant"):
        raise ValueError("Episode must summarize the event rather than copy an assistant response")
    return episode
