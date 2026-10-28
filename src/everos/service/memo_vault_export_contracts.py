"""Neutral contracts for delegated Vault access; independent of HTTP and MCP."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KINDS = ("fact", "episode", "profile")
LIVE_OPERATIONS = ("search_memory", "get_memory", "get_evidence", "build_context")
PACK_OPERATIONS = ("get_pack_info", "read_memory_pack")


class GatewayError(RuntimeError):
    """Sanitized error safe to send across the external boundary."""

    def __init__(self, code: str = "ACCESS_DENIED", status_code: int = 403) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__("access denied" if code == "ACCESS_DENIED" else code)


class GrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=500)
    mode: Literal["live_scope", "approved_pack"] = "live_scope"
    project_ids: list[str] = Field(min_length=1, max_length=32)
    kinds: list[Literal["fact", "episode", "profile"]] = Field(
        default_factory=lambda: list(KINDS), min_length=1
    )
    operations: list[
        Literal[
            "search_memory",
            "get_memory",
            "get_evidence",
            "build_context",
            "get_pack_info",
            "read_memory_pack",
        ]
    ] = Field(
        default_factory=lambda: ["search_memory", "get_memory", "build_context"],
        min_length=1,
    )
    expires_in_hours: int = Field(default=24, ge=1, le=720)
    token_budget: int = Field(default=4000, ge=100, le=32000)
    evidence_mode: Literal["none", "id", "snippet"] = "none"
    requests_per_minute: int = Field(default=30, ge=1, le=120)
    confirm_transfer: bool = False
    confirm_future_allow: bool = False

    @model_validator(mode="after")
    def normalize(self) -> GrantCreate:
        self.project_ids = sorted(set(self.project_ids))
        if any(not p.strip() or len(p) > 64 for p in self.project_ids):
            raise ValueError("Invalid project scope")
        self.kinds = list(dict.fromkeys(self.kinds))
        if self.mode == "approved_pack":
            self.operations = list(PACK_OPERATIONS)
        elif any(op not in LIVE_OPERATIONS for op in self.operations):
            raise ValueError("Operation does not match live mode")
        if "get_evidence" in self.operations and self.evidence_mode != "snippet":
            raise ValueError("Evidence excerpts require explicit snippet permission")
        return self


class EvidenceReference(BaseModel):
    evidence_id: str
    source_message_id: str
    quote: str
    quote_sha256: str
    available: bool
    matched: bool


class SourceMemory(BaseModel):
    """Canonical content joined to a confirmed Vault governance row."""

    memory_id: str
    everos_memory_id: str
    project_id: str
    namespace: str
    kind: str
    memory_type: str
    revision: int
    content: str | None
    timestamp: str | None
    recorded_at: str
    confirmed_at: str
    external_use: str
    valid_until: str | None
    expiry_status: str
    lifecycle_status: str
    evidence_kind: str
    content_sha256: str | None
    evidence: tuple[EvidenceReference, ...] = ()
    index_available: bool

    def fingerprint(self) -> str:
        state = self.model_dump(
            mode="json", exclude={"content", "evidence", "content_sha256"}
        )
        state["body_hash"] = hashlib.sha256((self.content or "").encode()).hexdigest()
        state["evidence"] = [
            e.model_dump(exclude={"quote"})
            | {"actual_quote_hash": hashlib.sha256(e.quote.encode()).hexdigest()}
            for e in self.evidence
        ]
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
