"""Owner grant creation policy, independent of HTTP request/session handling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from everos.infra.memo_vault_gateway import GatewayRepository, RepositoryDeniedError
from everos.service.memo_vault_export_contracts import GatewayError, GrantCreate
from everos.service.memo_vault_gateway import GatewayService


class GrantService:
    def __init__(
        self,
        repository: GatewayRepository,
        gateway: GatewayService,
        has_project: Callable[[str], bool],
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.has_project = has_project

    async def create(
        self,
        config: GrantCreate,
        *,
        draft_id: str | None = None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        if not config.confirm_transfer or (
            config.mode == "live_scope" and not config.confirm_future_allow
        ):
            raise GatewayError("GRANT_CONFIRMATION_REQUIRED", 422)
        if any(not self.has_project(project) for project in config.project_ids):
            raise GatewayError("INVALID_PROJECT_SCOPE", 422)
        manifest = None
        if config.mode == "approved_pack":
            try:
                draft = self.repository.draft(draft_id or "")
                if (
                    sorted(config.project_ids) != draft["scope"]["project_ids"]
                    or sorted(config.kinds) != draft["scope"]["kinds"]
                ):
                    raise RepositoryDeniedError()
                manifest = draft["manifest"]
                if (
                    manifest["config"]["token_budget"] > config.token_budget
                    or manifest["config"]["evidence_mode"] != config.evidence_mode
                ):
                    raise RepositoryDeniedError()
                sources = await self.gateway.load_sources(config.project_ids)
                self.gateway.validate_manifest(manifest, sources)
            except (RepositoryDeniedError, GatewayError) as exc:
                raise GatewayError("PACK_APPROVAL_STALE", 409) from exc
        elif draft_id:
            raise GatewayError("INVALID_GRANT_MODE", 422)
        try:
            return self.repository.create_grant(
                config.model_dump(mode="json"),
                manifest=manifest,
                draft_id=draft_id,
                request_key=request_key,
            )
        except RepositoryDeniedError as exc:
            raise GatewayError("PACK_APPROVAL_STALE", 409) from exc
