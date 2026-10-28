# Dynamic Ollama Knowledge window verification

Date: 2026-10-24. Implementation is documented in `docs/ollama-knowledge.md`.

## Regression and review

Focused joint regression: **182 passed in 7.18 seconds**, covering the new
Ollama adapter, Knowledge configuration/create/CRUD/routes, Claude imports and
all rerank adapters. Ruff lint/format and `git diff --check` pass.

Independent review identified source whitespace normalization and failure to
shrink a single large block. Both were reproduced by failing tests and fixed.
New assertions verify indentation, blank lines, trailing spaces, adjacent
character ranges, unique IDs, and recursive subdivision. Additional tests
cover invalid HTTP JSON and reusing one extractor across runtime changes.

## Actual runtime changes

The same extractor instance processed a short fixed Chinese text twice:

| Live Ollama context | Adapter observed context | Source coverage |
| --- | --- | --- |
| 4096 | 4096 | 20 / 20 characters |
| 8192 | 8192 | 20 / 20 characters |

The runtime was changed transiently through native model options, without
changing EverOS settings or restarting it between extractions. The test restored
the original 4096 runtime window afterward. This is a real Ollama experiment,
not only a mocked metadata test. The production code contains no 4096/8192/80K
window fallback.

## Real Claude long document

Inference-only test of the actual 9,773-character document:

- Actual context: 4096, dynamically resolved.
- Raw paragraphs: 72 (including original separators/indentation).
- Source fragments: 73; every fragment covered exactly once.
- Source characters: 9773; covered characters: 9773.
- Initial batches: 10; elapsed: 52.98 seconds.
- Generated Knowledge memories: 17, including the document root.

These raw paragraph counts differ from the old algorithm's 94 normalized atoms
because the new path slices the original string and retains all whitespace.

## Real core HTTP activation

Core was gracefully restarted with its original command/environment, preserving
`.everos-ollama` as the data root. Health returned `ok`, with LLM/embedding/rerank
capabilities true. Vault and Ollama were not restarted.

The same real long document was submitted to `/api/v2/knowledge/documents` in
an isolated project `knowledge-budget-test-20261024`, not resubmitted to the
previous uncertain Vault delivery.

Final live creation returned **HTTP 201 in 63.34 seconds**, with 21 topic nodes.
The stored `_original/claude-budget-live.txt` matched the submitted UTF-8 bytes
exactly. Detail read returned HTTP 200 after two index polling attempts. Model
outputs can differ across runs; source coverage must still be complete.

Document creation writes Markdown before asynchronous indexing. An initial
verification's immediate detail read returned 404 and its finally block cleaned
up the document. The next verification waited for indexing successfully. Its
immediate post-delete list still showed one stale row; a subsequent real list
confirmed total zero. The reusable temporary script now polls both creation
and deletion index convergence. No production index behavior was changed.

Both temporary documents were deleted (HTTP 200/204); the isolated scope's
final document count is zero. Existing Vault batch count remains five, the two
import candidates remain pending, and the original long-document delivery stays
`delivery_unknown`. No formal memories were published by these tests.

Private artifacts:

- `/tmp/claude-knowledge-dynamic-result.json`
- `/tmp/ollama-knowledge-real-window-change.json`
- `/tmp/claude-knowledge-dynamic-http/`
- `/tmp/ollama-knowledge-final-regression.log`
- `/tmp/ollama-knowledge-core-restart.log`
