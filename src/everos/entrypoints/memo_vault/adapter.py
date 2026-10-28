"""Stable boundary between MEMO Vault workflows and EverOS APIs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from everos.component.utils.datetime import get_utc_now, to_timestamp_ms

DEFAULT_EVEROS_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class EverOSHealth:
    reachable: bool
    status: str
    api_version: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    candidate_id: str
    kind: Literal["preference", "fact", "decision", "todo", "episode", "profile"]
    content: str
    source_memory_id: str
    source_scope: str


@dataclass(frozen=True, slots=True)
class EverOSMemoryRef:
    memory_id: str
    scope: str
    content: str
    revision: int


@dataclass(frozen=True, slots=True)
class EverOSMemoryRecord:
    """Canonical memory body read from the scoped EverOS memory index."""

    memory_id: str
    memory_type: Literal["episode", "atomic_fact", "profile"]
    content: str
    timestamp: str | None
    metadata: dict[str, object]


class EverOSAdapter(Protocol):
    """Operations the Vault needs; implementations speak to EverOS over HTTP."""

    async def health(self) -> EverOSHealth: ...

    async def extract_import(
        self,
        *,
        batch_id: str,
        app_id: str,
        project_id: str,
        user_id: str,
        agent_id: str,
        messages: list[dict[str, object]],
    ) -> dict[str, object]: ...

    async def publish_approved(
        self, *, memory_id: str, scope: str, content: str,
        memory_type: str = "atomic_fact", metadata: dict[str, object] | None = None,
    ) -> EverOSMemoryRef: ...

    async def list_user_memories(
        self,
        *,
        app_id: str,
        project_id: str,
        user_id: str,
        memory_type: Literal["episode", "atomic_fact", "profile"],
    ) -> list[EverOSMemoryRecord]: ...

    async def get_user_memory(
        self,
        *,
        app_id: str,
        project_id: str,
        user_id: str,
        memory_id: str,
        memory_type: Literal["episode", "atomic_fact", "profile"],
    ) -> EverOSMemoryRecord | None: ...

    async def apply_memory_lifecycle(
        self,
        *,
        candidate_id: str,
        memory_id: str,
        app_id: str,
        project_id: str,
        user_id: str,
        memory_type: Literal["episode", "atomic_fact", "profile"],
        action: Literal["correct", "withdraw", "delete"],
        expected_revision: int,
        content: str | None = None,
    ) -> dict[str, object]: ...

class EverOSAdapterError(RuntimeError):
    """EverOS is unavailable or rejected the import extraction request."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "EVEROS_UNAVAILABLE",
        status_code: int = 503,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.stage = stage


def _raise_for_everos_error(response: httpx.Response, *, operation: str) -> None:
    """Keep safe, structured error details from EverOS visible to the Vault UI."""
    if response.is_success:
        return
    code = f"EVEROS_HTTP_{response.status_code}"
    message = f"EverOS {operation} failed with HTTP {response.status_code}."
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict) and isinstance(payload, dict):
        detail = payload.get("error")
    stage = "everos_health" if operation == "health check" else "everos_contract"
    if isinstance(detail, dict):
        upstream_code = detail.get("code")
        upstream_message = detail.get("message")
        if isinstance(upstream_code, str) and upstream_code.isidentifier():
            code = upstream_code
        if isinstance(upstream_message, str) and upstream_message.strip():
            message = upstream_message.strip()[:500]
        upstream_stage = detail.get("stage")
        if isinstance(upstream_stage, str) and upstream_stage in {
            "model_boundary", "model_episode", "model_atomic_fact",
            "model_preference", "model_call", "everos_health", "everos_contract"
        }:
            stage = upstream_stage
    raise EverOSAdapterError(
        message, code=code, status_code=response.status_code, stage=stage
    )


class EverOSHttpAdapter:
    """HTTP adapter for candidate extraction, publication, and canonical reads."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_EVEROS_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.scan_budget: list[int] | None = None

    async def health(self) -> EverOSHealth:
        """Probe EverOS liveness with a short timeout for the status badge."""
        async with httpx.AsyncClient(
            timeout=min(self.timeout_seconds, 3.0), transport=self.transport
        ) as client:
            try:
                response = await client.get(f"{self.base_url}/health")
                if not response.is_success:
                    return EverOSHealth(False, f"http_{response.status_code}")
                payload = response.json()
                status = payload.get("status") if isinstance(payload, dict) else None
                return EverOSHealth(
                    status == "ok",
                    str(status or "invalid"),
                    (
                        payload.get("version")
                        if isinstance(payload, dict)
                        and isinstance(payload.get("version"), str)
                        else None
                    ),
                )
            except (httpx.HTTPError, ValueError) as exc:
                return EverOSHealth(False, type(exc).__name__)

    async def publish_approved(
        self, *, memory_id: str, scope: str, content: str,
        memory_type: str = "atomic_fact", metadata: dict[str, object] | None = None,
    ) -> EverOSMemoryRef:
        """Publish the exact human-approved text to the matching EverOS store."""
        parts = scope.split("/")
        if len(parts) != 4 or parts[2] != "users":
            raise EverOSAdapterError(
                "Approved memory scope must be app/project/users/user_id.",
                code="INVALID_MEMORY_SCOPE",
            )
        payload = {
            "candidate_id": memory_id,
            "app_id": parts[0],
            "project_id": parts[1],
            "user_id": parts[3],
            "content": content,
        }
        if memory_type != "atomic_fact":
            payload["memory_type"] = memory_type
            payload["metadata"] = metadata or {}
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v2/memo-vault/candidates/{memory_id}/publish",
                    json=payload,
                )
                _raise_for_everos_error(
                    response, operation="approved candidate publish"
                )
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, dict) or not all(
                    isinstance(data.get(k), str)
                    for k in ("memory_id", "scope", "content")
                ):
                    raise EverOSAdapterError(
                        "EverOS returned an invalid approved-memory receipt.",
                        code="EVEROS_INVALID_PUBLISH_RESPONSE",
                    )
                return EverOSMemoryRef(
                    memory_id=data["memory_id"],
                    scope=data["scope"],
                    content=data["content"],
                    revision=int(data.get("revision", 1)),
                )
            except httpx.HTTPError as exc:
                raise EverOSAdapterError(
                    "Could not connect to EverOS while publishing approved memory "
                    f"({type(exc).__name__})."
                ) from exc
            except ValueError as exc:
                raise EverOSAdapterError(
                    "EverOS returned an invalid approved-memory response."
                ) from exc

    async def apply_memory_lifecycle(
        self,
        *,
        candidate_id: str,
        memory_id: str,
        app_id: str,
        project_id: str,
        user_id: str,
        memory_type: Literal["episode", "atomic_fact", "profile"],
        action: Literal["correct", "withdraw", "delete"],
        expected_revision: int,
        content: str | None = None,
    ) -> dict[str, object]:
        """Apply a confirmed memory mutation through EverOS native storage."""
        payload: dict[str, object] = {
            "candidate_id": candidate_id,
            "memory_id": memory_id,
            "app_id": app_id,
            "project_id": project_id,
            "user_id": user_id,
            "memory_type": memory_type,
            "action": action,
            "expected_revision": expected_revision,
        }
        if content is not None:
            payload["content"] = content
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v2/memo-vault/candidates/{candidate_id}/lifecycle",
                    json=payload,
                )
                _raise_for_everos_error(response, operation="memory lifecycle update")
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("lifecycle_status"), str)
                    or not isinstance(data.get("revision"), int)
                    or not isinstance(data.get("content"), str)
                ):
                    raise EverOSAdapterError(
                        "EverOS returned an invalid memory lifecycle receipt.",
                        code="EVEROS_INVALID_LIFECYCLE_RESPONSE",
                    )
                return data
            except httpx.HTTPError as exc:
                raise EverOSAdapterError(
                    "Could not connect to EverOS while updating a memory "
                    f"({type(exc).__name__})."
                ) from exc
            except ValueError as exc:
                raise EverOSAdapterError(
                    "EverOS returned an invalid memory lifecycle response."
                ) from exc

    async def list_user_memories(
        self,
        *,
        app_id: str,
        project_id: str,
        user_id: str,
        memory_type: Literal["episode", "atomic_fact", "profile"],
    ) -> list[EverOSMemoryRecord]:
        """Read every canonical page while pinning and checking all scope fields."""
        if memory_type == "profile":
            payload = await self._get_page(
                app_id=app_id,
                project_id=project_id,
                user_id=user_id,
                memory_type=memory_type,
                page=1,
                page_size=1,
            )
            rows = payload.get("profiles")
            if (
                not isinstance(payload.get("total_count"), int)
                or isinstance(payload.get("total_count"), bool)
                or payload["total_count"] not in {0, 1}
                or not isinstance(payload.get("count"), int)
                or isinstance(payload.get("count"), bool)
                or not isinstance(rows, list)
                or payload["count"] != len(rows)
            ):
                raise EverOSAdapterError(
                    "EverOS returned an invalid profile response.",
                    code="EVEROS_INVALID_MEMORY_RESPONSE",
                    status_code=502,
                )
            if self.scan_budget is not None:
                if payload["total_count"] > self.scan_budget[0]:
                    raise EverOSAdapterError(
                        "Source scan limit exceeded.",
                        code="GATEWAY_SCAN_LIMIT",
                        status_code=413,
                    )
                self.scan_budget[0] -= payload["total_count"]
            return self._shape_user_records(
                payload, memory_type=memory_type,
                app_id=app_id, project_id=project_id, user_id=user_id,
            )

        records: list[EverOSMemoryRecord] = []
        seen_ids: set[str] = set()
        expected_total: int | None = None
        page = 1
        page_size = 100
        while expected_total is None or len(records) < expected_total:
            data = await self._get_page(
                app_id=app_id,
                project_id=project_id,
                user_id=user_id,
                memory_type=memory_type,
                page=page,
                page_size=page_size,
            )
            total_count = data.get("total_count")
            count = data.get("count")
            rows = data.get("atomic_facts" if memory_type == "atomic_fact" else "episodes")
            if (
                not isinstance(total_count, int)
                or isinstance(total_count, bool)
                or total_count < 0
                or not isinstance(count, int)
                or isinstance(count, bool)
                or not isinstance(rows, list)
                or count != len(rows)
            ):
                raise EverOSAdapterError(
                    "EverOS returned an invalid paginated memory response.",
                    code="EVEROS_INVALID_MEMORY_RESPONSE",
                    status_code=502,
                )
            if expected_total is None:
                if self.scan_budget is not None:
                    if total_count > self.scan_budget[0]:
                        raise EverOSAdapterError("Source scan limit exceeded.",
                        code="GATEWAY_SCAN_LIMIT",
                        status_code=413,)
                    self.scan_budget[0] -= total_count
                expected_total = total_count
            elif total_count != expected_total:
                raise EverOSAdapterError(
                    "EverOS memory index changed while pages were being read.",
                    code="EVEROS_MEMORY_PAGE_CHANGED",
                    status_code=503,
                )
            shaped = self._shape_user_records(
                data, memory_type=memory_type,
                app_id=app_id, project_id=project_id, user_id=user_id,
            )
            for record in shaped:
                if record.memory_id in seen_ids:
                    raise EverOSAdapterError(
                        "EverOS returned a duplicate memory across pages.",
                        code="EVEROS_INVALID_MEMORY_RESPONSE",
                        status_code=502,
                    )
                seen_ids.add(record.memory_id)
                records.append(record)
            if len(records) > expected_total or (count == 0 and len(records) < expected_total):
                raise EverOSAdapterError(
                    "EverOS memory pagination ended before all rows were returned.",
                    code="EVEROS_INVALID_MEMORY_RESPONSE",
                    status_code=502,
                )
            page += 1
        if len(records) != expected_total:
            raise EverOSAdapterError(
                "EverOS memory pagination returned an incomplete result.",
                code="EVEROS_INVALID_MEMORY_RESPONSE",
                status_code=502,
            )
        return records

    async def get_user_memory(
        self,
        *,
        app_id: str,
        project_id: str,
        user_id: str,
        memory_id: str,
        memory_type: Literal["episode", "atomic_fact", "profile"],
    ) -> EverOSMemoryRecord | None:
        """Read one exact in-scope native record or the candidate's profile item."""
        if memory_type == "profile":
            records = await self.list_user_memories(
                app_id=app_id,
                project_id=project_id,
                user_id=user_id,
                memory_type="profile",
            )
            return next((item for item in records if item.memory_id == memory_id), None)

        data = await self._get_page(
            app_id=app_id,
            project_id=project_id,
            user_id=user_id,
            memory_type=memory_type,
            memory_id=memory_id,
            page=1,
            page_size=1,
        )
        rows = data.get("atomic_facts" if memory_type == "atomic_fact" else "episodes")
        total_count = data.get("total_count")
        count = data.get("count")
        if (
            not isinstance(rows, list)
            or not isinstance(total_count, int)
            or isinstance(total_count, bool)
            or total_count not in {0, 1}
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count != len(rows)
        ):
            raise EverOSAdapterError(
                "EverOS returned an invalid exact memory response.",
                code="EVEROS_INVALID_MEMORY_RESPONSE",
                status_code=502,
            )
        records = self._shape_user_records(
            data, memory_type=memory_type,
            app_id=app_id, project_id=project_id, user_id=user_id,
        )
        if len(records) > 1:
            raise EverOSAdapterError(
                "EverOS returned more than one record for an exact memory ID.",
                code="EVEROS_INVALID_MEMORY_RESPONSE",
                status_code=502,
            )
        if records and records[0].memory_id != memory_id:
            raise EverOSAdapterError(
                "EverOS ignored the requested exact memory ID.",
                code="EVEROS_INVALID_MEMORY_RESPONSE",
                status_code=502,
            )
        return records[0] if records else None

    async def _get_page(
        self,
        *,
        app_id: str,
        project_id: str,
        user_id: str,
        memory_type: Literal["episode", "atomic_fact", "profile"],
        page: int,
        page_size: int,
        memory_id: str | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "user_id": user_id,
            "app_id": app_id,
            "project_id": project_id,
            "memory_type": memory_type,
            "page": page,
            "page_size": page_size,
            "sort_by": "timestamp",
            "sort_order": "desc",
        }
        if memory_id is not None:
            body["memory_id"] = memory_id
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v2/memory/get", json=body
                )
                if not response.is_success:
                    _raise_for_everos_error(response, operation="memory read")
                envelope = response.json()
            except httpx.HTTPError as exc:
                raise EverOSAdapterError(
                    "Could not connect to EverOS while reading canonical memories "
                    f"({type(exc).__name__})."
                ) from exc
            except ValueError as exc:
                raise EverOSAdapterError(
                    "EverOS returned invalid JSON while reading canonical memories.",
                    code="EVEROS_INVALID_MEMORY_RESPONSE",
                    status_code=502,
                ) from exc
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            raise EverOSAdapterError(
                "EverOS returned an invalid memory response envelope.",
                code="EVEROS_INVALID_MEMORY_RESPONSE",
                status_code=502,
            )
        return data

    @staticmethod
    def _shape_user_records(
        data: dict[str, object],
        *,
        memory_type: Literal["episode", "atomic_fact", "profile"],
        app_id: str,
        project_id: str,
        user_id: str,
    ) -> list[EverOSMemoryRecord]:
        field = {"episode": "episodes", "atomic_fact": "atomic_facts", "profile": "profiles"}[memory_type]
        rows = data.get(field)
        if not isinstance(rows, list):
            raise EverOSAdapterError(
                "EverOS response is missing the requested memory array.",
                code="EVEROS_INVALID_MEMORY_RESPONSE",
                status_code=502,
            )
        result: list[EverOSMemoryRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                raise EverOSAdapterError(
                    "EverOS returned an invalid memory item.",
                    code="EVEROS_INVALID_MEMORY_RESPONSE",
                    status_code=502,
                )
            owner_field = "user_id"
            if (
                row.get("app_id") != app_id
                or row.get("project_id") != project_id
                or row.get(owner_field) != user_id
            ):
                raise EverOSAdapterError(
                    "EverOS returned a memory outside the requested scope.",
                    code="EVEROS_MEMORY_SCOPE_MISMATCH",
                    status_code=502,
                )
            if memory_type == "profile":
                profile_data = row.get("profile_data")
                if not isinstance(profile_data, dict):
                    raise EverOSAdapterError(
                        "EverOS returned an invalid profile payload.",
                        code="EVEROS_INVALID_MEMORY_RESPONSE",
                        status_code=502,
                    )
                for section in ("explicit_info", "implicit_traits"):
                    entries = profile_data.get(section, [])
                    if not isinstance(entries, list):
                        raise EverOSAdapterError(
                            "EverOS returned an invalid profile entry list.",
                            code="EVEROS_INVALID_MEMORY_RESPONSE",
                            status_code=502,
                        )
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        candidate_id = entry.get("memo_vault_candidate_id")
                        content = entry.get("description")
                        if not isinstance(candidate_id, str) or not candidate_id or not isinstance(content, str):
                            continue
                        result.append(EverOSMemoryRecord(
                            memory_id=f"profile_{user_id}_{candidate_id}",
                            memory_type="profile",
                            content=content,
                            timestamp=None,
                            metadata={
                                "profile_id": row.get("id"),
                                "section": section,
                                "item": entry,
                                "profile_timestamp_ms": profile_data.get("profile_timestamp_ms"),
                            },
                        ))
                continue

            memory_id = row.get("id")
            content_field = "episode" if memory_type == "episode" else "fact"
            content = row.get(content_field)
            timestamp = row.get("timestamp")
            if not isinstance(memory_id, str) or not isinstance(content, str) or not isinstance(timestamp, str):
                raise EverOSAdapterError(
                    "EverOS returned an incomplete memory item.",
                    code="EVEROS_INVALID_MEMORY_RESPONSE",
                    status_code=502,
                )
            metadata: dict[str, object]
            if memory_type == "episode":
                metadata = {
                    "summary": row.get("summary", ""),
                    "subject": row.get("subject", ""),
                    "session_id": row.get("session_id", ""),
                    "sender_ids": row.get("sender_ids", []),
                }
            else:
                metadata = {
                    "session_id": row.get("session_id"),
                    "parent_id": row.get("parent_id", ""),
                }
            result.append(EverOSMemoryRecord(
                memory_id=memory_id,
                memory_type=memory_type,
                content=content,
                timestamp=timestamp,
                metadata=metadata,
            ))
        if memory_type == "profile" and len(rows) > 1:
            raise EverOSAdapterError(
                "EverOS returned more than one profile row for a user scope.",
                code="EVEROS_INVALID_MEMORY_RESPONSE",
                status_code=502,
            )
        return result

    async def extract_import(
        self,
        *,
        batch_id: str,
        app_id: str,
        project_id: str,
        user_id: str,
        agent_id: str,
        messages: list[dict[str, object]],
    ) -> dict[str, object]:
        """Extract review drafts per conversation; never flush the normal pipeline."""
        conversations: dict[str, list[dict[str, object]]] = {}
        deferred_tool_message_count = 0
        unsupported_role_message_count = 0
        for index, message in enumerate(messages):
            conversation_id = str(message.get("conversation_id") or "conversation-1")
            original_role = str(message.get("role", ""))
            if original_role == "tool":
                deferred_tool_message_count += 1
                continue
            if original_role not in {"user", "assistant"}:
                unsupported_role_message_count += 1
                continue
            conversations.setdefault(conversation_id, []).append(
                {
                    "id": str(message.get("id") or f"message-{index}"),
                    "role": original_role,
                    "timestamp_ms": int(
                        message.get("timestamp_ms")
                        or to_timestamp_ms(get_utc_now()) + index
                    ),
                    "content": str(message.get("content", "")),
                }
            )
        if not conversations:
            raise EverOSAdapterError(
                "This batch has no messages supported by the current EverOS adapter."
            )

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            try:
                health = await client.get(f"{self.base_url}/health")
                _raise_for_everos_error(health, operation="health check")
                health_data = health.json()
                if (
                    not isinstance(health_data, dict)
                    or health_data.get("status") != "ok"
                ):
                    raise EverOSAdapterError(
                        "EverOS health check did not report status=ok.", stage="everos_health"
                    )
                candidates: list[dict[str, object]] = []
                for conversation_id, items in conversations.items():
                    conversation_digest = hashlib.sha256(
                        conversation_id.encode()
                    ).hexdigest()[:12]
                    for start in range(0, len(items), 500):
                        response = await client.post(
                            f"{self.base_url}/api/v2/memo-vault/candidates/extract",
                            json={
                                "batch_id": batch_id,
                                "session_id": (
                                    f"mv_{batch_id[:20]}_{conversation_digest}"
                                ),
                                "app_id": app_id,
                                "project_id": project_id,
                                "user_id": user_id,
                                "agent_id": agent_id,
                                "messages": items[start : start + 500],
                            },
                        )
                        _raise_for_everos_error(
                            response, operation="candidate extraction"
                        )
                        result_data = response.json()
                        response_data = (
                            result_data.get("data")
                            if isinstance(result_data, dict)
                            else None
                        )
                        if (
                            not isinstance(response_data, dict)
                            or response_data.get("status") != "extracted"
                            or not isinstance(response_data.get("candidates"), list)
                        ):
                            raise EverOSAdapterError(
                                "EverOS did not return candidate extraction results "
                                f"conversation {conversation_id}.", stage="everos_contract"
                            )
                        for candidate in response_data["candidates"]:
                            if not isinstance(candidate, dict):
                                raise EverOSAdapterError(
                                    "EverOS returned an invalid candidate record."
                                )
                            if not all(
                                isinstance(candidate.get(field), str)
                                for field in ("candidate_id", "kind", "content")
                            ):
                                raise EverOSAdapterError(
                                    "EverOS returned an incomplete candidate record."
                                )
                            source_ids = candidate.get("source_message_ids")
                            if not isinstance(source_ids, list) or not source_ids:
                                raise EverOSAdapterError(
                                    "EverOS returned a candidate without source "
                                    "evidence."
                                )
                            candidates.append(candidate)
            except httpx.HTTPError as exc:
                raise EverOSAdapterError(
                    "Could not connect to EverOS while requesting local extraction "
                    f"({type(exc).__name__}).", stage="everos_health"
                ) from exc
            except ValueError as exc:
                raise EverOSAdapterError(
                    "EverOS returned an invalid response during local extraction.", stage="everos_contract"
                ) from exc
        return {
            "status": "extracted",
            "method": "local",
            "candidate_count": len(candidates),
            "candidate_scope": {
                "app_id": app_id,
                "project_id": project_id,
                "user_id": user_id,
            },
            "candidates": candidates,
            "user_id": user_id,
            "agent_id": agent_id,
            "conversation_count": len(conversations),
            "deferred_tool_message_count": deferred_tool_message_count,
            "unsupported_role_message_count": unsupported_role_message_count,
        }
