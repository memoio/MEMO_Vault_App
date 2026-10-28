# ChatGPT Import Implementation Plan

> **For agentic workers:** Implement the tasks in order in the current workspace. Track each task with its checkbox.

**Goal:** Import a ChatGPT account ZIP through the existing UMIF, Vault review, EverOS memory and export flow.

**Architecture:** A provider-specific ZIP adapter produces a validated UMIF package. Existing Vault projection, candidate review, Knowledge delivery and export services remain the downstream paths. The shared five-step UI gets a ChatGPT source panel rather than a second wizard.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite, React, TypeScript.

**Spec:** `docs/superpowers/specs/2026-10-28-chatgpt-import-design.zh-CN.md`

## Global Constraints

- Only visible user/assistant text on the `current_node` ancestor path enters candidate extraction.
- Preserve other visible branches as UMIF evidence with `user_memory=deny`; do not project hidden thoughts or reasoning recap.
- A missing attachment reference never becomes a Knowledge document.
- Keep Claude and DeepSeek source behavior intact.
- Do not auto-publish candidates or auto-deliver Knowledge.

## Review Focus

- Broken current-node chain: preserve archive but do not extract guessed messages.
- Duplicate native IDs across conversations: use conversation-qualified stable IDs.
- ZIP path traversal, symlink, encryption and decompression limits: reject before preview.
- Branch messages: visible in UMIF, excluded from extraction input.
- Export without file bytes: no fake Knowledge item.

---

### Task 1: ChatGPT ZIP to UMIF adapter

**Files:** Create `src/everos/entrypoints/memo_vault/chatgpt_export.py`; use `src/everos/umif/models.py`, `canonical.py`, `package.py`.

- [x] Inspect the provided ZIP schema and implement `convert_chatgpt_export(payload: bytes, filename: str, project_id: str, *, max_unpacked_bytes: int, max_files: int, max_compression_ratio: int) -> UmifPackage`.
- [x] Build stable sessions/messages from `conversations.json`, with parent-chain ordering, source pointers, privacy routing and skipped-event report.
- [x] Resolve only actual ZIP attachment bytes into `Asset`/`Document`; keep unresolved references in report.
- [x] Seal and validate the package against the supplied sample.

### Task 2: Vault preview, commit and content review

**Files:** Modify `src/everos/entrypoints/memo_vault/imports.py`, `umif_projection.py`, `routes/imports.py`, `routes/claude_content.py`.

- [x] Register `source_type=chatgpt` with project validation, parser version, deduplication and immutable raw ZIP/UMIF archive.
- [x] Keep non-main messages in `normalized_messages` with `memory_input=0`; preserve source-ID mapping and tool-event separation.
- [x] Extend document content/review/delivery endpoints to ChatGPT batches without changing Claude behavior.
- [x] Check candidate extraction reads only `memory_input=1`.

### Task 3: Shared five-step UI

**Files:** Modify `apps/memo-vault-ui/src/App.tsx`, `api.ts`, `styles.css`; create a focused ChatGPT review panel only if needed.

- [x] Enable the ChatGPT source card and single-ZIP upload in the existing wizard.
- [x] Show source-aware preview counts, warnings, visible messages, Agent trace table, and source-file availability.
- [x] Use the same candidate-review, memory-library, Memory Pack and Knowledge export pages as existing sources.
- [x] Show Knowledge approval only for real imported documents; retain unapproved files in Vault.

### Task 4: End-to-end verification and documentation

**Files:** Update `docs/` user guidance as needed.

- [x] Verify the supplied ZIP can preview and archive and that its UMIF passes validation.
- [x] Verify candidate extraction remains an explicit action and reads only current-branch messages; existing library and Memory Pack regression checks pass. Live model extraction and publication remain unverified because Core and Vault are not running locally.
- [x] Verify the supplied ZIP creates no Knowledge attachment because it contains no attachment bytes.
- [x] Verify representative Claude and DeepSeek flows still parse and project correctly (90 selected tests across ChatGPT, Claude, DeepSeek, library and Memory Pack).
