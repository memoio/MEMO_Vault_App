"""Vault-only stdio MCP adapter; delegates all policy to the HTTP gateway."""

from .server import GatewayMcpClient as GatewayMcpClient
from .server import create_server as create_server
from .server import run_stdio as run_stdio

__all__ = ["GatewayMcpClient", "create_server", "run_stdio"]
