"""Strict transport DTOs for owner grant management and external operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from everos.service.memo_vault_export_contracts import GrantCreate


class CreateGrantRequest(GrantCreate):
    draft_id: str | None = Field(default=None, min_length=32, max_length=32)


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_ids: list[str] = Field(min_length=1, max_length=32)
    kinds: list[Literal["fact", "episode", "profile"]] | None = Field(
        default=None, min_length=1, max_length=3
    )


class SearchRequest(ReadRequest):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2048)


class GetRequest(ReadRequest):
    memory_id: str = Field(min_length=1, max_length=256)


class EvidenceRequest(GetRequest):
    evidence_id: str = Field(min_length=1, max_length=256)


class ContextRequest(ReadRequest):
    query: str = Field(min_length=1, max_length=500)
    token_budget: int | None = Field(default=None, ge=100, le=32000)
    format: Literal["vault-memory/1", "markdown"] = "vault-memory/1"
    evidence_mode: Literal["none", "id"] = "none"
