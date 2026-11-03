from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import everos.service.memo_vault_candidates as extraction
from everalgo.types import MemCell


def test_configurable_chunk_budget_preserves_exact_source_spans():
    from everalgo.types import ChatMessage
    text = "abcdefghij" * 500
    message = ChatMessage(id="m", role="user", sender_id="u", sender_name="u", content=text, timestamp=1000)
    chunks = extraction._chunk_messages([message], token_counter=len, chunk_token_budget=3000, message_token_budget=2400)
    pieces = [piece for chunk in chunks for piece in chunk]
    assert len(pieces) == 3
    assert "".join(piece.message.content for piece in pieces) == text
    assert [(piece.char_start, piece.char_end) for piece in pieces] == [(0, 2400), (2400, 4800), (4800, 5000)]


def test_context_guard_rejects_oversized_prompt_before_call():
    from everalgo.llm.types import ChatMessage
    class Client:
        async def chat(self, *args, **kwargs):
            raise AssertionError("oversized request must not reach model")
    client = extraction._ContextGuardClient(Client(), token_counter=len, context_tokens=2300)
    with pytest.raises(extraction.CandidateContextBudgetError):
        asyncio.run(client.chat([ChatMessage(role="user", content="x" * 200)], max_tokens=2048))


def test_context_guard_reserves_output_for_unlimited_profile_call():
    from everalgo.llm.types import ChatMessage
    seen = {}
    class Client:
        async def chat(self, messages, **kwargs):
            seen.update(kwargs)
            return "ok"
    client = extraction._ContextGuardClient(Client(), token_counter=len, context_tokens=8192, model="qwen2.5:3b-memo-8k")
    assert asyncio.run(client.chat([ChatMessage(role="user", content="abc")])) == "ok"
    assert seen["max_tokens"] == 2048
    assert seen["model"] == "qwen2.5:3b-memo-8k"


def test_candidate_extraction_keeps_chat_under_user_and_returns_source_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def detect(messages, **kwargs):  # type: ignore[no-untyped-def]
        seen["messages"] = messages
        seen["boundary_kwargs"] = kwargs
        return SimpleNamespace(
            cells=[MemCell(items=messages, timestamp=messages[-1].timestamp)]
        )

    class EpisodeExtractor:
        def __init__(self, *, llm):  # type: ignore[no-untyped-def]
            pass

        async def aextract(
            self, cell, *, sender_id, prompt, custom_instructions
        ):  # type: ignore[no-untyped-def]
            seen["episode_sender_id"] = sender_id
            return SimpleNamespace(
                episode="用户要求简洁回答。", subject="回答风格", timestamp=1234
            )

    class ProfileExtractor:
        def __init__(self, *, llm):  # type: ignore[no-untyped-def]
            pass

        async def aextract(self, memcells, *, sender_id, prompt):  # type: ignore[no-untyped-def]
            seen["profile_messages"] = [item for cell in memcells for item in cell.items]
            seen["profile_sender_id"] = sender_id
            return SimpleNamespace(
                explicit_info=[{"category": "偏好", "description": "用户偏好简洁回答。", "evidence": "Please be concise."}],
                implicit_traits=[],
                timestamp=1234,
            )

    monkeypatch.setattr(extraction, "detect_boundaries", detect)
    monkeypatch.setattr(extraction, "EpisodeExtractor", EpisodeExtractor)
    monkeypatch.setattr(extraction, "ProfileExtractor", ProfileExtractor)

    candidates = asyncio.run(extraction.extract_chat_candidates(
        batch_id="batch-1",
        session_id="session-1",
        user_id="user-1",
        messages=[
            {
                "id": "message-user",
                "role": "user",
                "content": "Please be concise.",
                "timestamp_ms": 1000,
            },
            {
                "id": "message-assistant",
                "role": "assistant",
                "content": "Understood.",
                "timestamp_ms": 1234,
            },
        ],
        llm_client=object(),
        prompt_loader=SimpleNamespace(load=lambda name: f"prompt:{name}"),
        hard_token_limit=4096,
        hard_msg_limit=50,
    ))

    messages = seen["messages"]
    assert [message.role for message in messages] == ["user", "assistant"]
    assert {message.sender_id for message in messages} == {"user-1"}
    assert seen["episode_sender_id"] is None
    assert [item.role for item in seen["profile_messages"]] == ["user"]
    assert seen["profile_sender_id"] == "user-1"
    assert [(item["memory_type"], item["kind"], item["content"]) for item in candidates] == [
        ("episode", "episode", "用户要求简洁回答。"),
        ("profile", "profile", "用户偏好简洁回答。"),
    ]
    assert candidates[0]["source_message_ids"] == ["message-user", "message-assistant"]
    assert candidates[1]["source_message_ids"] == ["message-user"]
    assert candidates[1]["metadata"]["item"]["category"] == "偏好"


def test_candidate_extraction_retries_malformed_profile_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def detect(messages, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            cells=[MemCell(items=messages, timestamp=messages[-1].timestamp)]
        )

    class EpisodeExtractor:
        def __init__(self, *, llm):  # type: ignore[no-untyped-def]
            pass

        async def aextract(self, cell, *, sender_id, prompt, custom_instructions):  # type: ignore[no-untyped-def]
            return SimpleNamespace(episode="用户希望回答简洁。", subject="回答风格", timestamp=1234)

    class ProfileExtractor:
        def __init__(self, *, llm):  # type: ignore[no-untyped-def]
            pass

        async def aextract(self, memcells, *, sender_id, prompt):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise json.JSONDecodeError("Unterminated string", '{"explicit_info":', 17)
            return SimpleNamespace(explicit_info=[], implicit_traits=[], timestamp=1234)

    monkeypatch.setattr(extraction, "detect_boundaries", detect)
    monkeypatch.setattr(extraction, "EpisodeExtractor", EpisodeExtractor)
    monkeypatch.setattr(extraction, "ProfileExtractor", ProfileExtractor)

    candidates = asyncio.run(extraction.extract_chat_candidates(
        batch_id="batch-1",
        session_id="session-1",
        user_id="user-1",
        messages=[{
            "id": "message-user",
            "role": "user",
            "content": "Please be concise.",
            "timestamp_ms": 1000,
        }],
        llm_client=object(),
        prompt_loader=SimpleNamespace(load=lambda name: f"prompt:{name}"),
        hard_token_limit=4096,
        hard_msg_limit=50,
    ))

    assert attempts == 2
    assert [(candidate["memory_type"], candidate["content"]) for candidate in candidates] == [
        ("episode", "用户希望回答简洁。"),
    ]


def test_candidate_extraction_stops_after_repeated_invalid_atomic_fact_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class AtomicFactExtractor:
        async def aextract_from_text(self, text, *, timestamp):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            raise json.JSONDecodeError("Unterminated string", '{"atomic_facts":', 16)

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(extraction.asyncio, "sleep", no_wait)

    with pytest.raises(extraction.CandidateExtractionFormatError):
        asyncio.run(
            extraction._extract_atomic_facts_with_retry(
                AtomicFactExtractor(), "episode text", timestamp=1234
            )
        )

    assert attempts == extraction._ATOMIC_FACT_MAX_ATTEMPTS == 3


def test_truncated_episode_recovers_partial_content():
    from everalgo.llm import LLMError
    from openai import LengthFinishReasonError
    from openai.types.chat import ChatCompletion

    completion = ChatCompletion.model_validate({"id": "x", "created": 0, "model": "x", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": '{"title":"项目计划","content":"用户讨论了项目'}}]})
    class Truncated:
        async def chat(self, *args, **kwargs):
            raise LLMError("truncated") from LengthFinishReasonError(completion=completion)
    client = extraction._StructuredOutputClient(Truncated(), extraction._EpisodeOutput)
    response = asyncio.run(client.chat([]))
    assert json.loads(response.content) == {"title": "项目计划", "content": "用户讨论了项目"}
    assert client.output_truncated is True


def test_truncated_episode_without_title_does_not_invent_it():
    from everalgo.llm import LLMError
    from openai import LengthFinishReasonError
    from openai.types.chat import ChatCompletion

    completion = ChatCompletion.model_validate({"id": "x", "created": 0, "model": "x", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": '{"content":"用户讨论了项目'}}]})
    class Truncated:
        async def chat(self, *args, **kwargs):
            raise LLMError("truncated") from LengthFinishReasonError(completion=completion)
    client = extraction._StructuredOutputClient(Truncated(), extraction._EpisodeOutput)
    with pytest.raises(LLMError):
        asyncio.run(client.chat([]))


def test_recovered_episode_is_marked_for_review(monkeypatch):
    from everalgo.llm import LLMError
    from openai import LengthFinishReasonError
    from openai.types.chat import ChatCompletion

    completion = ChatCompletion.model_validate({
        "id": "x", "created": 0, "model": "x", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "length", "message": {
            "role": "assistant", "content": '{"title":"项目计划","content":"用户讨论了项目'
        }}],
    })
    class Truncated:
        async def chat(self, *args, **kwargs):
            raise LLMError("truncated") from LengthFinishReasonError(completion=completion)

    async def detect(messages, **kwargs):
        return SimpleNamespace(cells=[MemCell(items=messages, timestamp=1000)])

    class Profile:
        def __init__(self, **kwargs):
            pass
        async def aextract(self, *args, **kwargs):
            return SimpleNamespace(explicit_info=[], implicit_traits=[])

    monkeypatch.setattr(extraction, "detect_boundaries", detect)
    monkeypatch.setattr(extraction, "ProfileExtractor", Profile)
    result = asyncio.run(extraction.extract_chat_candidates(
        batch_id="batch", session_id="session", user_id="user",
        messages=[{"id": "m", "role": "user", "content": "用户讨论项目", "timestamp_ms": 1000}],
        llm_client=Truncated(), prompt_loader=SimpleNamespace(load=lambda name: "prompt"),
        hard_token_limit=4096, hard_msg_limit=50,
    ))
    assert result[0]["content"] == "用户讨论了项目"
    assert result[0]["metadata"]["output_truncated"] is True


def test_oversized_episode_is_split_without_losing_text(monkeypatch):
    async def detect(messages, **kwargs):
        return SimpleNamespace(cells=[MemCell(items=messages, timestamp=1000)])
    class Episode:
        def __init__(self, **kwargs):
            pass
        async def aextract(self, *args, **kwargs):
            return SimpleNamespace(episode="项目" * 2600, subject="项目", timestamp=1000)
    class Profile:
        def __init__(self, **kwargs):
            pass
        async def aextract(self, *args, **kwargs):
            return SimpleNamespace(explicit_info=[], implicit_traits=[])
    monkeypatch.setattr(extraction, "detect_boundaries", detect)
    monkeypatch.setattr(extraction, "EpisodeExtractor", Episode)
    monkeypatch.setattr(extraction, "ProfileExtractor", Profile)
    result = asyncio.run(extraction.extract_chat_candidates(
        batch_id="batch", session_id="session", user_id="user",
        messages=[{"id": "m", "role": "user", "content": "用户讨论项目", "timestamp_ms": 1000}],
        llm_client=object(), prompt_loader=SimpleNamespace(load=lambda name: "prompt"),
        hard_token_limit=4096, hard_msg_limit=50,
    ))
    assert len(result) == 2
    assert "".join(item["content"] for item in result) == "项目" * 2600
    assert all(len(item["content"]) <= 4000 for item in result)
    assert len({item["candidate_id"] for item in result}) == 2
    assert [item["metadata"]["part_index"] for item in result] == [1, 2]
    assert all(item["metadata"]["part_count"] == 2 for item in result)
    assert result[0]["source_segment"] == result[1]["source_segment"]
    assert all(item["metadata"]["output_truncated"] is False for item in result)


def test_split_episode_counts_emoji_as_browser_characters():
    text = "前" + "😀" * 2500 + "后"
    parts = extraction._split_episode_content(text)
    assert "".join(parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 4000 for part in parts)


def test_split_episode_preserves_paragraph_whitespace():
    text = ("内容" * 1500) + "。\n\n" + ("后文" * 1500)
    parts = extraction._split_episode_content(text)
    assert "".join(parts) == text
    assert parts[0].endswith("\n\n")


def test_non_chinese_episode_is_kept_with_review_warning(monkeypatch):
    async def detect(messages, **kwargs):
        return SimpleNamespace(cells=[MemCell(items=messages, timestamp=1000)])
    class Episode:
        def __init__(self, **kwargs): pass
        async def aextract(self, *args, **kwargs):
            return SimpleNamespace(episode="The user discussed project testing.", subject="Project testing", timestamp=1000)
    class Profile:
        def __init__(self, **kwargs): pass
        async def aextract(self, *args, **kwargs):
            return SimpleNamespace(explicit_info=[], implicit_traits=[])
    monkeypatch.setattr(extraction, "detect_boundaries", detect)
    monkeypatch.setattr(extraction, "EpisodeExtractor", Episode)
    monkeypatch.setattr(extraction, "ProfileExtractor", Profile)
    result = asyncio.run(extraction.extract_chat_candidates(
        batch_id="batch", session_id="session", user_id="user",
        messages=[{"id":"m", "role":"user", "content":"请讨论项目测试", "timestamp_ms":1000}],
        llm_client=object(), prompt_loader=SimpleNamespace(load=lambda name: "prompt"),
        hard_token_limit=4096, hard_msg_limit=50,
    ))
    assert result[0]["content"] == "The user discussed project testing."
    assert result[0]["metadata"]["output_language_not_chinese"] is True
    assert result[0]["source_message_ids"] == ["m"]


def test_episode_still_rejects_empty_or_copied_output():
    from everalgo.types import ChatMessage
    message = ChatMessage(id="m", role="assistant", sender_id="user", sender_name="assistant", content="The original assistant response.", timestamp=1000)
    cell = MemCell(items=[message], timestamp=1000)
    class Extractor:
        def __init__(self, title, content): self.title, self.content = title, content
        async def aextract(self, *args, **kwargs):
            return SimpleNamespace(episode=self.content, subject=self.title, timestamp=1000)
    with pytest.raises(ValueError, match="must not be empty"):
        asyncio.run(extraction._extract_episode_checked(Extractor("", "Some content"), cell))
    with pytest.raises(ValueError, match="rather than copy"):
        asyncio.run(extraction._extract_episode_checked(Extractor("Title", message.content), cell))


def test_non_chinese_profile_is_kept_with_source_and_review_warning(monkeypatch):
    async def detect(messages, **kwargs):
        return SimpleNamespace(cells=[MemCell(items=messages, timestamp=1000)])
    class Episode:
        def __init__(self, **kwargs): pass
        async def aextract(self, *args, **kwargs):
            return SimpleNamespace(episode="用户偏好简洁的回答。", subject="回答偏好", timestamp=1000)
    class Profile:
        def __init__(self, **kwargs): pass
        async def aextract(self, *args, **kwargs):
            return SimpleNamespace(explicit_info=[{"description": "identity-1234 prefers concise answers.", "evidence": "I prefer concise answers"}], implicit_traits=[], timestamp=1000)
    monkeypatch.setattr(extraction, "detect_boundaries", detect)
    monkeypatch.setattr(extraction, "EpisodeExtractor", Episode)
    monkeypatch.setattr(extraction, "ProfileExtractor", Profile)
    result = asyncio.run(extraction.extract_chat_candidates(
        batch_id="batch", session_id="session", user_id="identity-1234",
        messages=[{"id":"m", "role":"user", "content":"I prefer concise answers.", "timestamp_ms":1000}],
        llm_client=object(), prompt_loader=SimpleNamespace(load=lambda name: "prompt"),
        hard_token_limit=4096, hard_msg_limit=50,
    ))
    profile = next(item for item in result if item["memory_type"] == "profile")
    assert profile["content"] == "用户 prefers concise answers."
    assert profile["metadata"]["output_language_not_chinese"] is True
    assert profile["source_message_ids"] == ["m"]
    assert profile["source_segment"]["items"][0]["message_id"] == "m"
