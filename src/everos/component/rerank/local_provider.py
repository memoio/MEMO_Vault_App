"""In-process CrossEncoder reranking using an explicitly downloaded model.

Heavy dependencies and weights are loaded on first use in a worker thread.
The process-wide capability accessor owns the provider, so weights are reused
until the EverOS process exits. No model downloads or remote code are allowed.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Sequence
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from .protocol import RerankResult, RerankServiceError


class LocalRerankProvider:
    """Score query/document pairs locally, returning every input index.

    Timeout covers queueing, model loading and inference. Python cannot kill
    native inference on timeout: the worker retains its concurrency slot until
    it finishes. Subsequent calls reuse the loaded model. BGE base ignores the
    protocol's optional instruction because it is not instruction tuned.
    """

    def __init__(
        self,
        *,
        model: str,
        device: str = "cpu",
        max_length: int = 512,
        batch_size: int = 4,
        max_concurrent: int = 1,
        timeout: float = 120.0,
    ) -> None:
        model_path = Path(model).expanduser().resolve()
        if not model_path.is_dir() or not (model_path / "config.json").is_file():
            raise ValueError(
                f"Local rerank model directory is missing or incomplete: {model_path}. "
                "Download BAAI/bge-reranker-base including config and tokenizer files."
            )
        if not (model_path / "model.safetensors").is_file():
            raise ValueError(f"Local rerank safetensors weights missing: {model_path}")
        if not any(
            (model_path / name).is_file()
            for name in ("tokenizer.json", "sentencepiece.bpe.model", "vocab.txt")
        ):
            raise ValueError(f"Local rerank tokenizer missing: {model_path}")
        if find_spec("sentence_transformers") is None or find_spec("torch") is None:
            raise ValueError(
                "Local rerank dependencies missing. Install everos[local-rerank] "
                "in the Python environment running EverOS."
            )
        if batch_size < 1 or max_concurrent < 1 or max_length < 1 or timeout <= 0:
            raise ValueError("Local rerank limits must be positive.")
        self._model_path = str(model_path)
        self._device = device
        self._max_length = max_length
        self._batch_size = batch_size
        self._timeout = timeout
        self._model: Any = None
        self._load_lock = threading.Lock()
        # A thread semaphore keeps timed-out/cancelled native work accounted for.
        self._slots = threading.BoundedSemaphore(max_concurrent)

    def _load_model(self) -> Any:
        with self._load_lock:
            if self._model is None:
                from sentence_transformers import CrossEncoder
                from torch import nn

                self._model = CrossEncoder(
                    self._model_path,
                    device=self._device,
                    max_length=self._max_length,
                    local_files_only=True,
                    trust_remote_code=False,
                    activation_fn=nn.Sigmoid(),
                    model_kwargs={"use_safetensors": True},
                )
            return self._model

    def _predict(
        self,
        query: str,
        documents: list[str],
        deadline: float,
        cancelled: threading.Event,
    ) -> list[RerankResult]:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._slots.acquire(timeout=max(0.0, remaining)):
            raise TimeoutError("Local rerank queue timed out.")
        try:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("Local rerank request expired before inference.")
            model = self._load_model()
            results: list[RerankResult] = []
            for start in range(0, len(documents), self._batch_size):
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("Local rerank request timed out.")
                batch = documents[start : start + self._batch_size]
                scores = model.predict(
                    [(query, document) for document in batch],
                    batch_size=self._batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                )
                if len(scores) != len(batch):
                    raise ValueError(
                        "Local rerank model returned an invalid score count."
                    )
                for offset, score in enumerate(scores):
                    value = float(score)
                    if not math.isfinite(value):
                        raise ValueError(
                            "Local rerank model returned a non-finite score."
                        )
                    results.append(RerankResult(index=start + offset, score=value))
            return sorted(results, key=lambda result: result.score, reverse=True)
        finally:
            self._slots.release()

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        instruction: str | None = None,
    ) -> list[RerankResult]:
        """Run bounded local inference without blocking the API event loop."""
        if not documents:
            return []
        deadline = time.monotonic() + self._timeout
        cancelled = threading.Event()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._predict, query, list(documents), deadline, cancelled
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise RerankServiceError("Local rerank timed out.") from exc
        except Exception as exc:
            # Never include query/document text in client-facing diagnostics.
            raise RerankServiceError(
                "Local rerank failed to load or run. Check model files, device "
                "and local-rerank dependencies in the EverOS Python environment."
            ) from exc
        finally:
            cancelled.set()
