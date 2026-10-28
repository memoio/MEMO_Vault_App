"""Shared external Vault use cases, visibility policy and final admission."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from everos.component.utils.datetime import from_iso_format, get_utc_now
from everos.infra.memo_vault_gateway import (
    GatewayRepository,
    RepositoryDeniedError,
    RepositoryLimitedError,
)
from everos.service.memo_vault_export_contracts import GatewayError, SourceMemory

SourceLoader = Callable[[list[str]], Awaitable[list[SourceMemory]]]
Renderer = Callable[
    [list[SourceMemory], dict[str, Any], str], tuple[str, int, list[str]]
]


class GatewayService:
    def __init__(
        self,
        repository: GatewayRepository,
        load_sources: SourceLoader,
        render: Renderer,
    ) -> None:
        self.repository = repository
        self.load_sources = load_sources
        self.render = render

    def capabilities(self, token: str) -> dict[str, Any]:
        try:
            grant = self.repository.authorize(token)
        except RepositoryDeniedError as exc:
            raise GatewayError() from exc
        return self._capability_data(grant)

    @staticmethod
    def _capability_data(grant: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: grant[key]
            for key in (
                "id",
                "subject",
                "mode",
                "project_ids",
                "kinds",
                "operations",
                "expires_at",
                "pack_id",
                "consumed",
                "evidence_mode",
                "token_budget",
            )
        }
        if result["consumed"]:
            result["operations"] = [
                op for op in result["operations"] if op != "read_memory_pack"
            ]
        return result

    def _deny(self, request_id: str, reason: str) -> None:
        with suppress(sqlite3.Error):
            self.repository.deny(request_id, reason)

    async def discover(
        self,
        token: str,
        healthy: Callable[[], Awaitable[bool]],
        *,
        transport: str = "api",
    ) -> dict[str, Any]:
        try:
            grant, request_id = self.repository.begin(token, "capabilities", transport)
        except RepositoryLimitedError as exc:
            raise GatewayError("RATE_LIMITED", 429) from exc
        except (RepositoryDeniedError, sqlite3.Error) as exc:
            raise GatewayError() from exc
        try:
            async with asyncio.timeout(3):
                if not await healthy():
                    raise GatewayError()
            self.repository.admit(
                token, grant, request_id, [], {"capability_discovery": True}
            )
            return {
                "request_id": request_id,
                "receipt_id": request_id,
                "data": self._capability_data(grant),
            }
        except Exception as exc:
            self._deny(request_id, "ACCESS_DENIED")
            raise GatewayError() from exc

    def manifest(
        self, sources: list[SourceMemory], config: dict[str, Any], generated_at: str
    ) -> dict[str, Any]:
        if not sources:
            raise GatewayError("EMPTY_PACK", 422)
        rendered, estimate, included_ids = self.render(sources, config, generated_at)
        if set(included_ids) != {s.memory_id for s in sources}:
            raise GatewayError("PACK_BUDGET_CHANGED", 409)
        if len(rendered.encode()) > 1024 * 1024:
            raise GatewayError("RESPONSE_TOO_LARGE", 413)
        return {
            "version": 1,
            "generated_at": generated_at,
            "config": config,
            "members": {s.memory_id: s.fingerprint() for s in sources},
            "content_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "estimated_tokens": estimate,
        }

    @staticmethod
    def _eligible(source: SourceMemory) -> bool:
        return (
            source.project_id == source.namespace
            and source.lifecycle_status == "active"
            and source.expiry_status == "valid"
            and source.index_available
            and bool((source.content or "").strip())
            and (
                not source.valid_until
                or from_iso_format(source.valid_until) > get_utc_now()
            )
        )

    async def call(
        self,
        token: str,
        operation: str,
        body: dict[str, Any],
        *,
        transport: str = "api",
    ) -> dict[str, Any]:
        try:
            grant, request_id = self.repository.begin(token, operation, transport)
        except RepositoryLimitedError as exc:
            raise GatewayError("RATE_LIMITED", 429) from exc
        except (RepositoryDeniedError, sqlite3.Error) as exc:
            raise GatewayError(status_code=401 if not token else 403) from exc
        try:
            async with asyncio.timeout(30):
                result, used, raw_content = await self._execute(grant, operation, body)
            if len(json.dumps(result, ensure_ascii=False).encode()) > 1024 * 1024:
                raise GatewayError("RESPONSE_TOO_LARGE", 413)
            metadata = {
                "revisions": {s.memory_id: s.revision for s in used},
                "content_sha256": hashlib.sha256(
                    (raw_content or json.dumps(result, sort_keys=True)).encode()
                ).hexdigest(),
            }
            self.repository.admit(
                token, grant, request_id, [s.model_dump() for s in used], metadata
            )
            response = {
                "request_id": request_id,
                "receipt_id": request_id,
                "data": result,
            }
            if raw_content is not None:
                response["artifact"] = raw_content
                response["content_sha256"] = metadata["content_sha256"]
            return response
        except GatewayError as exc:
            if exc.code == "PACK_APPROVAL_STALE":
                self.repository.mark_stale(grant["id"])
            self._deny(request_id, exc.code)
            raise
        except (RepositoryDeniedError, TimeoutError) as exc:
            self._deny(request_id, "ACCESS_DENIED")
            raise GatewayError() from exc
        except Exception as exc:
            self._deny(request_id, "BACKEND_UNAVAILABLE")
            raise GatewayError() from exc

    async def _execute(
        self, grant: dict[str, Any], operation: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], list[SourceMemory], str | None]:
        scope = body.get("project_ids") or grant["project_ids"]
        kinds = body.get("kinds") or grant["kinds"]
        if not set(scope) <= set(grant["project_ids"]) or not set(kinds) <= set(
            grant["kinds"]
        ):
            raise GatewayError()
        if grant["mode"] == "approved_pack" and body.get("pack_id") != grant["pack_id"]:
            raise GatewayError()
        sources = await self.load_sources(scope)
        sources = [
            s
            for s in sources
            if s.namespace in scope
            and s.project_id == s.namespace
            and self._gateway_kind(s) in kinds
        ]
        if len(sources) > 5000 or len({s.memory_id for s in sources}) != len(sources):
            raise GatewayError("SCAN_LIMIT", 413)
        if grant["mode"] == "approved_pack":
            return self._fixed(grant, operation, sources)
        visible = [
            s for s in sources if self._eligible(s) and s.external_use == "allow"
        ]
        visible.sort(
            key=lambda s: (s.timestamp or s.confirmed_at, s.memory_id), reverse=True
        )
        if operation in {"get_memory", "get_evidence"}:
            source = next(
                (s for s in visible if s.memory_id == body.get("memory_id")), None
            )
            if source is None:
                raise GatewayError()
            if operation == "get_memory":
                return self._item(source), [source], None
            if grant["evidence_mode"] != "snippet":
                raise GatewayError()
            evidence = next(
                (
                    e
                    for e in source.evidence
                    if e.evidence_id == body.get("evidence_id")
                    and e.available
                    and e.matched
                ),
                None,
            )
            if (
                evidence is None
                or not evidence.quote
                or hashlib.sha256(evidence.quote.encode()).hexdigest()
                != evidence.quote_sha256
            ):
                raise GatewayError()
            return (
                {
                    "memory_id": source.memory_id,
                    "evidence_id": evidence.evidence_id,
                    "quote": evidence.quote[:360],
                },
                [source],
                None,
            )
        query = body.get("query", "").strip().casefold()
        if not query:
            raise GatewayError("QUERY_REQUIRED", 422)
        matches = [s for s in visible if query in (s.content or "").casefold()]
        if operation == "search_memory":
            start = self._read_cursor(body.get("cursor"), grant, body, matches)
            limit = body.get("limit", 10)
            selected = matches[start : start + limit]
            cursor = (
                self._cursor(grant, body, matches, start + limit)
                if start + limit < len(matches)
                else None
            )
            return (
                {
                    "memories": [self._item(s, snippet=True) for s in selected],
                    "next_cursor": cursor,
                    "search_mode": "keyword",
                },
                selected,
                None,
            )
        if operation != "build_context":
            raise GatewayError()
        budget = body.get("token_budget", min(4000, grant["token_budget"]))
        evidence_mode = body.get("evidence_mode", "none")
        if (
            budget > grant["token_budget"]
            or evidence_mode not in {"none", "id"}
            or (evidence_mode == "id" and grant["evidence_mode"] == "none")
        ):
            raise GatewayError()
        config = {
            "format": body.get("format", "vault-memory/1"),
            "token_budget": budget,
            "evidence_mode": evidence_mode,
        }
        rendered, estimate, included_ids = self.render(
            matches[:50], config, get_utc_now().isoformat()
        )
        used = [s for s in matches[:50] if s.memory_id in included_ids]
        pack = json.loads(rendered) if config["format"] == "vault-memory/1" else None
        return (
            {
                "pack": pack,
                "included_ids": included_ids,
                "rendered_content": rendered,
                "estimated_tokens": estimate,
                "estimate_is_approximate": True,
                "content_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            },
            used,
            None,
        )

    @staticmethod
    def _gateway_kind(source: SourceMemory) -> str:
        # Older Vault records label profiles as preferences. The gateway's
        # public authorization contract uses "profile" for both forms.
        if source.memory_type == "profile" or source.kind == "preference":
            return "profile"
        return source.kind

    @staticmethod
    def _item(source: SourceMemory, *, snippet: bool = False) -> dict[str, Any]:
        return {
            "memory_id": source.memory_id,
            "namespace": source.namespace,
            "kind": GatewayService._gateway_kind(source),
            "revision": source.revision,
            "text": (source.content or "")[:240] if snippet else source.content,
            "training_consent": False,
        }

    def validate_manifest(
        self, manifest: dict[str, Any], sources: list[SourceMemory]
    ) -> None:
        self._fixed(
            {"manifest": manifest, "pack_id": "approval", "consumed": False},
            "get_pack_info",
            sources,
        )

    def _fixed(
        self, grant: dict[str, Any], operation: str, sources: list[SourceMemory]
    ) -> tuple[dict[str, Any], list[SourceMemory], str | None]:
        manifest = grant["manifest"]
        if not manifest or manifest["version"] != 1:
            raise GatewayError("PACK_APPROVAL_STALE", 409)
        members = manifest["members"]
        selected = [s for s in sources if s.memory_id in members]
        if len(selected) != len(members) or any(
            not self._eligible(s)
            or s.external_use == "deny"
            or s.fingerprint() != members[s.memory_id]
            for s in selected
        ):
            raise GatewayError("PACK_APPROVAL_STALE", 409)
        rendered, estimate, included_ids = self.render(
            selected, manifest["config"], manifest["generated_at"]
        )
        if (
            set(included_ids) != set(members)
            or hashlib.sha256(rendered.encode()).hexdigest()
            != manifest["content_sha256"]
        ):
            raise GatewayError("PACK_APPROVAL_STALE", 409)
        if operation == "get_pack_info":
            return (
                {
                    "pack_id": grant["pack_id"],
                    "included_count": len(selected),
                    "content_sha256": manifest["content_sha256"],
                    "consumed": grant["consumed"],
                },
                selected,
                None,
            )
        return (
            {
                "pack_id": grant["pack_id"],
                "format": manifest["config"]["format"],
                "estimated_tokens": estimate,
            },
            selected,
            rendered,
        )

    def _cursor_payload(
        self, grant: dict[str, Any], body: dict[str, Any], matches: list[SourceMemory]
    ) -> str:
        request = {k: v for k, v in body.items() if k != "cursor"}
        return hashlib.sha256(
            json.dumps(
                {
                    "grant": grant["id"],
                    "version": grant["version"],
                    "request": request,
                    "members": [s.fingerprint() for s in matches],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _cursor(
        self,
        grant: dict[str, Any],
        body: dict[str, Any],
        matches: list[SourceMemory],
        offset: int,
    ) -> str:
        payload = json.dumps(
            {
                "digest": self._cursor_payload(grant, body, matches),
                "offset": offset,
                "time": get_utc_now().timestamp(),
            },
            separators=(",", ":"),
        ).encode()
        signature = (
            hmac.new(self.repository.cursor_secret(), payload, hashlib.sha256)
            .hexdigest()
            .encode()
        )
        return base64.urlsafe_b64encode(payload + b"." + signature).decode()

    def _read_cursor(
        self,
        cursor: str | None,
        grant: dict[str, Any],
        body: dict[str, Any],
        matches: list[SourceMemory],
    ) -> int:
        if not cursor:
            return 0
        try:
            payload, signature = base64.urlsafe_b64decode(cursor).rsplit(b".", 1)
            if not hmac.compare_digest(
                signature,
                hmac.new(self.repository.cursor_secret(), payload, hashlib.sha256)
                .hexdigest()
                .encode(),
            ):
                raise ValueError
            state = json.loads(payload)
            if (
                state["digest"] != self._cursor_payload(grant, body, matches)
                or not 0 <= get_utc_now().timestamp() - state["time"] <= 300
                or not isinstance(state["offset"], int)
                or state["offset"] < 0
            ):
                raise ValueError
            return state["offset"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GatewayError("CURSOR_RESTART_REQUIRED", 409) from exc
