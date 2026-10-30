"""Public request and response contracts for the local Vault API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ApiEnvelope[T](BaseModel):
    request_id: str
    data: T


class HealthData(BaseModel):
    status: Literal["ok"] = "ok"
    everos_base_url: str
    everos_status: Literal["online", "offline"] = "offline"
    everos_checked_at: str | None = None
    data_dir: str
    app_id: str
    user_id: str
    agent_id: str
    max_import_bytes: int
    max_archive_unpacked_bytes: int
    max_archive_files: int
    max_archive_compression_ratio: int
    features: dict[str, str]


class ArchiveRequest(BaseModel):
    namespace: str = Field(min_length=1, max_length=96)


class ImportCommitRequest(BaseModel):
    preview_id: str = Field(min_length=32, max_length=32)
    project_id: str = Field(min_length=1, max_length=64)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_project_id: str | None = Field(default="personal", min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)


class ConfirmCandidateRequest(BaseModel):
    revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    kind: Literal["preference", "fact", "decision", "todo", "episode", "profile"]
    namespace: str = Field(min_length=1, max_length=96)
    external_use: Literal["allow", "ask", "deny"] = "ask"
    validity_days: Literal[30, 90] | None = None
    evidence_kind: Literal["human_verified", "human_authored"]


class ManualCandidateRequest(BaseModel):
    kind: Literal["episode", "preference"]
    namespace: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=4000)
    external_use: Literal["allow", "ask", "deny"] = "ask"
    validity_days: Literal[30, 90] | None = None


class RejectCandidateRequest(BaseModel):
    revision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class CandidateReviewItem(BaseModel):
    id: str
    batch_id: str
    filename: str
    project_id: str
    namespace: str
    kind: Literal["preference", "fact", "decision", "todo", "episode", "profile"]
    memory_type: Literal["atomic_fact", "episode", "profile"]
    content: str
    status: Literal["pending", "publishing", "confirmed", "rejected"]
    revision: int
    evidence_kind: str | None = None
    external_use: str
    origin: Literal["import", "agent", "manual"]
    evidence_status: Literal["valid", "missing", "mismatch", "not_required"]
    validity_days: Literal[30, 90] | None = None
    valid_until: str | None = None
    created_at: str
    evidence: list[dict[str, object]]


class CandidateListData(BaseModel):
    candidates: list[CandidateReviewItem]
    total: int


class LibraryMemoryItem(BaseModel):
    id: str
    everos_memory_id: str
    project_id: str
    namespace: str
    kind: str
    memory_type: Literal["atomic_fact", "episode", "profile"]
    content_preview: str
    timestamp: str
    external_use: Literal["allow", "ask", "deny"]
    status: Literal["valid", "expired"]
    lifecycle_status: Literal["active", "withdrawn", "deleted"] = "active"
    revision: int
    evidence_kind: str
    valid_until: str | None


class LibraryEvidenceItem(BaseModel):
    filename: str
    message_id: str
    role: str
    timestamp_ms: int | None
    quote: str
    available: bool
    excerpt_matched: bool = False
    archive_status: Literal["available", "missing", "hash_mismatch", "hash_unavailable"] = "missing"


class LibraryMemoryDetail(LibraryMemoryItem):
    content: str = ""
    metadata: dict[str, object]
    evidence: list[LibraryEvidenceItem]
    evidence_available: bool
    source_segment: dict[str, object] | None = None
    lifecycle_note: str | None = None


class MemoryLibraryListData(BaseModel):
    memories: list[LibraryMemoryItem]
    visible_total: int
    missing_index_ids: list[str]


class MemoryLibraryProjectSummary(BaseModel):
    project_id: str
    name: str
    visible_valid_count: int
    syncing_count: int


class MemoryLibrarySummaryData(BaseModel):
    projects: list[MemoryLibraryProjectSummary]


MemoryKind = Literal["fact", "episode", "profile"]
ExternalUse = Literal["allow", "ask", "deny"]
PackFormat = Literal["markdown", "vault-memory/1"]
EvidenceMode = Literal["none", "id", "snippet", "full"]


class PackPreviewRequest(BaseModel):
    namespaces: list[str] = Field(min_length=1, max_length=32)
    kinds: list[MemoryKind] = Field(
        default_factory=lambda: ["fact", "episode", "profile"],
        min_length=1,
    )
    format: Literal["markdown", "vault-memory/1"] = "markdown"
    token_budget: int = Field(default=4000, ge=100, le=32000)
    evidence_mode: EvidenceMode = "id"
    memory_ids: list[str] | None = Field(default=None, max_length=1000)


class PackExportRequest(PackPreviewRequest):
    memory_ids: list[str] = Field(min_length=0, max_length=1000)
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_generated_at: str = Field(min_length=1, max_length=64)
    confirmed_ask_ids: list[str] = Field(default_factory=list, max_length=1000)
    confirm_evidence: bool = False
    confirm_unrecoverable_transfer: bool = False
    delivery_action: Literal["copy", "download"]


class PackPreviewItem(BaseModel):
    memory_id: str
    revision: int
    kind: MemoryKind
    namespace: str
    external_use: ExternalUse
    lifecycle_status: Literal["active", "withdrawn", "deleted"]
    validity_status: Literal["valid", "expired"]
    display_content: str | None
    eligible: bool
    selected: bool
    included: bool
    exclusion_reason: str | None


class PackPreviewData(BaseModel):
    rendered_content: str
    generated_at: str
    preview_sha256: str
    content_sha256: str
    items: list[PackPreviewItem]
    selected_ask_ids: list[str]
    exclusion_counts: dict[str, int]
    included_count: int
    estimated_tokens: int
    token_budget: int
    estimate_is_approximate: bool = True


class PackExportData(BaseModel):
    rendered_content: str
    generated_at: str
    preview_sha256: str
    content_sha256: str
    format: PackFormat
    included_ids: list[str]
    included_revisions: dict[str, int]
    included_count: int
    estimated_tokens: int


class AdpValidateRequest(BaseModel):
    trace_ids: list[str] = Field(min_length=1, max_length=100)
    format: Literal["adp", "atif"] = "adp"


class AdpConsentRequest(AdpValidateRequest):
    consent: bool


class AdpExportRequest(AdpValidateRequest):
    preview_sha256: str = Field(min_length=64, max_length=64)
