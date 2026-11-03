# Vault remote MCP implementation plan

**Goal:** Expose grant-scoped Vault tools over Streamable HTTP with a URL and individual Bearer credential.

**Architecture:** Run a separate loopback MCP service using the existing SDK and Vault gateway. Validate every HTTP request via gateway capabilities and propagate its credential in request-local context; the gateway continues checking every operation. Deploy behind a TLS proxy on /mcp. Keep stdio available.

**Checks:** Real gateway integration must reject missing, invalid and revoked credentials; discover and call only authorized tools; concurrent requests must not share credentials. Verify an MCP SDK client, existing gateway tests and frontend build. Document proxy and version/client limitations.
