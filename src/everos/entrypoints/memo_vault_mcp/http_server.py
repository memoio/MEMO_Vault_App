"""Grant-scoped Streamable HTTP MCP endpoint; bind only to loopback."""

from __future__ import annotations

import contextvars
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .server import GatewayMcpClient, create_server

_current_client: contextvars.ContextVar[GatewayMcpClient] = contextvars.ContextVar(
    "mcp_gateway_client"
)


class RequestGateway:
    async def request(self, path: str, body: dict | None = None) -> dict:
        return await _current_client.get().request(path, body)


def create_http_app(
    *, gateway: str = "http://127.0.0.1:8766", http: httpx.AsyncClient | None = None
) -> FastAPI:
    server = create_server(RequestGateway())
    hosts = os.environ.get(
        "MEMO_VAULT_MCP_ALLOWED_HOSTS",
        "127.0.0.1,127.0.0.1:*,localhost,localhost:*,[::1],[::1]:*",
    ).split(",")
    origins = [
        x for x in os.environ.get("MEMO_VAULT_MCP_ALLOWED_ORIGINS", "").split(",") if x
    ]
    manager = StreamableHTTPSessionManager(
        server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
        max_request_body_size=65536,
    )

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def mcp_endpoint(scope, receive, send):
        request = Request(scope, receive)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer mv_") or len(authorization) > 256:
            await JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        client = GatewayMcpClient(gateway, authorization[7:], http=http)
        result = await client.request("/capabilities")
        if "error" in result:
            status = (
                503
                if result["error"] == "GATEWAY_UNAVAILABLE"
                else (429 if result.get("status") == 429 else 403)
            )
            await JSONResponse({"detail": "Access denied"}, status_code=status)(
                scope, receive, send
            )
            return
        marker = _current_client.set(client)
        try:
            await manager.handle_request(scope, receive, send)
        finally:
            _current_client.reset(marker)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await mcp_endpoint(scope, receive, send)

    app.router.routes.append(
        Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"])
    )
    return app


app = create_http_app(
    gateway=os.environ.get("MEMO_VAULT_MCP_GATEWAY_URL", "http://127.0.0.1:8766")
)
