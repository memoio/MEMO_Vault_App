# Claude Export Import Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task by task.

**Goal:** Import Claude manifest exports through durable downloads, validated UMIF, and reviewed Vault ingestion.
**Architecture:** Cache each export ZIP before parsing. Convert the whole export into a deterministic package, persist an immutable preview, and reuse Vault commit and candidate review. Preserve documents and assertions with explicit review and delivery states.
**Tech Stack:** Python, FastAPI, SQLite, httpx, React/TypeScript.
**Spec:** Approved design in the conversation; UMIF specification in `docs/superpowers/specs/UMIF统一中间数据格式与EverOS接入规范.md`.

## Global Constraints

- Never log signed links or private export contents.
- Download complete ZIPs, reuse cache, validate paths and resource limits.
- Preserve source roles, provenance, original timestamps and project identities.
- Source adapters emit UMIF only; formal memories require review.
- Do not execute source content or fabricate tool events.

## Review Focus

- Expired or interrupted one-use links preserve completed downloads.
- Restart and concurrent retries do not run duplicate tasks.
- Cross-file references and unavailable attachments remain visible.
- Changed signed links do not change source record identity.
- Instructions and saved summaries do not become user messages.

### Task 1: Durable export acquisition

Files: `export_downloader.py`, `claude_manifest.py`, `import_jobs.py`, `routes/import_jobs.py`, `app.py`; tests `test_claude_import.py`.
Interfaces: manifest bytes -> validated entries; job manager create/run/status/retry/cancel -> durable SQLite state and cached paths.
- [x] Write and run failing tests for URL validation, ZIP cache reuse, expired URLs and restart recovery.
- [x] Implement bounded streamed acquisition with safe errors and persistent job state.
- [x] Run acquisition tests.

### Task 2: Claude adapter and UMIF integrity

Files: `claude_export.py`, UMIF `models.py`, `package.py`, `validator.py`; tests `test_claude_import.py`.
Interfaces: cached archive paths + target project -> UmifPackage with blobs and import report.
- [x] Inspect real export member schemas: completed using the four user-supplied ZIPs on 2026-10-24; directory projects and memory_files are covered by redacted fixtures.
- [x] Write and run failing fidelity, stable-ID, unsafe ZIP and blob validation tests.
- [x] Implement conversation, project, document and assertion conversion with explicit unsupported reports.
- [x] Run adapter and existing UMIF tests.

### Task 3: Vault workflow and UI

Files: `imports.py`, `umif_projection.py`, import job routes, `App.tsx`, `api.ts`.
Interfaces: completed job -> immutable UMIF preview -> existing commit; archived documents/assertions -> review/delivery endpoints.
- [x] Write and run failing preview/commit, duplicate, project scope and saved assertion review tests.
- [x] Integrate cached package preview and original evidence retention.
- [x] Expose Claude source, acquisition progress, recovery and reports in UI.
- [x] Verify backend regression tests and frontend build.

### Task 4: Review and documentation

- [x] Document supported formats, recovery, limits and data loss reporting.
- [x] Run focused tests, repository checks and full unit suite where feasible.
- [x] Obtain independent final code review; address important findings and report evidence.

## Execution notes

User authorized execution of the proposed design. Changes stay in the current feature checkout unless an alternative workspace preference arrives. No commits or external publishing are required. Real exports remain in ignored local runtime storage.


## Validation and review record

- Independent reviewer examined download security, source fidelity, recovery,
  deduplication, review compatibility and Knowledge delivery. Important findings
  were addressed with regression tests: complete ZIP marker recovery, account
  scoped tool IDs, editable platform summaries, review labels, all-source project
  reference checks, service interruption recovery, and overlapping assertions.
- Source acquisition was run against the supplied manifest in local job
  `e4ddba2456144d07a625876eed5afbb3`. No memories were committed. All four links
  now resolve through the Claude domain migration to HTML pages; the job retains
  per-part `CLAUDE_EXPORT_NOT_AVAILABLE` diagnostics and accepts ZIP replacement.
- The full unit command was attempted but collection is blocked by missing
  `opentelemetry` and `pymilvus` dependencies in this environment.
- One existing DeepSeek test is excluded from the relevant green regression run:
  `test_deepseek_harness_tool_traces_are_not_sent_to_memory_extraction`.
  The same failure was reproduced from an unmodified HEAD archive.
- Repository datetime checks expose existing violations in Vault modules; newly
  introduced code uses the datetime helpers.
- Design choices: keep user-authorized work in the current checkout, map source
  projects to one explicitly selected target with multi-project confirmation,
  route saved summaries to the current profile/preference review flow, preserve
  source evidence, and keep uncertain Knowledge outcomes blocked until reconciled.
- Real-export conversion, UMIF archival, duplicate protection, and local conversation candidate extraction passed on 2026-10-24. Knowledge delivery remains capability-blocked because rerank is not configured. See docs/testing/2026-10-24-claude-live-import.md.
