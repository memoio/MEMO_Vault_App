"""Composition adapters joining Vault governance and the neutral gateway use case."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI

from everos.infra.memo_vault_gateway import GatewayRepository
from everos.service.memo_vault_export_contracts import GatewayError, SourceMemory
from everos.service.memo_vault_gateway import GatewayService
from everos.service.memo_vault_grants import GrantService

from .adapter import EverOSHttpAdapter
from .contracts import PackPreviewRequest
from .library import (
    LibraryError,
    MemoVaultLibraryService,
    PackEvidenceReference,
    PackSourceMemory,
)
from .memory_pack import MemoryPackService
from .storage import VaultStore


def gateway_url() -> str:
    return os.environ.get("MEMO_VAULT_GATEWAY_URL", "http://127.0.0.1:8766").rstrip("/")


def configure_gateway(app: FastAPI, store: VaultStore, adapter: Any) -> GatewayService:
    repository = GatewayRepository(
        store.database_path,
        audit_retention_days=int(
            os.environ.get("MEMO_VAULT_AUDIT_RETENTION_DAYS", "30")
        ),
    )
    repository.initialize()

    async def load(project_ids: list[str]) -> list[SourceMemory]:
        selected_adapter = adapter
        if isinstance(adapter, EverOSHttpAdapter):
            selected_adapter = EverOSHttpAdapter(
                adapter.base_url,
                timeout_seconds=min(adapter.timeout_seconds, 30),
                transport=adapter.transport,
            )
            selected_adapter.scan_budget = [5000]
        library = MemoVaultLibraryService(store, selected_adapter)
        loaded: list[PackSourceMemory] = []
        for project_id in project_ids:
            try:
                loaded.extend(await library.list_pack_sources(project_id))
            except LibraryError as exc:
                if exc.code == "GATEWAY_SCAN_LIMIT":
                    raise GatewayError("SCAN_LIMIT", 413) from exc
                raise
            if len(loaded) > 5000:
                raise GatewayError("SCAN_LIMIT", 413)
        library.revalidate_pack_sources(project_ids, loaded)
        result = []
        for source in loaded:
            value = asdict(source)
            # The legacy evidence ID binds the originally stored quote hash;
            # the egress hash binds the currently matched, bounded excerpt.
            for evidence in value["evidence"]:
                evidence["quote_sha256"] = hashlib.sha256(
                    evidence["quote"].encode()
                ).hexdigest()
            result.append(SourceMemory.model_validate(value))
        return result

    def render(
        sources: list[SourceMemory], config: dict[str, Any], generated_at: str
    ) -> tuple[str, int, list[str]]:
        originals = to_pack_sources(sources)
        request = PackPreviewRequest(
            namespaces=sorted({s.namespace for s in sources}) or ["personal"],
            kinds=list(dict.fromkeys(s.kind for s in sources))
            or ["fact", "episode", "profile"],
            memory_ids=[s.memory_id for s in sources],
            **config,
        )
        preview = MemoryPackService().preview(
            request, originals, generated_at=generated_at
        )
        return (
            preview.rendered_content,
            preview.estimated_tokens,
            [item.memory_id for item in preview.items if item.included],
        )

    service = GatewayService(repository, load, render)
    app.state.gateway_repository = repository
    app.state.gateway_service = service
    app.state.grant_service = GrantService(repository, service, store.has_project)
    return service


def to_pack_sources(sources: list[SourceMemory]) -> list[PackSourceMemory]:
    originals = []
    for source in sources:
        fields = source.model_dump(exclude={"evidence"})
        fields["evidence"] = tuple(
            PackEvidenceReference(**e.model_dump()) for e in source.evidence
        )
        originals.append(PackSourceMemory(**fields))
    return originals
