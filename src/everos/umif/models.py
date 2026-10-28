"""The versioned, provider-independent first UMIF record slice."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0.0"


class UmifRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    record_type: str
    record_id: str
    source: dict[str, Any]
    scope: dict[str, Any]
    occurred_at: str | None = None
    timestamp_ms: int | None = None
    sequence: int
    state: Literal["active", "deleted", "redacted", "superseded", "tombstone"] = (
        "active"
    )
    provenance: dict[str, Any]
    routing_hints: dict[str, Any] = Field(default_factory=dict)
    privacy: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any]
    extensions: dict[str, Any] = Field(default_factory=dict)


class Principal(UmifRecord):
    record_type: Literal["principal"] = "principal"


class Session(UmifRecord):
    record_type: Literal["session"] = "session"


class Message(UmifRecord):
    record_type: Literal["event.message"] = "event.message"


class ToolCall(UmifRecord):
    record_type: Literal["event.tool_call"] = "event.tool_call"


class ToolResult(UmifRecord):
    record_type: Literal["event.tool_result"] = "event.tool_result"


Event = Message | ToolCall | ToolResult


class Asset(UmifRecord):
    record_type: Literal["asset"] = "asset"


class Document(UmifRecord):
    record_type: Literal["document"] = "document"


class MemoryAssertion(UmifRecord):
    record_type: Literal["memory.assertion"] = "memory.assertion"


class FileEntry(BaseModel):
    path: str
    sha256: str
    record_count: int


class Manifest(BaseModel):
    format: Literal["umif"] = "umif"
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    package_id: str
    created_at: str | None = None
    generator: dict[str, str]
    source_exports: list[dict[str, Any]]
    default_scope: dict[str, Any]
    files: list[FileEntry]
    privacy: dict[str, Any] = Field(default_factory=dict)


class UmifPackage(BaseModel):
    manifest: Manifest
    principals: tuple[Principal, ...]
    sessions: tuple[Session, ...]
    events: tuple[Event, ...]
    optional_records: dict[str, tuple[UmifRecord, ...]] = Field(default_factory=dict)
    import_report: dict[str, Any] = Field(default_factory=dict)
    blobs: dict[str, bytes] = Field(default_factory=dict, exclude=True)
