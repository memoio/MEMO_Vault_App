import { ownerFetch } from "./ownerSession";
export type FeatureStatus = "not_started" | "ready";

export interface VaultHealth {
  status: "ok";
  everos_base_url: string;
  everos_status: "online" | "offline";
  everos_checked_at: string | null;
  data_dir: string;
  app_id: string;
  user_id: string;
  agent_id: string;
  max_import_bytes: number;
  max_archive_unpacked_bytes: number;
  max_archive_files: number;
  max_archive_compression_ratio: number;
  features: Record<string, FeatureStatus>;
}

export interface PreviewMessage {
  conversation_id: string;
  ordinal: number;
  role: string;
  content: string;
  timestamp_ms: number | null;
  source_message_id: string | null;
  content_hash: string;
  duplicate: boolean;
}

export interface PreviewToolEvent {
  conversation_id: string;
  ordinal: number;
  source_event_id: string;
  source_event_seq: number;
  event_type: "tool/call" | "tool/result";
  timestamp_ms: number;
  turn: number | null;
  step: number | null;
  call_id: string | null;
  tool_name: string | null;
  arguments: Record<string, unknown> | null;
  result_content: Record<string, unknown>[] | null;
  result_text: string | null;
  is_error: boolean | null;
  source_event_seqs: number[];
  content_hash: string;
  duplicate: boolean;
}

export interface ImportPreview {
  preview_id: string;
  source_type: string;
  filename: string;
  file_size: number;
  raw_file_hash: string;
  umif_package_id: string;
  umif_record_count: number;
  umif_warnings: string[];
  conversation_count: number;
  message_count: number;
  duplicate_message_count: number;
  tool_call_count: number;
  tool_result_count: number;
  duplicate_tool_event_count: number;
  source_event_conflict_count: number;
  skipped_event_counts: Record<string, number>;
  warnings: string[];
  messages: PreviewMessage[];
  tool_events: PreviewToolEvent[];
  duplicate_batch_id: string | null;
  import_report?: { counts?: Record<string, number>; warnings?: string[]; retained_source_files?: string[] };
}

export interface ImportBatch {
  batch_id: string;
  source_type: string;
  original_filename: string;
  app_id: string;
  project_id: string;
  user_id: string;
  agent_id: string;
  raw_file_hash: string;
  umif_package_id: string;
  umif_record_count: number;
  conversation_count: number;
  message_count: number;
  duplicate_message_count: number;
  archived_at: string;
  extraction_status: string;
}

export interface ProjectSpace {
  project_id: string;
  name: string;
  parent_project_id: string | null;
  description: string;
  full_path: string;
  depth: number;
}

export type LibraryStatus = "valid" | "expired" | "all";
export interface LibraryMemory {
  id: string;
  everos_memory_id: string;
  project_id: string;
  namespace: string;
  kind: string;
  memory_type: "atomic_fact" | "episode" | "profile";
  content_preview: string;
  timestamp: string;
  external_use: "allow" | "ask" | "deny";
  status: "valid" | "expired";
  lifecycle_status: "active" | "withdrawn" | "deleted";
  revision: number;
  evidence_kind: string;
  valid_until: string | null;
}
export interface LibraryEvidence {
  filename: string;
  message_id: string;
  role: string;
  timestamp_ms: number | null;
  quote: string;
  available: boolean;
  archive_status: "available" | "missing" | "hash_mismatch" | "hash_unavailable";
  excerpt_matched: boolean;
}
export interface SourceKeyPosition {
  char_start: number;
  char_end: number;
  source_char_start: number;
  source_char_end: number;
  text: string;
  method: "lexical";
}
export interface SourceSegmentMessage {
  message_id: string;
  ordinal: number;
  role: string;
  timestamp_ms: number | null;
  source_char_start: number;
  source_char_end: number;
  content: string;
  key_position: SourceKeyPosition | null;
}
export interface SourceSegment {
  version: 1;
  precision: "exact_input" | "linked_messages";
  conversation_id: string | null;
  filename: string;
  archive_available: boolean;
  archive_status: "available" | "missing" | "hash_mismatch" | "hash_unavailable";
  segment_status: "valid" | "legacy_fallback" | "hash_mismatch" | "invalid";
  key_position_status: "located" | "not_found";
  input_sha256?: string | null;
  messages: SourceSegmentMessage[];
}
export interface LibraryMemoryDetail extends LibraryMemory {
  content: string;
  metadata: Record<string, unknown>;
  evidence: LibraryEvidence[];
  evidence_available: boolean;
  source_segment: SourceSegment | null;
  lifecycle_note: string | null;
}
export interface LibraryProjectCount {
  project_id: string;
  name: string;
  visible_valid_count: number;
  syncing_count: number;
}
export interface LibraryListResult {
  memories: LibraryMemory[];
  visible_total: number;
  missing_index_ids: string[];
}

export interface ExtractionResult {
  status: "extracted" | "no_new_messages" | "deferred_tool_messages" | "unsupported_role_messages";
  method: "local" | "none";
  candidate_count: number;
  candidate_scope: { app_id: string; project_id: string; user_id: string } | null;
  deferred_tool_message_count: number;
  unsupported_role_message_count: number;
}

export type CandidateKind = "preference" | "fact" | "decision" | "todo" | "episode" | "profile";
export type PackKind = "fact" | "episode" | "profile";
export type PackFormat = "markdown" | "vault-memory/1";
export type PackEvidenceMode = "none" | "id" | "snippet" | "full";
export type ExternalUsePolicy = "allow" | "ask" | "deny";

export interface PackPreviewRequest {
  namespaces: string[];
  kinds: PackKind[];
  format: PackFormat;
  token_budget: number;
  evidence_mode: PackEvidenceMode;
  memory_ids: string[] | null;
}

export interface PackPreviewItem {
  memory_id: string;
  revision: number;
  kind: PackKind;
  namespace: string;
  external_use: ExternalUsePolicy;
  lifecycle_status: "active" | "withdrawn" | "deleted";
  validity_status: "valid" | "expired";
  display_content: string | null;
  eligible: boolean;
  selected: boolean;
  included: boolean;
  exclusion_reason: string | null;
}

export interface PackPreview {
  rendered_content: string;
  generated_at: string;
  preview_sha256: string;
  content_sha256: string;
  items: PackPreviewItem[];
  selected_ask_ids: string[];
  exclusion_counts: Record<string, number>;
  included_count: number;
  estimated_tokens: number;
  token_budget: number;
  estimate_is_approximate: boolean;
}

export interface PackExportRequest extends PackPreviewRequest {
  memory_ids: string[];
  preview_sha256: string;
  preview_generated_at: string;
  confirmed_ask_ids: string[];
  confirm_evidence: boolean;
  confirm_unrecoverable_transfer: boolean;
  delivery_action: "copy" | "download";
}

export interface PackExport {
  rendered_content: string;
  generated_at: string;
  preview_sha256: string;
  content_sha256: string;
  format: PackFormat;
  included_ids: string[];
  included_revisions: Record<string, number>;
  included_count: number;
  estimated_tokens: number;
}

export type CandidateStatus = "pending" | "publishing" | "confirmed" | "rejected";
export interface CandidateEvidence {
  message_id: string;
  role: string;
  timestamp_ms: number | null;
}
export interface SourceConversation {
  batch_id: string;
  filename: string;
  conversation_id: string;
  selected_message_id: string;
  source_segment: SourceSegment | null;
  messages: { id: string; ordinal: number; role: string; content: string; timestamp_ms: number | null }[];
}
export interface ReviewCandidate {
  id: string;
  batch_id: string;
  filename: string;
  project_id: string;
  namespace: string;
  kind: CandidateKind;
  memory_type: "atomic_fact" | "episode" | "profile";
  content: string;
  status: CandidateStatus;
  revision: number;
  evidence_kind: string | null;
  external_use: string;
  origin: "import" | "agent" | "manual";
  evidence_status: "valid" | "missing" | "mismatch" | "not_required";
  validity_days: 30 | 90 | null;
  valid_until: string | null;
  created_at: string;
  evidence: CandidateEvidence[];
  metadata?: Record<string, unknown>;
}

interface Envelope<T> {
  request_id: string;
  data: T;
}

async function requestData<T>(response: Response): Promise<T> {
  const body = await response.json();
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(
      typeof detail?.message === "string"
        ? `${detail.stage ? `[${detail.stage}] ` : ""}${detail.code ? `${detail.code}: ` : ""}${detail.message}`
        : `Vault API returned HTTP ${response.status}`,
    );
  }
  return (body as Envelope<T>).data;
}

export async function previewMemoryPack(
  request: PackPreviewRequest,
  signal?: AbortSignal,
): Promise<PackPreview> {
  return requestData<PackPreview>(await ownerFetch("/api/v1/packs/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal,
  }));
}

export async function exportMemoryPack(request: PackExportRequest): Promise<PackExport> {
  return requestData<PackExport>(await ownerFetch("/api/v1/packs/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  }));
}

export async function getHealth(): Promise<VaultHealth> {
  return requestData<VaultHealth>(await ownerFetch("/api/v1/system/health"));
}

export async function getProjects(): Promise<ProjectSpace[]> {
  const result = await requestData<{ projects: ProjectSpace[] }>(
    await ownerFetch("/api/v1/projects"),
  );
  return result.projects;
}

export async function getMemoryLibrarySummary(): Promise<LibraryProjectCount[]> {
  const result = await requestData<{ projects: LibraryProjectCount[] }>(
    await ownerFetch("/api/v1/memories/summary"),
  );
  return result.projects;
}

export async function getLibraryMemories(
  projectId: string,
  query: string,
  status: LibraryStatus,
): Promise<LibraryListResult> {
  const params = new URLSearchParams({ project_id: projectId, status });
  if (query.trim()) params.set("q", query.trim());
  return requestData<LibraryListResult>(
    await ownerFetch(`/api/v1/memories?${params.toString()}`),
  );
}

export async function getLibraryMemory(
  projectId: string,
  memoryId: string,
): Promise<LibraryMemoryDetail> {
  const params = new URLSearchParams({ project_id: projectId });
  return requestData<LibraryMemoryDetail>(
    await ownerFetch(`/api/v1/memories/${encodeURIComponent(memoryId)}?${params.toString()}`),
  );
}

export async function createProject(
  name: string,
  parentProjectId: string | null,
  description: string,
): Promise<ProjectSpace> {
  return requestData<ProjectSpace>(
    await ownerFetch("/api/v1/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, parent_project_id: parentProjectId, description }),
    }),
  );
}

export async function correctLibraryMemory(
  projectId: string,
  memory: LibraryMemory,
  content: string,
  externalUse: LibraryMemory["external_use"],
  note: string,
): Promise<LibraryMemoryDetail> {
  const params = new URLSearchParams({ project_id: projectId });
  return requestData<LibraryMemoryDetail>(await ownerFetch(`/api/v1/memories/${encodeURIComponent(memory.id)}?${params.toString()}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision: memory.revision, content, external_use: externalUse, note: note.trim() || null }),
  }));
}

export async function withdrawLibraryMemory(
  projectId: string,
  memory: LibraryMemory,
): Promise<LibraryMemoryDetail> {
  const params = new URLSearchParams({ project_id: projectId });
  return requestData<LibraryMemoryDetail>(await ownerFetch(`/api/v1/memories/${encodeURIComponent(memory.id)}/withdraw?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision: memory.revision }),
  }));
}

export async function deleteLibraryMemory(
  projectId: string,
  memory: LibraryMemory,
): Promise<LibraryMemoryDetail> {
  const params = new URLSearchParams({ project_id: projectId, revision: String(memory.revision) });
  return requestData<LibraryMemoryDetail>(await ownerFetch(`/api/v1/memories/${encodeURIComponent(memory.id)}?${params.toString()}`, {
    method: "DELETE",
  }));
}

export async function previewImport(
  file: File,
  sourceType: string,
  projectId: string,
  signal: AbortSignal,
): Promise<ImportPreview> {
  const form = new FormData();
  form.append("file", file);
  form.append("source_type", sourceType);
  form.append("project_id", projectId);
  return requestData<ImportPreview>(
    await ownerFetch("/api/v1/imports/preview", { method: "POST", body: form, signal }),
  );
}

export async function cancelPreview(previewId: string): Promise<void> {
  await requestData<{ cancelled: boolean }>(
    await ownerFetch(`/api/v1/imports/previews/${encodeURIComponent(previewId)}`, {
      method: "DELETE",
    }),
  );
}

export async function commitImport(
  previewId: string,
  projectId: string,
): Promise<ImportBatch> {
  return requestData<ImportBatch>(
    await ownerFetch("/api/v1/imports/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preview_id: previewId, project_id: projectId }),
    }),
  );
}

export async function extractImport(batchId: string): Promise<ExtractionResult> {
  return requestData<ExtractionResult>(
    await ownerFetch(`/api/v1/imports/${encodeURIComponent(batchId)}/extract`, {
      method: "POST",
    }),
  );
}

export async function getCandidates(filters: { status?: string; kind?: string; origin?: string; evidence?: string } = {}): Promise<ReviewCandidate[]> {
  const params = new URLSearchParams();
  if (filters.status && filters.status !== "all") params.set("status", filters.status);
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.origin) params.set("origin", filters.origin);
  if (filters.evidence) params.set("evidence", filters.evidence);
  const suffix = params.size ? `?${params.toString()}` : "";
  const result = await requestData<{ candidates: ReviewCandidate[]; total: number }>(
    await ownerFetch(`/api/v1/candidates${suffix}`),
  );
  return result.candidates;
}

export interface ManualCandidateInput {
  kind: "episode" | "preference";
  namespace: string;
  validity_days: 30 | 90 | null;
  external_use: "allow" | "ask" | "deny";
  content: string;
}

export async function createManualCandidate(input: ManualCandidateInput): Promise<ReviewCandidate> {
  return requestData<ReviewCandidate>(await ownerFetch("/api/v1/candidates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }));
}

export async function getCandidateSource(
  candidateId: string,
  messageId: string,
  content: string,
): Promise<SourceConversation> {
  return requestData<SourceConversation>(await ownerFetch(
    `/api/v1/candidates/${encodeURIComponent(candidateId)}/source/${encodeURIComponent(messageId)}`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) },
  ));
}

export interface CandidateChanges { content: string; kind: CandidateKind; namespace: string; validity_days: 30 | 90 | null; external_use: "ask" | "allow" | "deny" }

export async function confirmCandidate(candidate: ReviewCandidate, changes: CandidateChanges): Promise<void> {
  await requestData<{ id: string; status: string; everos_memory_id: string; revision: number }>(
    await ownerFetch(`/api/v1/candidates/${encodeURIComponent(candidate.id)}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        revision: candidate.revision,
        content: changes.content,
        kind: changes.kind,
        namespace: changes.namespace,
        external_use: changes.external_use,
        validity_days: changes.validity_days,
        evidence_kind: candidate.origin === "manual" ? "human_authored" : "human_verified",
      }),
    }),
  );
}

export async function rejectCandidate(candidate: ReviewCandidate): Promise<void> {
  await requestData<{ id: string; status: string }>(
    await ownerFetch(`/api/v1/candidates/${encodeURIComponent(candidate.id)}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision: candidate.revision }),
    }),
  );
}

export interface ClaudeJob {
  id: string; status: string; project_id: string; filename: string; batch_id?: string | null;
  error_code: string | null; error_message: string | null; retryable: boolean;
  input_kind?: 'manifest' | 'single_zip' | 'local_bundle'; part_count?: number; created_at?: string; manifest_verified?: boolean;
  progress: Record<string, unknown>;
}
export interface ClaudeRecord {
  record_id: string; title?: string; text: string; kind?: string;
  source_project_id?: string | null; status: string; target_id?: string | null;
  review_decision?: 'knowledge' | 'vault_only' | 'exclude' | null; reviewed_at?: string | null;
  text_length?: number; text_truncated?: boolean;
}
export interface ClaudeContent {
  documents: ClaudeRecord[]; assertions: ClaudeRecord[];
  agent_event_count: number;
  agent_events: { record_id: string; record_type: string; call_id?: string;
    tool_name?: string; sequence: number; timestamp_ms: number | null;
    is_error: boolean; preview: string }[];
  assets: { record_id: string; filename?: string; media_type?: string; bytes: number; status: string;
    target_id?: string | null; review_decision?: 'knowledge' | 'vault_only' | 'exclude' | null;
    linked_document_ids: string[] }[];
  source_files: { name?: string; original_name?: string; sha256?: string; media_type?: string;
    bytes?: number; status?: string; container?: string }[];
  source_file_total?: number; source_files_truncated?: boolean;
  scope: { app_id: string; project_id: string };
  report: { warnings?: string[]; counts?: Record<string, number> };
}
export async function claudeRequest<T>(path: string, method = 'GET', body?: BodyInit): Promise<T> {
  return requestData<T>(await ownerFetch(`/api/v1/${path}`, {
    method, body, headers: typeof body === 'string' ? { 'Content-Type': 'application/json' } : undefined,
  }));
}
