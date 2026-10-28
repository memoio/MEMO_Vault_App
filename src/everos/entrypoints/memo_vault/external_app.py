"""Dedicated delegated-access listener, defaulting to loopback port 8766."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adapter import EverOSHttpAdapter
from .auth import DEFAULT_ORIGINS
from .config import get_settings
from .gateway_runtime import configure_gateway, gateway_url
from .request_limits import RequestLimitMiddleware
from .routes.external_gateway import router
from .storage import VaultStore


def create_external_app(
    *, store: VaultStore | None = None, adapter: Any = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings = get_settings()
        local_store = store or VaultStore(settings.data_dir)
        local_store.initialize()
        local_adapter = adapter or EverOSHttpAdapter(
            settings.everos_base_url, timeout_seconds=30
        )
        application.state.everos_adapter = local_adapter
        configure_gateway(application, local_store, local_adapter)
        yield

    application = FastAPI(
        title="MEMO Vault External Gateway", version="0.1.0", lifespan=lifespan
    )
    origins = os.environ.get("MEMO_VAULT_UI_ORIGINS", ",".join(DEFAULT_ORIGINS)).split(
        ","
    )
    hosts = {"127.0.0.1", "localhost", "::1", urlsplit(gateway_url()).hostname} | {
        urlsplit(o).hostname for o in origins
    }
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[h for h in hosts if h]
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Vault-Client"],
        expose_headers=["X-Vault-Receipt", "X-Content-SHA256", "X-Request-ID"],
    )

    @application.middleware("http")
    async def external_boundary(request: Request, call_next):
        if request.headers.get("origin") and request.headers["origin"] not in origins:
            return JSONResponse({"detail": "access denied"}, status_code=403)
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 65536:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    application.add_middleware(RequestLimitMiddleware)

    @application.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    application.include_router(router, prefix="/api/v1")
    if store is not None:
        application.state.everos_adapter = adapter
        configure_gateway(application, store, adapter)
    return application


app = create_external_app()
