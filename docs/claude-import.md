# Claude export import

MEMO Vault accepts a Claude export manifest (`version: "1.0"`) or a set of
locally downloaded export ZIPs from the **Claude export** source. Each
acquisition job belongs to one target Vault project. Select all ZIPs from one
export snapshot in the Claude panel to create one local job and one UMIF package.
Local ZIP upload does not use Claude download links or require a Claude session.
At most 999 ZIPs can be selected in one local request.
The local upload can include the manifest as an optional inventory check: its
filename set must exactly match the selected ZIPs, and its URLs are not opened.
Without it, the user must confirm that all ZIPs from the export were selected.
The original single-ZIP API remains available for existing callers.

## Workflow

The console now uses the shared five-step wizard: **select source → upload and
check → parse preview → extract and review → complete**. Claude's multi-ZIP
controls and job history appear within that wizard; they do not introduce a
second progress bar. A committed job can be reopened from the upload step.

1. Select the target project. Choose all locally downloaded ZIPs together, or
   upload a manifest when its links are accessible.
2. Local ZIPs are uploaded in one multipart request, saved with SHA-256 values,
   and processed in one durable job. Manifest jobs download each part with a
   complete streamed GET; they do not issue HEAD requests.
3. Completed files are cached for restart and retry. Manifest downloads retain
   SHA-256 markers; missing markers after a crash can be recovered from ZIPs.
4. All parts are parsed together, converted into one UMIF package, and validated.
5. Prepare a preview. If records refer to multiple source projects, explicitly
   confirm mapping them all to the selected target project. Automatic creation
   of separate target projects is not supported in this iteration.
6. Confirm archival. The original ZIPs, original manifest, immutable UMIF package,
   blobs, and conversion report are retained locally.
7. Extract conversation candidates and review them through the existing queue.
   Saved memories have a separate editable review action and remain identified
   as Claude platform summaries. Project documents stay in Vault unless the
   user selects **批准进入 Knowledge**. That single action records the approval
   and starts delivery; a result with uncertain delivery status can be
   reconciled from the same card. An attachment already linked to a project
   document follows that document's approval. Existing review API decisions
   remain available for compatibility, but the import screen presents only
   the Knowledge approval action.

The import content inventory lists UMIF records by conversation, Agent,
Knowledge and assertion track, plus the source ZIP members that were retained
without a UMIF projection. It returns metadata and safe previews, not binary
attachment bytes. DeepSeek tool events remain in `normalized_tool_events` and
are not sent to conversation candidate extraction.

No import publishes a formal memory without review. System/developer messages
are retained in UMIF and excluded from ordinary user-memory projection. Hidden
reasoning blocks are counted as unconverted and remain only in original evidence.
Tool events are archived when explicitly present; the existing Vault chat
extraction path does not produce Agent Cases or Skills from them.

## Recovery

The Claude panel lists local jobs after refresh, including input kind, file
count, target project, creation time, short job ID and status. Reopen a committed
job to access its archived documents and saved summaries. Queued and interrupted
active jobs resume when the API restarts; explicit user cancellation remains
cancelled. A multi-ZIP local job can retry its saved files. Supplying an extra
ZIP after job creation is supported only for manifest jobs, using the exact
manifest filename. Uploading each ZIP as a separate job does not combine them.

An unavailable link produces `CLAUDE_LINK_EXPIRED` (HTTP 401/403/404/410). This
code means unavailable, not proof of the exact cause. Other valid parts are still
downloaded. Supply missing ZIPs using their exact manifest filenames, then retry.
Completed caches are reused. A link returning an HTML page instead of a ZIP is
reported as `CLAUDE_EXPORT_NOT_AVAILABLE`, and the remaining parts are still
attempted. Both `claude.ai` and `claude.com` export URLs and the observed domain
migration redirect are supported. A fresh export manifest can also start a new job;
exports from different snapshots must not be mixed.

A Knowledge request with an uncertain outcome is marked `delivery_unknown` and
cannot be blindly repeated. **Reconcile previous delivery** searches the target
app/project for the content-addressed upload filename. A match records the
remote document ID. If none is found, the document remains archived and the
uncertain request stays blocked pending operator investigation. Reconciliation
checks at most 100 pages of 100 documents; it does not prove a timed-out request
can never finish later. HTTP 4xx rejections can be retried after configuration or
content issues are resolved.

A trusted redirect to `/app-unavailable-in-region` or `/unsupported-country`
produces `CLAUDE_REGION_UNAVAILABLE`. This distinguishes server-region access
restrictions from export expiration. See the [2026-10-24 live verification](testing/2026-10-24-claude-live-import.md).

## Supported source shapes

The adapter currently recognizes arrays of records, or objects wrapping the same
arrays under `users`, `conversations`, `projects`, or `memories`. Corresponding
JSON filenames may have numbered suffixes. Unknown shapes fail explicitly rather
than being treated as a successful empty import.

- Users: `uuid`/`id` supplies the source namespace. Account email is not copied
  into normalized records. Missing account IDs use the local owner namespace
  with a warning; only one source account per package is accepted.
- Conversations: `uuid`/`id`, `chat_messages`/`messages`, optional project reference.
- Messages: `uuid`/`id`, `sender`/`role`, `created_at`, and text content blocks.
  Text from `content` is preferred over the duplicate `text` field. Explicit
  `tool_use`, `tool_result`, and text-bearing `artifact` blocks are supported.
- Projects: `uuid`/`id`, `name`, `prompt_template`/`instructions`, and exported
  text in `docs`/`documents` entries. The live export shape
  `projects/<project-id>.json` is supported. Project-only ZIPs must be imported
  with account metadata through the complete manifest; library callers may
  supply a verified `source_account_id`. A shared project creator is not assumed
  to be the exporting account.
- Saved memories: `uuid`/`id` and `content`/`text`/`memory` strings, or the live
  `memories/<account-id>.json` shape with `account_uuid` and `memory_files`
  (`path`, `content`, `updated_at`). Each memory file becomes one assertion; its
  stable identity uses account and path, while content updates change its hash. They are source
  assertions, not reconstructed user speech. Reviewers can shorten summaries to
  4000 characters while retaining original evidence. Candidates use the existing
  profile/preference review type and carry `origin_kind=platform_summary`.
- Files: raw members become content-addressed Assets. Unambiguous attachment
  filenames are linked to messages. Missing/ambiguous files are reported.

Private branch fields are preserved, but this iteration uses exported message
list order and does not reconstruct branch graphs. Unknown JSON files and
content blocks are counted in the report, retained in raw evidence, and not
silently routed to Knowledge. Project entries with exported binary files are linked by unambiguous filename
and delivered in their original format. PDF and other rich formats depend on
the configured EverOS parser. Missing document files are reported.

Missing source times remain missing in UMIF and archived messages. If the
existing candidate extraction API needs a timestamp, processing-time inference
is explicitly marked in candidate metadata. Saved-summary candidate timestamps
represent review creation time, with the original source timestamp stored
separately. Original source time is never overwritten in the package.

**Compatibility status:** Adapter `claude-export-2` has been tested against
all four actual ZIPs supplied locally on 2026-10-24: one conversation/four
messages, two tool calls with matching results, two project documents, and one
saved memory file. The complete package passed validation and archival, and
local Ollama extraction produced one conversation candidate. The saved memory
created another pending candidate. One referenced conversation attachment is
absent from the source export and is explicitly reported. Thinking, injected
prompt blocks, and login history are excluded from memory projection.

Knowledge delivery in this environment is blocked by the core API's configured
capabilities: `PROVIDER_NOT_CONFIGURED` for `rerank`. It is reported as
`EVEROS_RERANK_NOT_CONFIGURED` with `delivery_rejected`, preserving the archived
documents for retry after configuration. A definitive HTTP rejection must not
be confused with an unknown network outcome.

## Storage and limits

Job state is stored in Vault SQLite (`import_jobs`); raw acquisitions and staged
packages live in `<MEMO_VAULT_DATA_DIR>/import-jobs/<job_id>/`. Committed packages
and copied source files live in the existing evidence batch directory. Job
storage is private to the local account; this feature does not add encryption or
multi-user authentication.

| Setting | Default |
|---|---:|
| `MEMO_VAULT_CLAUDE_MAX_DOWNLOAD_BYTES` | 512 MiB per ZIP/upload |
| `MEMO_VAULT_CLAUDE_MAX_TOTAL_BYTES` | 2 GiB per manifest acquisition |
| `MEMO_VAULT_CLAUDE_MAX_UNPACKED_BYTES` | 512 MiB across all parsed parts |
| `MEMO_VAULT_CLAUDE_MAX_FILES` | 10,000 members |
| Manifest upload limit | 2 MiB |
| Maximum compression ratio | 200 |
| Concurrent acquisition workers per API process | 2 |

ZIP reads are bounded by declared and actual member sizes. JSON documents are
currently decoded in memory within these limits; the implementation is not an
unbounded streaming JSON parser. The existing preview metadata also retains
normalized records in memory. Set limits to fit the local process memory budget.

Downloads allow HTTPS Claude addresses and restricted storage redirects. DNS
answers must be public and connections are pinned to a validated address while
preserving TLS hostname validation. HTTP-library logs are suppressed only in the
private download context, so signed URLs and redirect credentials are not logged.
ZIP paths, duplicates, links, compression, byte quotas and Blob hashes are checked.

## API

All endpoints are on the Vault API, normally port 8765.

| Method and path | Purpose |
|---|---|
| `POST /api/v1/import-jobs` | Multipart `file`, `project_id`; returns 202 |
| `POST /api/v1/import-jobs/local` | Multipart repeated `files` ZIP fields, `project_id`, optional `manifest` JSON for offline filename-set validation; one job for the complete set, returns 202 |
| `GET /api/v1/import-jobs` | List recent jobs |
| `GET /api/v1/import-jobs/{id}` | State, progress, report, errors and committed batch ID |
| `POST /api/v1/import-jobs/{id}/retry` | Retry unfinished work using local caches |
| `POST /api/v1/import-jobs/{id}/cancel` | Request cancellation |
| `POST /api/v1/import-jobs/{id}/parts` | Supply an original missing ZIP by filename |
| `POST /api/v1/import-jobs/{id}/preview` | JSON `{ "confirm_project_merge": false }` |
| `POST /api/v1/imports/commit` | Existing immutable preview commit |
| `GET /api/v1/imports/{batch}/claude-content` | Archived documents, assertions and states |
| `GET /api/v1/imports/{batch}/content-inventory` | Paginated UMIF track records and retained source-member metadata |
| `POST /api/v1/imports/{batch}/assertions/{record}/review` | Optional `{ "content": "reviewed summary" }` |
| `POST /api/v1/imports/{batch}/documents/{record}/review` | `{ "decision": "knowledge" \| "vault_only" \| "exclude" }` |
| `POST /api/v1/imports/{batch}/assets/{record}/review` | Review an unlinked standalone attachment |
| `POST /api/v1/imports/{batch}/documents/{record}/deliver` | `{ "confirmed": true }` |
| `POST /api/v1/imports/{batch}/assets/{record}/deliver` | Deliver an approved standalone attachment |
| `POST /api/v1/imports/{batch}/documents/{record}/reconcile` | Resolve an uncertain upload |

The standalone Vault schema is in `memo-vault-openapi.json`. Core EverOS also
exposes the new scoped Knowledge original and ZIP download endpoints described
in [Knowledge Base](knowledge.md). Existing UMIF packages remain readable: Blob
storage is additive, and existing optional record envelopes are preserved.
