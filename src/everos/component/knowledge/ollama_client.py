"""Native Ollama access with a live runtime window and bounded JSON output."""

from __future__ import annotations

import json
from typing import Any

import httpx

from everos.core.errors import ExternalServiceError


class IncompleteExtractionError(ExternalServiceError):
    """No document may be persisted after an incomplete extraction."""


def json_text(value: Any) -> str:
    """Compact JSON, retaining Unicode for conservative byte budgeting."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class OllamaKnowledgeClient:
    """Resolve actual context; never substitute a theoretical/fixed window."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str, model: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _matches(self, name: str) -> bool:
        def normalize(value: str) -> str:
            return value if ":" in value.rsplit("/", 1)[-1] else value + ":latest"

        return normalize(name) == normalize(self.model)

    @staticmethod
    def _json_response(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise IncompleteExtractionError(
                "Ollama returned invalid HTTP JSON."
            ) from exc
        if not isinstance(body, dict):
            raise IncompleteExtractionError("Ollama HTTP JSON must be an object.")
        return body

    async def _running_window(self) -> int | None:
        response = await self.http.get(f"{self.base_url}/api/ps")
        response.raise_for_status()
        entries = self._json_response(response).get("models", [])
        if not isinstance(entries, list) or any(
            not isinstance(e, dict) for e in entries
        ):
            raise IncompleteExtractionError(
                "Ollama returned invalid runtime model metadata."
            )
        for entry in entries:
            if self._matches(str(entry.get("name") or entry.get("model") or "")):
                window = entry.get("context_length")
                if type(window) is not int or window <= 0:
                    raise IncompleteExtractionError(
                        "Ollama returned an invalid runtime context length."
                    )
                return window
        return None

    async def context_window(self) -> int:
        """Re-read on every extraction; warm a cold model using server defaults."""
        window = await self._running_window()
        if window is None:
            response = await self.http.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": "", "stream": False},
            )
            response.raise_for_status()
            window = await self._running_window()
        if window is None:
            raise IncompleteExtractionError(
                "Ollama runtime context is unavailable after model loading."
            )
        if self.input_budget(window) <= 0:
            raise IncompleteExtractionError(
                "Ollama runtime context is too small for Knowledge extraction."
            )
        return window

    @staticmethod
    def output_budget(window: int) -> int:
        return window // 4

    @classmethod
    def input_budget(cls, window: int) -> int:
        # UTF-8 bytes upper-bound byte-tokenizer content tokens. Reserve chat
        # framing too, rather than assuming o200k matches the selected model.
        return window - cls.output_budget(window) - max(128, window // 16)

    @classmethod
    def fits(cls, messages: list[dict[str, str]], window: int) -> bool:
        return sum(
            len(m["content"].encode("utf-8")) for m in messages
        ) <= cls.input_budget(window)

    async def complete(
        self, messages: list[dict[str, str]], schema: dict[str, Any], window: int
    ) -> dict[str, Any]:
        if not self.fits(messages, window):
            raise IncompleteExtractionError(
                "Knowledge prompt exceeds the actual Ollama input budget."
            )
        response = await self.http.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": schema,
                "options": {
                    "num_ctx": window,
                    "num_predict": self.output_budget(window),
                    "temperature": 0,
                },
            },
        )
        response.raise_for_status()
        body = self._json_response(response)
        if body.get("done_reason") != "stop":
            raise IncompleteExtractionError(
                "Knowledge model output is truncated or unfinished."
            )
        if body.get("prompt_eval_count", 0) + body.get("eval_count", 0) > window:
            raise IncompleteExtractionError(
                "Knowledge generation exceeded its pinned context window."
            )
        try:
            parsed = json.loads(body["message"]["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IncompleteExtractionError(
                "Knowledge model did not return complete JSON."
            ) from exc
        if not isinstance(parsed, dict):
            raise IncompleteExtractionError("Knowledge model JSON must be an object.")
        return parsed
