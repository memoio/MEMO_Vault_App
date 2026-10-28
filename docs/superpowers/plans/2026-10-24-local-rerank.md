# Embedded reranker implementation

User-approved scope: implement steps 3 and 4 of the local CrossEncoder proposal,
in the current workspace, using downloaded BAAI/bge-reranker-base weights.

1. Extend RerankSettings and factory with local provider and device/max_length.
2. Add lazy offline CrossEncoder loading, thread-based async inference, bounded
   native concurrency, deadline handling and complete sorted results.
3. Declare optional dependencies, document operation, and replace the active
   .everos-ollama rerank configuration with CPU/local settings.
4. Complete missing model metadata downloads if needed.

The existing process-wide accessor owns the model. Local mode does not require
HTTP credentials. No core API process restart or Knowledge delivery is performed
by this implementation task. Tests were not requested and are not run.

## Implementation record

- Steps 1–4 implemented; missing JSON/tokenizer files downloaded successfully.
- Corrected the download CLI to repeat `--include` for each pattern.
- Ruff lint and formatting checks passed; `git diff --check` passed.
- No automated tests or actual inference run; core API remains running with its
  previous in-memory configuration until the user restarts it.

- Independent static review identified queued work surviving cancellation; added
  a per-request thread event checked before loading and before each batch, so
  cancelled queued work never starts new inference. No other important findings.
