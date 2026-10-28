# Dynamic Ollama Knowledge extraction implementation plan

User-approved scope: use the actual Ollama runtime context for document and
postprocessing batches, validate JSON and full source coverage, and follow
context changes without a hardcoded window.

## Design

Use an EverOS adapter rather than mutating the installed EverAlgo dependency.
Auto mode selects Ollama for the existing local Ollama configuration; an
explicit backend also supports proxies. Other endpoints keep the existing
extractor. Preserve KnowledgeMemory and existing document persistence APIs.

Resolve the selected model's live /api/ps context_length once per extraction.
If absent, preload without specifying num_ctx and read /api/ps again. Fail
clearly if no actual window can be determined; never use model theoretical
limits or a fixed window fallback. Each extraction pins the discovered window
on its native requests. Later extractions resolve again.

Budget input conservatively by UTF-8 bytes (upper bound on byte-based tokenizer
payload), reserving framing and output space derived from the discovered
window. Split oversized paragraphs without dropping characters. Use bounded
JSON-schema output and original text for topic content, not LLM paraphrases.
Perform uncovered-block assignment only within bounded extraction units.
Validate closed-set IDs, duplicate IDs, structured output and final coverage.
Invalid or incomplete units retry as smaller units, ending in an explicit
error if even a single unit cannot be processed. Never accept partial coverage.

## Implementation

- [x] Add context resolution/native bounded client and tests for runtime changes,
  cold loading, model aliases, errors and no fixed-window fallback.
- [x] Add bounded extraction/repair and tests for splitting, invalid JSON,
  truncation, coverage and original-content preservation.
- [x] Wire route/config, document activation and run focused regression coverage.
- [x] Run inference-only verification with the real Claude document; record
  actual coverage and runtime context, without resubmitting uncertain delivery.

Current source exports and other working-tree changes remain user-owned.

## Review and verification

Independent review found source preprocessing could alter whitespace and a
failed single block did not shrink. Both were reproduced by failing tests,
then fixed with raw source spans, absolute character coverage validation and
single-block recursive subdivision. Focused joint regression: 182 passed.
Static Ruff checks and git diff --check passed. No dependency files under
site-packages were modified.

Final inference-only verification after the review fixes: actual runtime context
4096, 10 initial batches, 72 lossless source paragraphs / 73 fragments, all 73
fragments covered, all 9773 source characters preserved; 52.98 seconds.
Core restarted with preserved original command/environment to activate the
adapter; health status ok. Independent-scope real HTTP create returned 201 in 63.34s; original bytes
match, detail read passed after index convergence, cleanup confirmed total zero.
Real same-instance runtime switch 4096 -> 8192 followed automatically and was
restored afterward. Full evidence: docs/testing/2026-10-24-ollama-dynamic-window.md.
