"""Local rerank configuration, result contract and cancellation coverage."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import Mock

import pytest

from everos.component.rerank.factory import build_rerank_provider
from everos.component.rerank.local_provider import LocalRerankProvider
from everos.component.rerank.protocol import RerankServiceError
from everos.config import RerankSettings


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    for name in ("config.json", "model.safetensors", "tokenizer.json"):
        (tmp_path / name).write_text("{}")
    monkeypatch.setattr(
        "everos.component.rerank.local_provider.find_spec", lambda _: object()
    )
    return tmp_path


def test_factory_local_without_http_credentials(model_dir):
    settings = RerankSettings(provider="local", model=str(model_dir), base_url="")
    assert isinstance(build_rerank_provider(settings), LocalRerankProvider)


def test_incomplete_model_rejected(tmp_path):
    with pytest.raises(ValueError, match="missing or incomplete"):
        LocalRerankProvider(model=str(tmp_path))


def test_missing_dependency_rejected(model_dir, monkeypatch):
    monkeypatch.setattr(
        "everos.component.rerank.local_provider.find_spec", lambda _: None
    )
    with pytest.raises(ValueError, match="dependencies missing"):
        LocalRerankProvider(model=str(model_dir))


@pytest.mark.asyncio
async def test_sorted_complete_results_and_model_reuse(model_dir):
    provider = LocalRerankProvider(model=str(model_dir), batch_size=2)
    model = Mock()
    model.predict.side_effect = [[0.1, 0.9], [0.5], [0.2]]
    # Bypass heavy imports; the real HTTP test separately exercises real weights.
    provider._model = model
    first = await provider.rerank("query", ["a", "b", "c"])
    second = await provider.rerank("query", ["a"])
    assert [r.index for r in first] == [1, 2, 0]
    assert [r.score for r in first] == [0.9, 0.5, 0.1]
    assert len(second) == 1
    assert provider._model is model
    assert model.predict.call_count == 3


@pytest.mark.asyncio
async def test_empty_documents_do_not_load(model_dir):
    provider = LocalRerankProvider(model=str(model_dir))
    provider._load_model = Mock(side_effect=AssertionError("must not load"))
    assert await provider.rerank("query", []) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scores", [[float("nan")], []])
async def test_invalid_scores_fail_explicitly(model_dir, scores):
    provider = LocalRerankProvider(model=str(model_dir))
    model = Mock()
    model.predict.return_value = scores
    provider._model = model
    with pytest.raises(RerankServiceError, match="failed to load or run"):
        await provider.rerank("query", ["a"])


@pytest.mark.asyncio
async def test_cancelled_queued_worker_never_predicts(model_dir):
    provider = LocalRerankProvider(model=str(model_dir), timeout=2)
    model = Mock()
    model.predict.return_value = [0.5]
    provider._model = model
    acquired = threading.Event()
    original_acquire = provider._slots.acquire
    original_acquire()

    def observe_acquire(*args, **kwargs):
        acquired.set()
        return original_acquire(*args, **kwargs)

    provider._slots.acquire = observe_acquire
    task = asyncio.create_task(provider.rerank("query", ["a"]))
    try:
        assert await asyncio.to_thread(acquired.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        provider._slots.release()
    # The worker must drain its old queue slot before this valid call can run.
    result = await provider.rerank("valid", ["b"])
    assert len(result) == 1
    model.predict.assert_called_once()
    assert model.predict.call_args.args[0] == [("valid", "b")]


@pytest.mark.asyncio
async def test_timeout_keeps_running_native_work_bounded(model_dir):
    provider = LocalRerankProvider(model=str(model_dir), timeout=0.1)
    entered, release = threading.Event(), threading.Event()
    model = Mock()

    def predict(*args, **kwargs):
        entered.set()
        release.wait(2)
        return [0.7]

    model.predict.side_effect = predict
    provider._model = model
    task = asyncio.create_task(provider.rerank("query", ["a"]))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(RerankServiceError, match="timed out"):
            await task
        # Native work is still running, so it must still hold the slot.
        assert not provider._slots.acquire(blocking=False)
    finally:
        release.set()
