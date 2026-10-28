# Unified Import Tracks and Knowledge Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use one five-step import workflow across sources, review imported files before Knowledge delivery, preserve DeepSeek tool traces, and export selected project Knowledge originals individually or as a ZIP with optional generated Markdown.

**Architecture:** Keep UMIF and Vault evidence immutable. Add a Vault review ledger and inventory API for imported Assets/Documents, then let the shared React wizard render source-specific acquisition inside common stages. Add scoped Knowledge download/export operations to the core API and a separate Knowledge tab beside Memory Pack.

**Tech Stack:** Python 3.12, FastAPI, SQLite, Pydantic, React/TypeScript, native `zipfile`.

**Spec:** `docs/superpowers/specs/2026-10-26-unified-import-tracks-and-exports-design.zh-CN.md`

## Global Constraints

- Work in the current `/root/EverOS` checkout as previously requested; preserve existing uncommitted changes.
- Claude multi-ZIP remains one job, one UMIF package, one Vault batch; offline Manifest validation does not open URLs.
- DeepSeek Harness parser, `normalized_tool_events`, preview redaction, and tool exclusion from user-memory extraction retain their behavior.
- Original archives and UMIF remain recoverable evidence; only reviewed candidates become formal memories and only approved files enter Knowledge.
- Project scope is `app_id/project_id`; exports must not disclose server paths or cross projects.
- No commits to the shared dirty checkout in this execution.

## Review Focus

- A document present in two projects must only be downloadable within the selected project.
- Missing or replaced originals must fail clearly, without exposing an absolute path or silently changing ZIP contents.
- ZIP member names from uploaded document titles must not escape the archive or overwrite same-name siblings.
- Retried Knowledge delivery must not create duplicates, and a delivery with an unknown outcome must be reconciled before retry.
- DeepSeek tool events must remain archived and must not enter candidate extraction when the wizard is refactored.

---

### Task 1: Scoped Knowledge original and ZIP export

**Files:** Modify `src/everos/entrypoints/api/routes/knowledge.py`; create `src/everos/service/knowledge_export.py`; add focused API tests under `tests/unit/test_entrypoints/`.

**Interfaces:** `GET /api/v2/knowledge/documents/{doc_id}/original?app_id=&project_id=` streams the original file; `POST /api/v2/knowledge/exports` accepts scoped `doc_ids: list[str]` and `include_derived: bool`, returning a ZIP. Source metadata comes from `get_document` in `service/knowledge.py`; selected originals are checked against the project Knowledge root. ZIP contains `manifest.json`, `originals/<doc_id>/<safe-name>`, and optionally per-document summary and Topic Markdown.

- [ ] Write failing tests for exact original bytes, project isolation, missing originals, malicious/same filenames, selected-only ZIP, optional Topic Markdown, and quota rejection.
- [ ] Run the focused tests and verify failures show missing endpoint/service behavior.
- [ ] Implement project-scoped file resolution, streaming and bounded ZIP construction using the current Knowledge storage layout.
- [ ] Run focused tests and read results.

### Task 2: Vault imported content inventory and review decisions

**Files:** Modify `src/everos/entrypoints/memo_vault/storage.py`, `routes/claude_content.py`, `app.py`; add a focused review/inventory module if needed; extend `tests/unit/test_entrypoints/test_claude_import.py`.

**Interfaces:** `GET /api/v1/imports/{batch_id}/content-inventory` returns all UMIF Assets/Documents/Assertions and retained unrecognized source members with source provenance, type, status and target IDs. `POST /api/v1/imports/{batch_id}/documents/{record_id}/review` persists `knowledge_approved | archive_only | excluded` and optional review note. Existing delivery route requires `knowledge_approved`; old committed batches remain readable and may be reviewed. `umif_record_map` or an additive review table stores the crosswalk.

- [ ] Write failing tests for complete inventory, archive-only/excluded persistence, denied unapproved delivery, approved delivery, restart recovery and project scope.
- [ ] Run focused tests to confirm missing behavior.
- [ ] Add additive database migration and API, reusing archived UMIF as the source of inventory data.
- [ ] Run focused tests and read results.

### Task 3: One five-step import wizard

**Files:** Modify `apps/memo-vault-ui/src/App.tsx`, `ClaudeImport.tsx`, `api.ts`, `styles.css`; add focused UI behavior checks if the project has a suitable test harness.

**Interfaces:** Shared wizard state owns `source`, `step`, `job`, `preview`, `batch` and extraction state. Claude acquisition panel only renders Stage 2 inputs/progress/history and invokes parent callbacks. Stage 3 renders source-specific preview content; Stage 4 is labelled `抽取与审核` and shows memory, Agent trace and Knowledge file sections; Stage 5 shows consolidated receipt and pending work.

- [ ] Capture intended stage transitions and DeepSeek unchanged extraction as a failing behavior check or focused component test.
- [ ] Remove Claude's nested stepper and wire its job/preview/commit/extraction transitions into the parent wizard.
- [ ] Render inventory decisions and Knowledge delivery status in Stage 4; retain resume/retry and historical committed jobs.
- [ ] Build TypeScript and run the focused checks.

### Task 4: Export center UI and integration

**Files:** Modify `apps/memo-vault-ui/src/App.tsx`, `api.ts`, `styles.css`; create a focused `KnowledgeExport.tsx`; update `docs/knowledge.md` and `docs/claude-import.md`.

**Interfaces:** Existing Memory Pack UI remains a tab of `导出中心`; Knowledge tab lists selected `app_id/project_id` documents with category/title/date/source filters, single original download, multi-select ZIP, and `include_derived` option. All downloads use authenticated backend streams and browser Blob URLs; no absolute paths are displayed.

- [ ] Check selected-project filtering, file selection, disabled missing originals and ZIP option in a focused UI behavior check if available.
- [ ] Implement UI and API client methods, then run TypeScript build.
- [ ] Update docs with the new review and export behavior.
- [ ] Verify focused backend tests, frontend build, DeepSeek regression checks, and inspect `git diff --check`.

## Final release gate

- [ ] Check every requirement in the spec against the implementation.
- [ ] Run the relevant Claude and DeepSeek import tests and Knowledge export tests; report any pre-existing failure separately.
- [ ] Confirm the frontend production build and lint checks that are available in this checkout.
- [ ] Inspect only task-owned diffs and leave unrelated user changes intact.
