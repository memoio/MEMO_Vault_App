# Claude import with embedded rerank — live verification

Date: 2026-10-24 (Asia/Shanghai). Requests used real local HTTP services and the
user-supplied Claude ZIPs. Signed URLs and source content are excluded here.

## Services and source integrity

- Core `:8000/health`: HTTP 200; LLM, embedding and rerank capabilities true.
- Vault `:8765/api/v1/system/health`: HTTP 200; core online.
- Four original ZIPs pass CRC verification.
- Archived batch `813e40999ada4839b9e1101bdd006be3`: UMIF validation passes,
  19 records, with two pending candidates and no additional publication.

## Full local ZIP replay

For a fresh HTTP parsing run, a private temporary ZIP combined the original
entries from all four export ZIPs without changing entry contents. This uses
the supported direct-ZIP route; it does not retest remote Claude acquisition.
The previous remote acquisition attempt hit a Claude region restriction; this
run does not establish whether the remote response has changed.

- `POST /api/v1/import-jobs`: HTTP 202, job
  `37cefa7b5650462e8927ac46279d0293`.
- Job reaches `ready_for_preview`, with no error code.
- Preview: HTTP 200; 19 UMIF records, one conversation, four messages,
  two tool calls and two tool results.
- All four messages recognized as duplicates of the original batch.
- Commit: HTTP 409, `IMPORT_DUPLICATE_FILE`.
- Total Vault batch count stays five. The two candidates stay pending.
- Existing batch extraction reentry: HTTP 409,
  `EXTRACTION_ALREADY_COMPLETED`; no duplicate extraction performed.

## Fresh candidate extraction

The four archived user/assistant messages were sent through the real core
`POST /api/v2/memo-vault/candidates/extract` endpoint after the long-document
request completed. It returned HTTP 200 in 10.49 seconds, `status=extracted`,
`method=local`, and one candidate. This read-and-infer endpoint returns drafts;
it does not publish memories or insert duplicate Vault candidates.

## Embedded model and live retrieval

The core process maps `/root/models/bge-reranker-base/model.safetensors`.
The configured model is actually loaded, beyond the capability presence check.

The archived short project instruction (99 characters) delivered through the
Vault endpoint with `confirmed=true`: HTTP 200, `delivered`, Knowledge document
`d_10bc70f1e8a2`. The core list confirms one topic.

Two actual hybrid Knowledge searches returned HTTP 200, the delivered document
and topic content. First call took 17.54 seconds; repeat took 1.04 seconds.
These totals include embedding, retrieval and reranking. Search's final score
is a pipeline score, not the raw CrossEncoder relevance score.

A separate native call using the actual configured provider and weights scored
two fixed Chinese passages for a Claude import query:

| Passage | Raw relevance score |
| --- | --- |
| Relevant import instructions | 0.9946364760 |
| Unrelated weather sentence | 0.0000374125 |

Both runs returned both original indices in correct descending order. First
call including imports/model load took 14.561 seconds; repeat took 0.111 seconds.
Empty input returned an empty result.

## Long document observation

The second archived project document has 9,773 characters. The test client
reached its own 300-second wait limit; the Vault-to-core request continued.
No retry delivery was issued while its outcome was uncertain.

Ollama logs show a prompt of 5,987 tokens truncated to 2,050 tokens at 17:50:08,
followed by LLM calls returning HTTP 500 after 180 seconds at 17:53:21 and
17:56:21. Ollama reports a 4,096-token runtime context window for qwen2.5:3b.
This establishes actual truncation and repeated LLM timeout behavior during the
long-document request. It does not by itself prove truncation caused the timeout
or identify the exact internal postprocessing substage.

The final LLM retry also returned HTTP 500 after 180 seconds at 17:59:22.
Vault subsequently recorded `delivery_unknown`; reconciliation returned HTTP
200 with `not_found`. There remains one delivered document and one uncertain
long-document delivery. No second delivery was issued for the long document.

Repeating delivery for the already-delivered short document returned HTTP 200
with the same document ID; the remote document count stayed one.

## Regression coverage

Command:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_component/test_rerank \
  tests/unit/test_entrypoints/test_claude_import.py -q
```

Result: **91 passed**. Includes nine new local provider cases for local settings
without HTTP credentials, missing model/dependencies, complete sorted scores,
model reuse, empty input, invalid scores, cancellation and timeout accounting.
Ruff checks on the new tests pass. Full unit suite and browser UI were not run.
Private raw HTTP responses are retained in `/tmp/claude-rerank-live-results/`;
regression output is `/tmp/claude-rerank-regression.log`.
