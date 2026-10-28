"""Standalone FastAPI app for the local MEMO Vault console."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .adapter import EverOSHttpAdapter
from .auth import OwnerSecurity, install_owner_security
from .config import get_settings
from .gateway_runtime import configure_gateway
from .import_jobs import ImportJobManager
from .imports import ImportService
from .request_limits import ClaudeLocalUploadLimitMiddleware
from .routes import (
    candidates,
    claude_content,
    exports,
    grants,
    import_jobs,
    imports,
    knowledge_exports,
    memories,
    projects,
    system,
)
from .storage import VaultStore


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize Vault-local runtime data when the service starts."""

    settings = get_settings()
    store = VaultStore(settings.data_dir)
    store.initialize()
    app.state.vault_store = store
    app.state.everos_adapter = EverOSHttpAdapter(
        settings.everos_base_url, timeout_seconds=settings.everos_timeout_seconds
    )
    configure_gateway(app, store, app.state.everos_adapter)
    app.state.owner_security = OwnerSecurity(settings.data_dir)
    app.state.import_service = ImportService(
        store,
        max_file_bytes=settings.max_import_bytes,
        max_archive_unpacked_bytes=settings.max_archive_unpacked_bytes,
        max_archive_files=settings.max_archive_files,
        max_archive_compression_ratio=settings.max_archive_compression_ratio,
    )
    app.state.import_jobs = ImportJobManager(store, app.state.import_service)
    app.state.import_job_tasks = {}
    app.state.import_job_semaphore = asyncio.Semaphore(2)
    for job_id in app.state.import_jobs.recover():
        import_jobs.launch_job(app, job_id)
    try:
        yield
    finally:
        # Wait for workers; cancelling to_thread cannot stop filesystem writes.
        tasks = list(app.state.import_job_tasks.values())
        if tasks:
            app.state.import_jobs.stopping.set()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="MEMO Vault Local API",
    version="0.1.0",
    description="Local-first MEMO Vault import and archival API.",
    lifespan=lifespan,
)
for route in (
    system.router,
    projects.router,
    imports.router,
    candidates.router,
    memories.router,
):
    app.include_router(route, prefix="/api/v1")
app.include_router(exports.router, prefix="/api/v1")

app.include_router(import_jobs.router, prefix="/api/v1")
app.include_router(claude_content.router, prefix="/api/v1")
app.include_router(knowledge_exports.router, prefix="/api/v1")

app.include_router(grants.router, prefix="/api/v1")
app.add_middleware(ClaudeLocalUploadLimitMiddleware)
install_owner_security(app)
