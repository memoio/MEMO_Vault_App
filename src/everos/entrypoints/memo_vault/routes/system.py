"""Health and feature capability endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib.util import find_spec

from fastapi import APIRouter, Request

from ..adapter import EverOSHttpAdapter
from ..config import get_settings
from ..contracts import ApiEnvelope, HealthData

router = APIRouter(prefix="/system", tags=["memo-vault-system"])


@router.get("/health", response_model=ApiEnvelope[HealthData])
async def health(request: Request) -> ApiEnvelope[HealthData]:
    settings = get_settings()
    identity = request.app.state.vault_store.get_identity()
    adapter = getattr(request.app.state, "everos_adapter", None) or EverOSHttpAdapter(
        settings.everos_base_url, timeout_seconds=settings.everos_timeout_seconds
    )
    everos_health = await adapter.health()
    return ApiEnvelope(
        request_id=uuid.uuid4().hex,
        data=HealthData(
            everos_status="online" if everos_health.reachable else "offline",
            everos_checked_at=datetime.now(UTC).isoformat(),
            everos_base_url=settings.everos_base_url,
            data_dir=str(settings.data_dir),
            app_id=identity["app_id"],
            user_id=identity["user_id"],
            agent_id=identity["agent_id"],
            max_import_bytes=settings.max_import_bytes,
            max_archive_unpacked_bytes=settings.max_archive_unpacked_bytes,
            max_archive_files=settings.max_archive_files,
            max_archive_compression_ratio=settings.max_archive_compression_ratio,
            features={
                "imports": "ready",
                "review": "ready",
                "library": "ready",
                "memory_pack": "ready",
                "grant_management": "ready",
                "external_pack_api": "ready",
                "vault_gateway": "ready",
                "vault_mcp_stdio": "ready" if find_spec("mcp") else "not_started",
                "vault_mcp_remote": "not_started",
                "adp_export": "ready",
            },
        ),
    )
