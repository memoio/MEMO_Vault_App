"""Local Vault gateway launcher and optional stdio MCP adapter commands."""

from __future__ import annotations

import asyncio
import os

import typer


def register(app: typer.Typer) -> None:
    @app.command("vault-mcp")
    def vault_mcp(
        gateway: str = typer.Option(
            "http://127.0.0.1:8766", help="Local Vault external gateway URL."
        ),
    ) -> None:
        """Expose only the tools allowed by MEMO_VAULT_TOKEN over stdio."""
        token = os.environ.get("MEMO_VAULT_TOKEN", "")
        if not token:
            typer.echo(
                "Set MEMO_VAULT_TOKEN to an external grant credential.", err=True
            )
            raise typer.Exit(2)
        try:
            from everos.entrypoints.memo_vault_mcp import run_stdio

            asyncio.run(run_stdio(gateway, token))
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("mcp"):
                typer.echo(
                    "From the EverOS checkout, install the adapter: "
                    "pip install -e '.[mcp]'",
                    err=True,
                )
                raise typer.Exit(2) from exc
            raise
        except ValueError:
            typer.echo("Invalid gateway URL or external credential.", err=True)
            raise typer.Exit(2) from None

    @app.command("vault-gateway")
    def vault_gateway(
        host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8766)
    ) -> None:
        """Run the external-only Vault API (initially paused until owner enables it)."""
        if host not in {"127.0.0.1", "localhost", "::1"}:
            typer.echo(
                "Bind to loopback; use a configured TLS gateway for remote access.",
                err=True,
            )
            raise typer.Exit(2)
        import uvicorn

        uvicorn.run(
            "everos.entrypoints.memo_vault.external_app:app",
            host=host,
            port=port,
            access_log=False,
        )
