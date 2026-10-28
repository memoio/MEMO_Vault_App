# Vault authorization, API and MCP

The **Authorization & MCP** page manages delegated access to MEMO Vault. An
application can use the HTTP API directly, or an AI application can launch the
stdio MCP adapter. Both paths use the same scoped credential and policy checks.
The page follows the product prototype supplied on 2026-10-24.

## Start the local services

Keep the existing EverOS core running on port 8000. Use the same
`MEMO_VAULT_DATA_DIR` for the owner API and external gateway:

```bash
source .venv/bin/activate
export MEMO_VAULT_DATA_DIR="$PWD/.memo-vault"
uvicorn everos.entrypoints.memo_vault.app:app --host 127.0.0.1 --port 8765
```

In another terminal, with the same virtual environment and data directory:

```bash
source .venv/bin/activate
export MEMO_VAULT_DATA_DIR="$PWD/.memo-vault"
everos vault-gateway
```

The external gateway listens on `127.0.0.1:8766`. It starts paused on a new
Vault and remembers pause/resume across restarts. The owner API remains on
8765; it exposes session-protected local management, import and review routes.
The gateway never mounts those routes or the EverOS core API.

Start the existing frontend:

```bash
cd apps/memo-vault-ui
npm run dev
```

On first startup the owner API creates `owner-bootstrap.token` in the Vault
data directory, readable only by its OS owner. Enter its contents into the
local connection screen. This is the **owner credential**, not an Agent token.
A successful login creates an HTTP-only session lasting eight hours. CSRF and
origin checks protect owner mutations. Existing browser sessions must reconnect
after upgrading/restarting the owner server. Do not expose the owner listener.

Open Authorization & MCP and explicitly enable external access when ready.
If either API is already running from an older checkout, restart it to load the
new routes; restarting only the frontend is insufficient.

## Create an authorization

Select a subject label, explicit projects, operations, memory types, purpose and
expiry. Projects are exact IDs, not path prefixes or implicit descendant scopes.
The default expiry is 24 hours; allowed durations are 1-720 hours.

- **Live scope:** repeatedly reads current confirmed, active, unexpired `allow`
  memories in those projects. Future allow memories enter this scope too; the
  final review explicitly confirms that behavior. Ask and deny records are not
  returned, including their titles, snippets or counts.
- **Fixed Pack:** first select and preview content on Memory Pack, then choose
  API/MCP authorization and confirm each included ask record. This creates a
  five-minute approval draft; the authorization form checks it again and consumes
  the draft exactly once. The resulting grant admits one content delivery.

The external token is revealed once. Only its hash and a display fingerprint
are stored. Copy/save it privately; subsequent dropdown changes cannot recover
it. Rotation invalidates the previous token atomically without extending expiry.
Recreate opens a new review form; it does not revive an expired credential.
Subjects are labels: separate authorizations for one subject do not combine
permissions. A copied Bearer token confers the access of its grant.

## Direct API example

Select API invocation in the configuration card, or use this template:

```bash
curl -X POST 'http://127.0.0.1:8766/api/v1/external/context/build' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"project_ids":["personal"],"query":"Python","token_budget":4000}'
```

Replace the token privately; it is never a query-string parameter. Results have
request/receipt IDs. Context returns `vault-memory/1` JSON plus rendered content
and an approximate budget estimate. Hashes describe the exact rendered artifact,
not arbitrary JSON reserialization or the containing response envelope.

| Operation | Method and path |
| --- | --- |
| Authorized capability discovery | `GET /api/v1/external/capabilities` |
| Keyword search | `POST /api/v1/external/memories/search` |
| Read a scoped memory | `POST /api/v1/external/memories/get` |
| Read a permitted evidence excerpt | `POST /api/v1/external/evidence/get` |
| Build context | `POST /api/v1/external/context/build` |
| Inspect approved Pack | `GET /api/v1/external/packs/{pack_id}` |
| Deliver approved Pack once | `POST /api/v1/external/packs/{pack_id}/deliver` |

Use `/openapi.json` on the external listener for strict request schemas. Unknown
identity fields are rejected. The owner control API has a separate OpenAPI
schema and is not a delegated export surface.

Search initially uses case-insensitive keyword substring matching, newest first,
not semantic retrieval. Results are bounded and cursors bind the query, grant and
permitted record versions; changed results require a new search. Current limits:
5,000 scanned canonical rows, 50 search results/page, 1 MiB response, 64 KiB
request, 30-second execution, two in-flight calls/grant, and a default of 30
calls/minute. Cursor expiry is five minutes. Oversized work is rejected rather
than described as complete. Context uses up to 50 matching records with whole
items omitted when they exceed budget; memory text is never cut mid-item.

Evidence is disabled by default. IDs need explicit permission; `get_evidence`
requires an additional excerpt permission and returns at most 360 Unicode
characters of a matching excerpt with a valid archive. It never returns the
whole original conversation. External fixed Packs support no evidence or IDs;
existing local copy/download excerpt options remain available.

## Connect an MCP application

Install the optional SDK in the environment running the adapter:

```bash
pip install -e '.[mcp]'
```

The adapter is tested with MCP Python SDK 1.30.0; its optional dependency is
`mcp>=1.30.0,<2`. Ordinary EverOS installations do not need this SDK. The
`everos vault-mcp` command reports installation instructions when it is missing.

Copy the MCP configuration template from the page. Example for JSON-configured
hosts:

```json
{
  "mcpServers": {
    "memo-vault": {
      "command": "everos",
      "args": ["vault-mcp", "--gateway", "http://127.0.0.1:8766"],
      "env": { "MEMO_VAULT_TOKEN": "<YOUR_TOKEN>" }
    }
  }
}
```

Replace `everos` with the absolute executable path if the host has a different
PATH. Replace the placeholder with the granted token, or use the host's supported
secret/environment mechanism. A literal secret in a local config makes that
config a secret file; never commit or share it. The gateway must be reachable
from the machine running this subprocess. Configuration formats vary by host.

Live tools are `search_memory`, `get_memory`, `build_context`, plus separately
permitted `get_evidence`. Fixed grants expose `get_pack_info` and
`read_memory_pack`. The model does not receive the credential as a tool argument
and cannot create grants or approve ask records. Tool discovery is scoped, and
every invocation rechecks permissions even if a host caches its tool list.

The browser's connection test exercises the real HTTP gateway with an external
token. It does **not** establish an MCP connection to your AI application. In
the actual host, verify process launch, tool discovery and one permitted call.
Automated acceptance covers the real SDK stdio client, initialization, tool
listing, invocation and denial after revocation. No vendor GUI host compatibility
is claimed without testing that installed host/version.

## Revocation, pause and receipts

Revoke blocks both API and MCP. Pause blocks all external grant operations,
including capability discovery. A memory-backend outage fails closed with a
sanitized denial; owner diagnostics retain upstream reachability information.
Requests admitted before a pause/revocation commits may finish. Content already
returned cannot be recalled.

A fixed Pack becomes stale when an approved member's body/version/policy or
rendered evidence changes. It is rejected as a whole; the gateway does not
silently replace or omit it. Unrelated memories do not invalidate the approval.
A successful delivery reserves its use and writes a metadata-only audit before
returning bytes. If the connection subsequently fails, it can still be consumed;
this is admission, not proof of client receipt. Do not retry single-use delivery
automatically. Its non-consuming metadata check is a separate operation.

The owner can inspect bounded audit records. They contain operations, timestamps,
outcomes and permitted IDs/revisions/hashes; no query text, memory/evidence body
or token. Default audit retention is 30 days.

## Configuration and boundaries

- `MEMO_VAULT_DATA_DIR`: shared local metadata directory.
- `MEMO_VAULT_EVEROS_URL`: private EverOS core URL.
- `MEMO_VAULT_GATEWAY_URL`: external URL displayed in templates, default 8766.
- `MEMO_VAULT_AUDIT_RETENTION_DAYS`: audit cleanup window, 1-365 days, default 30.
- `MEMO_VAULT_UI_ORIGINS`: comma-separated exact trusted browser origins. Defaults
  include local 5173 UI and 8765 owner API. Update it for different frontend ports.
- `MEMO_VAULT_UI_API_URL`: optional Vite development proxy target, default 8765.

Run ordinary unit tests plus the focused gateway modules. Optional synthetic
end-to-end tests require Playwright Chromium, MCP SDK and UI node_modules:

```bash
pytest tests/e2e/test_memo_vault_authorization.py -m slow -q
```

This release does not implement encrypted Vault locking, candidate writes,
remote HTTP MCP/OAuth or cloud delivery. Gateway pause is access control, not
storage encryption. The existing separate core `everos-mcp` integration is a
different surface and does not inherit these grants. A same-user OS process can
already read local files/core APIs; scoped grants are not an OS sandbox.
