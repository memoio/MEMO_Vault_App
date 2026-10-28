# Claude live acquisition verification — 2026-10-24

## Input and running services

The updated manifest is under `docs/memo_input/Claude/`, with creation timestamp
`2026-10-24T03:57:55.466746+00:00` (11:57:55 Asia/Shanghai). It declares four ZIP
parts: light metadata, projects, memories and conversations. The first live API
submission was recorded at `2026-10-24 04:02:55` UTC, about five minutes later.

- EverOS `127.0.0.1:8000/health`: HTTP 200, `status=ok`.
- Vault `127.0.0.1:8765/api/v1/system/health`: HTTP 200; EverOS online.
- Frontend `127.0.0.1:5173`: HTTP 200.
- Ollama `127.0.0.1:11434/api/tags`: HTTP 200; configured local models listed.
  This check verifies reachability, not inference quality.

## Initial acquisition outcome

The manifest was submitted through `POST /api/v1/import-jobs` on the running
Vault API. It returned HTTP 202 and job ID
`a7f7d5f617444115a20a736081cd0d11`.

Every part failed before ZIP parsing. Direct, address-pinned HTTPS requests
received HTTP 302 from `claude.ai`, redirecting to:

```
https://claude.com/app-unavailable-in-region
```

Following that redirect produced an HTML page, not a ZIP. A separate comparison
using the environment-configured HTTP proxy returned HTTP 403. The evidence
establishes that Claude returns a region-unavailable route for this server's
direct egress; it does not establish a specific physical location for the server
or prove what happens in the user's browser.

The fresh manifest is within its stated validity window, so elapsed 24-hour
expiry does not explain this attempt. Restarting the local inference and API
services cannot change the observed remote response.

## Diagnostic correction and repeat verification

The downloader now recognizes the exact trusted Claude region-unavailable
redirect before fetching the HTML page and returns `CLAUDE_REGION_UNAVAILABLE`.
The job manager records the error separately for every part and still attempts
the remaining parts. The signed source URLs and their tokens are not logged.

After running the updated worker against the same durable job, querying the
running Vault API confirms:

| Part | State | Error |
|---|---|---|
| light_metadata-000.zip | failed | CLAUDE_REGION_UNAVAILABLE |
| projects-000.zip | failed | CLAUDE_REGION_UNAVAILABLE |
| memories-000.zip | failed | CLAUDE_REGION_UNAVAILABLE |
| conversations-000.zip | failed | CLAUDE_REGION_UNAVAILABLE |

No package was committed, no candidates were extracted, and no formal memories
were published by this live attempt. At that acquisition-only stage, real source-schema, UMIF conversion and
Ollama extraction acceptance were still unverified. The local ZIP continuation
below records the later successful validation. The existing missing-part upload endpoint can accept locally
obtained ZIPs using their original filenames.

The downloader diagnostic change requires the Vault worker to reload before
new API submissions use it; the tested durable job already contains the updated
per-part result.


## Local ZIP continuation

The user supplied all four original ZIPs in `docs/memo_input/Claude/`.
Their ZIP CRC checks passed. Inspection found the actual directory-oriented
source schema: `projects/<id>.json` stores a single project, and
`memories/<account>.json` contains `account_uuid` and `memory_files`.
The adapter now handles these source records and their exact JSON pointers.
Account login history is preserved only in original evidence, not projected
into normalized assets or memory inputs. Adapter version is `claude-export-2`.

The local export contains one conversation with four messages, one project
with instructions and one knowledge document, and one saved memory file.
Two explicit tool calls and two matching results are archived. Three thinking
blocks and three injected prompt blocks are excluded from memory inputs.
One conversation document/file reference has no corresponding file in the
export; it is reported as unresolved, rather than fabricated from other data.


## Local ZIP HTTP results

After loading adapter `claude-export-2`, all four files were supplied through
`POST /api/v1/import-jobs/{id}/parts` to the same original job. Each upload
returned HTTP 202; the final state became `ready_for_preview`. Preview and
commit both returned HTTP 200. The committed batch is
`813e40999ada4839b9e1101bdd006be3`, under project `personal`.

| Verification | Result |
|---|---|
| Source ZIPs | Four CRC checks and archived source hashes verified |
| Conversations/messages | 1 / 4, matching source coverage |
| Tool events | 2 calls + 2 results archived |
| Project documents | 2: instructions and knowledge text |
| Saved memory files | 1 assertion, original path and timestamp preserved |
| UMIF | 19 records; validation passed before and after archival |
| Local conversation extraction | HTTP 200, `extracted`, 1 candidate |
| Saved memory review | HTTP 200, 1 additional pending candidate |
| Formal memory publication | None; both candidates remain pending review |
| Duplicate preview | Existing batch identified, all four messages duplicate |
| Duplicate commit | `IMPORT_DUPLICATE_FILE`; no additional batch created |
| Knowledge delivery | Core HTTP 422: rerank provider not configured |

A preview fingerprint defect was reproduced and fixed: manifest dictionary key
order changed after canonical package serialization. Fingerprints now use
canonical serialization, while previously stored valid fingerprints remain
accepted. Regression tests cover direct ZIP preview/commit and source tampering.

The Knowledge diagnostic also exposed an exception hierarchy bug:
`ImportWorkflowError` inherits `ValueError`, so a definitive provider rejection
was incorrectly caught as an uncertain response. The handler now re-raises
workflow errors explicitly. The two affected live document states were
reclassified after confirming the synchronous rerank capability rejection and
an empty remote project document list; an audit event records this correction.
Repeating the actual requests now returns `EVEROS_RERANK_NOT_CONFIGURED` and
leaves both documents `delivery_rejected`, allowing a later configured retry.
No provider configuration was changed and no knowledge documents were written.

An independent review identified a project-only archive namespace risk. Such
archives must include verified account context; `creator.uuid` is not used as
export-owner identity because projects can be shared. This prevents inconsistent
IDs between standalone and full exports.
