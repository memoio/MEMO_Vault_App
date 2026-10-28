"""Structured model output truncation must be retried without losing the batch."""

import asyncio
from types import SimpleNamespace

import openai
import pytest
from everalgo.llm import LLMError

from everos.service import memo_vault_candidates as extraction


def truncated_error():
    cause = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
    error = LLMError("truncated")
    error.__cause__ = cause
    return error


def test_retries_truncated_structured_output(monkeypatch):
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise truncated_error()
        return "valid"

    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(extraction.asyncio, "sleep", no_wait)
    assert (
        asyncio.run(extraction._retry_structured_output("episode", operation))
        == "valid"
    )
    assert calls == 2


def test_exhausted_truncation_reports_stage_without_returning_partial_json(monkeypatch):
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise truncated_error()

    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(extraction.asyncio, "sleep", no_wait)
    with pytest.raises(extraction.CandidateExtractionFormatError) as error:
        asyncio.run(extraction._retry_structured_output("episode", operation))
    assert error.value.stage == "episode"
    assert error.value.reason == "output_truncated"
    assert calls == 3


def test_other_model_errors_still_propagate():
    async def operation():
        raise LLMError("connection failed")

    with pytest.raises(LLMError):
        asyncio.run(extraction._retry_structured_output("episode", operation))
