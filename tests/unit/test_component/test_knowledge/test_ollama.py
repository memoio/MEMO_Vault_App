"""Dynamic runtime window and lossless bounded Knowledge extraction."""

from __future__ import annotations

import json

import httpx
import pytest
from everalgo.types import ParsedContent

from everos.component.knowledge.ollama_client import (
    IncompleteExtractionError,
    OllamaKnowledgeClient,
)
from everos.component.knowledge.ollama_extractor import OllamaKnowledgeExtractor


@pytest.mark.asyncio
async def test_window_follows_runtime_and_alias():
    window = 4096

    def handle(request):
        return httpx.Response(
            200, json={"models": [{"name": "qwen:latest", "context_length": window}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OllamaKnowledgeClient(http, base_url="http://ollama", model="qwen")
        assert await client.context_window() == 4096
        window = 8192
        assert await client.context_window() == 8192


@pytest.mark.asyncio
async def test_cold_model_loads_without_overriding_context():
    loaded = False
    requests = []

    def handle(request):
        nonlocal loaded
        requests.append(request)
        if request.url.path == "/api/generate":
            assert "options" not in json.loads(request.content)
            loaded = True
            return httpx.Response(200, json={"done": True})
        return httpx.Response(
            200,
            json={
                "models": [{"name": "qwen:latest", "context_length": 12288}]
                if loaded
                else []
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OllamaKnowledgeClient(http, base_url="http://ollama", model="qwen")
        assert await client.context_window() == 12288
        assert [r.url.path for r in requests] == ["/api/ps", "/api/generate", "/api/ps"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, -1, True, "4096"])
async def test_no_fixed_window_fallback(value):
    def handle(request):
        return httpx.Response(
            200, json={"models": [{"name": "qwen", "context_length": value}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OllamaKnowledgeClient(http, base_url="http://ollama", model="qwen")
        with pytest.raises(IncompleteExtractionError, match="context"):
            await client.context_window()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,text", [("length", "{}"), ("stop", "not-json")])
async def test_invalid_or_truncated_json_rejected(reason, text):
    def handle(request):
        body = json.loads(request.content)
        assert body["options"]["num_ctx"] == 8192
        assert 0 < body["options"]["num_predict"] < 8192
        return httpx.Response(
            200, json={"message": {"content": text}, "done_reason": reason}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OllamaKnowledgeClient(http, base_url="http://ollama", model="qwen")
        with pytest.raises(IncompleteExtractionError):
            await client.complete([{"role": "user", "content": "test"}], {}, 8192)


def mock_transport(mode="complete", window=2048):
    calls = []

    def handle(request):
        if request.url.path == "/api/ps":
            return httpx.Response(
                200, json={"models": [{"name": "qwen", "context_length": window}]}
            )
        body = json.loads(request.content)
        calls.append(body)
        data = json.loads(body["messages"][-1]["content"])
        if "topics" in data:
            result = {
                "assignments": [
                    {"block_id": b["id"], "topic_index": 0} for b in data["blocks"]
                ]
            }
        else:
            ids = [b["id"] for b in data["blocks"]]
            if mode == "missing":
                ids = ids[:1]
            if mode == "invalid":
                ids = [999999]
            result = {
                "topics": [{"title": "Topic", "summary": "Summary", "block_ids": ids}]
            }
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps(result)}, "done_reason": "stop"},
        )

    return httpx.MockTransport(handle), calls


@pytest.mark.asyncio
async def test_batches_and_preserves_all_original_text():
    transport, calls = mock_transport()
    text = "\n\n".join("原始正文" + str(i) + "。" * 150 for i in range(10))
    extractor = OllamaKnowledgeExtractor(
        base_url="http://ollama", model="qwen", transport=transport
    )
    result = await extractor.aextract(
        ParsedContent(text=text), doc_id="doc", title="Title"
    )
    assert len(calls) > 1
    all_content = "".join(m.content for m in result[1:])
    assert all(textpart in all_content for textpart in text.split("\n\n"))
    assert sorted(i for m in result[1:] for i in m.block_refs) == list(range(10))
    assert len(set(i for m in result[1:] for i in m.block_refs)) == 10
    for call in calls:
        assert call["options"]["num_ctx"] == 2048
        assert sum(
            len(m["content"].encode()) for m in call["messages"]
        ) <= OllamaKnowledgeClient.input_budget(2048)


@pytest.mark.asyncio
async def test_uncovered_blocks_are_repaired_in_bounded_requests():
    transport, calls = mock_transport(mode="missing", window=4096)
    result = await OllamaKnowledgeExtractor(
        base_url="http://ollama", model="qwen", transport=transport
    ).aextract(
        ParsedContent(text="First paragraph.\n\nSecond paragraph.\n\nThird paragraph."),
        doc_id="doc",
        title="Title",
    )
    assert sorted(i for m in result[1:] for i in m.block_refs) == [0, 1, 2]
    assert any("topics" in json.loads(c["messages"][-1]["content"]) for c in calls)


@pytest.mark.asyncio
async def test_unknown_ids_never_count_as_complete():
    transport, _calls = mock_transport(mode="invalid")
    extractor = OllamaKnowledgeExtractor(
        base_url="http://ollama", model="qwen", transport=transport
    )
    with pytest.raises(IncompleteExtractionError):
        await extractor.aextract(
            ParsedContent(text="Only paragraph."), doc_id="doc", title="Title"
        )


@pytest.mark.asyncio
async def test_oversized_single_paragraph_split_losslessly():
    transport, _calls = mock_transport()
    text = "你好世界。" * 400
    result = await OllamaKnowledgeExtractor(
        base_url="http://ollama", model="qwen", transport=transport
    ).aextract(ParsedContent(text=text), doc_id="doc", title="Title")
    assert "".join(m.content for m in result[1:]) == text


@pytest.mark.asyncio
async def test_preserves_indentation_blank_lines_and_trailing_space():
    transport, _calls = mock_transport()
    text = (
        "\n\ndef example():\n    if True:\n        return 1  \n\n- outer\n  - inner\n\n"
    )
    memories = await OllamaKnowledgeExtractor(
        base_url="http://ollama", model="qwen", transport=transport
    ).aextract(ParsedContent(text=text), doc_id="doc", title="Title")
    assert "".join(m.content for m in memories[1:]) == text


@pytest.mark.asyncio
async def test_single_failed_block_is_split_and_coverage_revalidated():
    def handle(request):
        if request.url.path == "/api/ps":
            return httpx.Response(
                200, json={"models": [{"name": "qwen", "context_length": 4096}]}
            )
        body = json.loads(request.content)
        data = json.loads(body["messages"][-1]["content"])
        if len(data["blocks"][0]["text"]) > 80:
            return httpx.Response(
                200, json={"done_reason": "length", "message": {"content": "{"}}
            )
        result = {
            "topics": [
                {
                    "title": "Topic",
                    "summary": "Summary",
                    "block_ids": [b["id"] for b in data["blocks"]],
                }
            ]
        }
        return httpx.Response(
            200,
            json={"done_reason": "stop", "message": {"content": json.dumps(result)}},
        )

    text = "source paragraph " * 20
    extractor = OllamaKnowledgeExtractor(
        base_url="http://ollama", model="qwen", transport=httpx.MockTransport(handle)
    )
    memories = await extractor.aextract(
        ParsedContent(text=text), doc_id="doc", title="Title"
    )
    assert "".join(m.content for m in memories[1:]) == text
    assert extractor.last_report["covered_chars"] == len(text)
    refs = [i for m in memories[1:] for i in m.block_refs]
    assert len(refs) == len(set(refs))


@pytest.mark.asyncio
async def test_same_extractor_refreshes_window_for_next_document():
    base, calls = mock_transport()
    window = 2048

    async def handle(request):
        if request.url.path == "/api/ps":
            return httpx.Response(
                200, json={"models": [{"name": "qwen", "context_length": window}]}
            )
        return await base.handle_async_request(request)

    extractor = OllamaKnowledgeExtractor(
        base_url="http://ollama", model="qwen", transport=httpx.MockTransport(handle)
    )
    await extractor.aextract(
        ParsedContent(text="Source one."), doc_id="a", title="Title"
    )
    assert calls[-1]["options"]["num_ctx"] == 2048
    window = 8192
    await extractor.aextract(
        ParsedContent(text="Source two."), doc_id="b", title="Title"
    )
    assert calls[-1]["options"]["num_ctx"] == 8192
    assert extractor.last_report["context_window"] == 8192


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["context", "completion"])
async def test_invalid_http_json_is_an_explicit_extraction_error(endpoint):
    def handle(request):
        return httpx.Response(200, text="invalid JSON")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OllamaKnowledgeClient(http, base_url="http://ollama", model="qwen")
        with pytest.raises(IncompleteExtractionError):
            if endpoint == "context":
                await client.context_window()
            else:
                await client.complete([{"role": "user", "content": "test"}], {}, 4096)
