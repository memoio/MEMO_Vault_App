"""Deterministic UMIF serialization, content hashes and source IDs."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

_NAMESPACE = uuid.UUID("6cb37c40-f139-4a87-8c62-998331e72e87")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_hash(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()


def record_id(
    provider: str, native_type: str, native_id: str, branch_id: str = "main"
) -> str:
    return (
        "rec_"
        + uuid.uuid5(
            _NAMESPACE, "\x1f".join((provider, native_type, native_id, branch_id))
        ).hex
    )


def derived_record_id(
    provider: str, session_id: str, sequence: int, payload: Any
) -> str:
    return (
        "rec_"
        + hashlib.sha256(
            canonical_bytes([provider, session_id, sequence, payload])
        ).hexdigest()
    )
