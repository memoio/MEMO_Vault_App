# Claude long-document Knowledge diagnosis

Date: 2026-10-24. This is a follow-up to
`2026-10-24-claude-local-rerank-live.md`. Experiments use the actual 9,773-character
Claude project document and installed algorithm, without writing Knowledge or
publishing memories. Raw prompts/responses remain private under
`/tmp/claude-knowledge-diagnosis/`.

## Storage and product surfaces

The committed Vault batch retains all four original ZIPs and the manifest under
`.memo-vault/evidence/813e40999ada4839b9e1101bdd006be3/source/`.
SHA-256 checks confirm each archived ZIP is byte-identical to the user-supplied
ZIP. The sibling `umif/` holds normalized records and document blobs. For Claude
project documents supplied as text in a JSON export, the raw source is the
original JSON inside the ZIP; an unavailable attachment cannot be reconstructed.
Successful Knowledge delivery also retains its submitted file under `_original`.
A Knowledge failure does not delete Vault evidence.

The Vault UI has a confirmed-memory content substring filter, implemented by
`MemoVaultLibraryService.list_memories`; it does not call Knowledge search.
Claude UI exposes document delivery, not a Knowledge question-answering view.
Current Memory Pack preview/export loads confirmed memory sources; it does not
retrieve or rerank Knowledge. The earlier live Knowledge query was a direct
core `/api/v2/knowledge/search` verification, separate from current Pack exports.

## Reproduction method

The same installed `KnowledgeExtractor` and real `qwen2.5:3b` endpoint were used
with the same taxonomy and document. A tracing wrapper records each LLM call's
stage, usage and finish reason. SDK retries were disabled only in this temporary
tracer and each call was bounded to 45 seconds to avoid repeating a nine-minute
wait. This changes diagnostic retry/deadline behavior, not prompt contents.

Current production settings remain unchanged. All additional requests were
inference-only; the unresolved delivery was not resubmitted.

## What the earlier failure actually did

The original Ollama logs show three requests starting with 3,316 input tokens.
Each request produced five `slot context shift` events before ending after
180 seconds. The configured client has a 180-second timeout and the SDK default
`max_retries=2`, so one stalled stage generates three attempts.

This is evidence of sustained generation during the failure, rather than an
unreachable server. The original response text was not captured, so repeating
specific text or exact generated JSON cannot be established retrospectively.

## Exact problematic stage and budget mismatch

The real document becomes 94 atoms. The installed Knowledge splitter defaults
to **80,000 tokens per batch**, explicitly designed for a 128K-context model.
The active Ollama model runtime is **4,096 tokens**, and that capacity is not
passed to Knowledge's splitter. The document therefore becomes one batch.

In the traced replay:

| Stage | Observation |
| --- | --- |
| `_aextract_batch` | 16,606 prompt characters; only 2,050 prompt tokens consumed; 23.92 s, stop |
| `aclassify_category` | 603 prompt tokens; 0.74 s, stop |
| `aassign_uncovered_blocks` | 3,316 prompt tokens; 17.76 s, stop |

The first extraction covers 18 of 94 atoms. The application still holds all
94 original atoms, so it asks the postprocessor to assign 76 uncovered blocks.
The postprocessor returned assignments for only 23 of them. The extractor
returned seven memories anyway. Thus a successful request does not establish
complete paragraph coverage.

The 3,316-token postprocessing request matches the input length in all three
original failed attempts. This locates the previously stalled stage, while
showing that the timeout is not reproduced on every run. A second uncapped
streamed request to that same postprocessing prompt also finished in 20.19 s,
returning assignments for 26 distinct block IDs.

## Controlled experiments

1. Capping the original postprocessing request at 1,024 output tokens returns
   `finish_reason=length` and invalid JSON after 18.33 s (24 block IDs emitted).
2. The rendered postprocessing prompt contains invalid doubled JSON braces.
   Correcting only that example still reaches 1,024 tokens and invalid JSON
   after 18.30 s (22 IDs). It is a genuine prompt defect, but correcting it alone
   does not solve the output budget/completeness problem.
3. A temporary native Ollama request with `num_ctx=8192`, `num_predict=2048`
   consumes the entire 5,987-token extraction prompt and returns valid JSON in
   22.20 s, covering 92 of 94 atoms. This demonstrates that input truncation can
   be avoided; it does not guarantee stable complete extraction.
4. A fresh full algorithm run with the same temporary limits consumes all 5,987
   input tokens, but its first extraction leaves 63 atoms uncovered. The
   postprocessing stage hits the 2,048-token output limit in 34.56 s; invalid
   JSON is skipped by the current parser. The algorithm still returns eight
   memories, with **63 atoms uncovered**. Increasing context alone and simply
   adding an output cap are therefore insufficient acceptance criteria.

The temporary 8K runtime was restored to 4K by an explicit native request at the
end. No persistent TOML was changed.

## Required correction

- Budget document batches using actual model context, prompt overhead and output
  headroom, not the current algorithm's 80K default.
- Apply bounds to postprocessing inputs and outputs too; do not expand every
  uncovered atom into a single unbounded assignment request.
- Treat length-truncated/invalid JSON as an explicit incomplete result; retry a
  smaller unit rather than accepting partial topic coverage as complete.
- Validate coverage of all original atom IDs, with explicit exclusions only.
- Correct postprocessing JSON examples and bound stage retries and total time.

The repeated 180-second timeout was not reproduced in this diagnostic run.
What is confirmed is the failed stage's sustained generation, the budget
mismatch, incomplete coverage after input truncation, and failure to reject
incomplete structured results. Model response variability remains a factor.
