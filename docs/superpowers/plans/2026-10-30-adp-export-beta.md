# ADP Export Beta Implementation Plan

**Goal:** Export archived agent sessions as redacted, validated paper-style ADP JSONL.

**Architecture:** Discover sessions from the agent track, convert DeepSeek archived source using an in-repository converter, and support Claude only where its archived events can be reliably mapped. Validate and export the same redacted representation; preserve per-session training consent.

**Tech Stack:** Python, FastAPI, SQLite, React, TypeScript, JSON Schema.

**Constraints:** One complete session per trajectory; no invented calls or results; no runtime dependency on `/root/deepseek_trans`; no ATIF in this release; preserve existing imports and user data.

1. Add converter and schema under EverOS, with tests against DeepSeek source and malformed call/result pairs.
2. Add session discovery, consent, redaction preview, validation, export and audit endpoints, with API tests including stale and missing sources.
3. Build the ADP export page with session selection, consent, preview, validation and JSONL download.
4. Verify DeepSeek and Claude imported trajectories, existing import regression tests, and frontend build; document unsupported Claude data explicitly.
