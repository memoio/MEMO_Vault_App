# Vault API/MCP implementation validation

Date: 2026-10-25 (Asia/Shanghai). Branch: `feat/vault-authorization-mcp`.
Existing unrelated working-tree changes were preserved; no merge/push or release
was performed. This report distinguishes feature acceptance from repository-wide
baseline debt.

## Implemented

- Authorization & MCP navigation/page: grant table, reviewed live/fixed forms,
  one-time credentials, recreate/rotate/revoke, API/MCP templates, real HTTP
  connection tests, scoped operation inputs and bounded audit display.
- Owner bootstrap credential, eight-hour HTTP-only sessions, CSRF and exact
  origin/host checks. All owner routes fail closed before initialization.
- Separate external listener, exact scoped live search/get/evidence/context,
  current confirmed canonical source filtering and no ask/deny disclosure.
- Immutable approved-Pack manifests, per-item ask consent, single-use approval
  drafts and atomic single content delivery with version/hash verification.
- Persisted pause/revoke/stale state, one-way stale invalidation, rotation,
  creation retry deduplication, per-grant rate/concurrency leases, byte/scan/time
  caps and metadata-only audit with configurable retention.
- A Vault-only stdio MCP adapter, grant-scoped tool schemas including discoverable
  project/Pack IDs, structured/text results, real HTTP delegation and CLI commands
  `everos vault-mcp` / `everos vault-gateway`.
- Optional SDK dependency `mcp>=1.30.0,<2` and frozen MCP dependency graph. No SDK
  import is required for ordinary owner/API operation.

Guide: [Local setup and API/MCP usage](../memo-vault-authorization.md).
Plan: [Implementation plan](../superpowers/plans/2026-10-23-memory-pack-api-mcp-proposal.md).

## Feature verification

The six focused modules run together: **56 passed** (26 new service/HTTP/MCP
checks plus 30 existing Pack/source/route checks):

```bash
.venv/bin/python -m pytest \
  tests/unit/test_service/test_memo_vault_gateway.py \
  tests/unit/test_entrypoints/test_memo_vault_gateway_http.py \
  tests/unit/test_entrypoints/test_memo_vault_mcp.py \
  tests/unit/test_entrypoints/test_memo_vault_memory_pack.py \
  tests/unit/test_entrypoints/test_memo_vault_pack_sources.py \
  tests/unit/test_entrypoints/test_api/test_routes/test_memo_vault_exports.py -q
```

Chromium and a separate real SDK stdio subprocess: **2 passed**:

```bash
.venv/bin/python -m pytest tests/e2e/test_memo_vault_authorization.py -m slow -q
npm --prefix apps/memo-vault-ui run build
```

Browser coverage includes initial owner login, creating while initially paused,
one-time credential visibility, enabling access, live read, revoke/deny, fixed
Pack review -> authorization -> single delivery, and narrow-screen layout. MCP
coverage includes initialization, discovery, real tool execution and revocation.
The reference client is Python MCP SDK 1.30.0. No untested vendor desktop GUI
compatibility is asserted.

The combined 130-test Vault/import/Pack regression run produced **129 passed,
1 existing failure**, `test_deepseek_harness_tool_traces_are_not_sent_to_memory_extraction`
(see below). Claude and UMIF acceptance tests pass with explicit owner context.
Legacy route tests now provide a trusted owner at the test composition seam,
including tests which restart lifespan. Production has no test bypass flag.

New Python source/tests pass targeted Ruff checks. `git diff --check` is clean.
Installed dependency compatibility (`uv pip check`) and the frozen MCP dependency
graph (`uv tree --frozen --package mcp --no-dev`) pass. A real wheel build verifies
MCP modules, CLI integration, authorization service and optional-extra metadata.

## Independent review and fixes

A separate read-only reviewer checked the feature and found no Critical bypass.
Three Important findings were reproduced with failing tests and fixed:

1. Persisted stale Pack grants could reactivate after a source recovered. Stale
   now blocks authorization and rotation; new approval is required.
2. Capability discovery bypassed limits/final revocation checks. It now uses the
   same bounded admission and audit process, including health-await races.
3. MCP schemas hid required project/Pack identifiers. Discovery now exposes only
   the granted identifiers as bounded schema values, and token-only fixed Pack
   retrieval is tested through the real SDK protocol.

Connection-test input changes were also guarded to prevent old responses being
shown against new parameters. Additional regressions cover duplicate draft use,
creation retry/secret replay, missing governance state, chunked oversized bodies,
upstream scan caps, byte hashes and the initially paused credential reveal.

## Full repository verification: not green

The first full default run encountered missing existing test dependencies; the
locked OpenTelemetry and Milvus development dependencies were installed in the
project virtual environment. A subsequent default run produced 151 failures,
28 setup errors, 2,944 passes and 13 skips. Many failures came from the installed
third-party `pkg_resources` deprecation warning under pytest's warning-as-error
policy. With only those two warnings filtered, a complete run produced 3,088
passes, 37 failures, 1 setup error and 13 skips (31 slow/live tests deselected).
Thirty of those failures were the legacy owner-context integration and the
now-fixed scan error mapping, verified by rerunning all previous failures.
The final previous-failure recheck was **30 passed, 7 failed, 1 setup error**.

The remaining tests are outside this feature's changed business logic:

1. `tests/e2e/test_openapi_endpoint_matches_docs.py::test_dev_mode_openapi_endpoint_matches_committed_docs`
   — existing core routes and `docs/openapi.json` differ; those files were not
   changed by this feature.
2. `tests/unit/test_entrypoints/test_api/test_routes/test_get_route_validation.py::test_invalid_memory_type_value_returns_422`
   — older invalid-type expectation conflicts with the core's current type support.
3. `tests/unit/test_memory/test_get/test_dto.py::test_get_request_rejects_invalid_memory_type_value`
   — same existing core DTO expectation.
4. `tests/unit/test_entrypoints/test_memo_vault_imports.py::test_deepseek_harness_tool_traces_are_not_sent_to_memory_extraction`
   — existing import projection now supplies two user messages while this older
   test expects user/assistant/user. Import projection was not modified here.
5. `tests/unit/test_scripts/test_check_datetime_discipline.py::test_real_repo_passes_discipline_gate`
   — existing timezone conversion violations in imports/library/Pack code. The
   added gateway/auth/service files use the project clock helpers and scan clean.
6. `tests/unit/test_service/test_memo_vault_candidate_extraction.py::test_candidate_extraction_keeps_chat_under_user_and_returns_source_links`
7. `tests/unit/test_service/test_memo_vault_candidate_extraction.py::test_candidate_extraction_retries_malformed_atomic_fact_json`
   — existing extractor test doubles do not accept the current `prompt` keyword.

Setup error:
`tests/e2e/test_add_flush_user_pipeline_e2e.py::test_async_client_starts_and_health_responds[index=lancedb]`
requires LLM configuration that is absent from that test fixture. No live API key
was supplied or paid LLM request introduced for this feature.

Warning-isolated full-run command:

```bash
.venv/bin/python -m pytest -q \
  -W 'ignore:pkg_resources is deprecated as an API:DeprecationWarning' \
  -W 'ignore:Deprecated call to.*pkg_resources:DeprecationWarning'
```

Repository-wide architecture lint also retains its existing
`service.memo_vault_lifecycle -> entrypoints.api.utils` violation. That file was
not modified. Global datetime lint retains six pre-existing conversions in
imports/library/Pack. These checks were run and remain reported as failures,
not silently suppressed or claimed green.

Full dependency re-resolution was blocked by existing EverAlgo package registry
availability. The MCP closure was resolved separately with every existing shared
version constrained, then added to the frozen lock without upgrading existing
packages. The pre-existing `local-rerank` working-tree extra is not claimed to
have been fully re-locked by this feature.

## Decisions and release boundary

Work was implemented in a new branch in the current checkout because the
provided plan depends on substantial existing uncommitted work. None was reset
or silently committed. Owner controls are consolidated in one thin grants router;
grant creation policy and shared external reads live in application services.
Playwright uses the existing pytest stack rather than introducing a second UI
unit-test runner. Existing unrelated failed gates were recorded rather than
changing their business behavior to force a green result.

Gateway pause is not encryption. Remote MCP/OAuth, candidate writes, semantic
retrieval and stronger OS-process isolation remain outside this release. Actual
vendor GUI host configuration still needs its own client/version validation.
The feature is implemented and locally accepted; the entire repository is not
represented as merge-ready while its unrelated baseline gates remain red.

The original `docs/project-guide.zh-CN.md` referenced during planning is absent
from the implementation workspace. It was not recreated over a possible user
deletion. Current setup/state is documented in the new gateway guide, and
`docs/mcp.md` now explicitly distinguishes the core MCP path from the Vault
authorization path. Current OpenAPI inspection reports 47 owner operations,
7 external gateway operations, and 20 normalized core operations (38 core
method/path combinations including version aliases). These are working-tree
counts, not a claim that every unrelated prototype route is released.
