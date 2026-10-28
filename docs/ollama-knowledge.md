# Knowledge extraction using the live Ollama context

The native Ollama adapter budgets extraction and uncovered-block assignment
against the actual selected model's runtime window. It does not use the model's
theoretical maximum, a fixed 4K window, or EverAlgo's 80K batching default.

## Configuration

In the EverOS core data-root `everos.toml`:

```toml
[knowledge.extraction]
backend = "ollama"
total_timeout_seconds = 540
```

The existing `[llm]` model, API key and request timeout are reused. The native
base URL is derived by removing `/v1` from the configured LLM API root. For a
reverse proxy, an explicit native root can be provided:

```toml
[knowledge.extraction]
backend = "ollama"
ollama_base_url = "http://localhost:11434"
```

Use `backend = "auto"` (the shipped default) to select native Ollama when the
LLM API uses port 11434 or the API key placeholder `ollama`. Explicit
`ollama_base_url` also selects native Ollama in auto mode. Other endpoints
continue to use the existing EverAlgo extractor; `backend = "everalgo"` opts
out explicitly. Both document creation and replacement use this adapter.

Environment variables follow the existing nested settings convention:
`EVEROS_KNOWLEDGE__EXTRACTION__BACKEND`,
`EVEROS_KNOWLEDGE__EXTRACTION__OLLAMA_BASE_URL`, and
`EVEROS_KNOWLEDGE__EXTRACTION__TOTAL_TIMEOUT_SECONDS`.

## Following context changes

At the start of **each document extraction**, the adapter queries `/api/ps` for
the configured model and reads `context_length`. A cold model is loaded through
an empty `/api/generate` request **without a context override**, then inspected
again. Failure to obtain the actual window is an explicit error, not a fallback
to a guessed number. Missing/invalid metadata never selects another model's
window.

One extraction pins the discovered value in its native `/api/chat` options.
The next document queries again, even on the same extractor instance. If the
Ollama runtime changes to a new window, subsequent extraction budgets follow
it without editing a Knowledge window setting or restarting EverOS. Changing
an Ollama environment variable only matters once it actually changes the
model's allocated runtime window; `/api/ps` is the source of truth.

Deploying this code initially requires restarting the core API once. It does
not require restarting the Vault API or a separate reranker service.

Official runtime field: <https://docs.ollama.com/api/ps>.

## Batching and completeness

Input budgeting uses UTF-8 byte counts as a conservative upper bound for a
byte-tokenizer payload, reserving a quarter of the live window for output plus
chat framing headroom. This deliberately favors safe small batches over
assuming a tokenizer trained for another model matches Ollama. Budgets can
therefore be smaller than an exact model-tokenizer count would allow.

Source paragraphs are slices of the original text, including indentation,
blank lines and trailing whitespace. Oversized paragraphs split into adjacent
character ranges without dropping characters. JSON schemas constrain topic and
assignment responses to the provided IDs; stored content is assembled from
original slices rather than LLM-generated prose.

The adapter emits a document root plus flat topic children using the existing
KnowledgeMemory/storage contract. It does not run an unbounded cross-batch
hierarchy/summary merge. Category selection is also bounded and reduces groups
of candidates when needed. Documents using the other backend retain its
existing hierarchy extraction behavior.

Missing IDs trigger bounded assignment requests over the current unit. Invalid
JSON, non-stop completion, unknown/duplicate IDs or incomplete assignments
cause smaller-unit retries. A failing single large source block is split again.
There is a bounded identical retry for short units, followed by an explicit
error; no fabricated topic is used as a fallback. Native HTTP has no SDK retry
multiplier, and the total extraction deadline bounds all retries.

Before returning success the adapter verifies:

- Every current source block ID is assigned exactly once.
- Original character ranges are contiguous, nonoverlapping, and byte-for-byte
  equivalent at the text level (all characters and whitespace preserved).
- JSON is complete and generation has not reached its output limit.

Only then does the existing service write the Knowledge document. A failure
preserves Vault evidence and is returned as an external-service error. It does
not publish a partially extracted document. This verifies source coverage and
structure; it does not establish that every generated summary is semantically
correct.

A metadata-only `ollama_knowledge_complete` event records the actual window,
initial batch count, paragraph/block counts and covered character counts.
