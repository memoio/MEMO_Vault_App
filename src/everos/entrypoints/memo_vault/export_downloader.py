"""Bounded complete downloads with DNS pinning and durable verified ZIP cache."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import socket
import zipfile
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .claude_manifest import validate_download_url
from .import_contracts import ImportWorkflowError

_download_context: ContextVar[bool] = ContextVar("claude_download", default=False)


class _PrivateDownloadFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _download_context.get()


# HTTP libraries log request URLs (and redirect headers at DEBUG). Suppress only
# this worker context; other requests and application logging remain unaffected.
for _logger in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
):
    logging.getLogger(_logger).addFilter(_PrivateDownloadFilter())


def write_private(path: Path, data: bytes) -> None:
    """Atomically persist private state before publishing its final filename."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        os.chmod(temporary, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class ExportDownloader:
    """Stream once into a partial file; never probe a potentially single-use link."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        max_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.transport = transport
        self.max_bytes = max_bytes

    @staticmethod
    def checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def download(
        self,
        url: str,
        target: Path,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[int], None] = lambda _: None,
    ) -> str:
        """Reuse verified caches and report expired links without their tokens."""
        validate_download_url(url)
        marker = target.with_suffix(".sha256")
        if target.is_file():
            digest = self.checksum(target)
            if not marker.exists():
                try:
                    with zipfile.ZipFile(target) as archive:
                        if not archive.infolist():
                            raise zipfile.BadZipFile
                    write_private(marker, digest.encode())
                except zipfile.BadZipFile as exc:
                    raise ImportWorkflowError(
                        "CLAUDE_INVALID_ZIP", "Cached export is not a readable ZIP."
                    ) from exc
            if digest == marker.read_text():
                return digest
            raise ImportWorkflowError(
                "CLAUDE_CACHE_CORRUPTED", "Cached export checksum mismatch."
            )
        partial = target.with_suffix(".partial")
        target.parent.mkdir(parents=True, exist_ok=True)
        token = _download_context.set(True)
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=httpx.Timeout(120, connect=30),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                for attempt in range(6):
                    host = validate_download_url(url, redirect=attempt > 0)
                    request_url = httpx.URL(url)
                    headers = {
                        "Accept-Encoding": "identity",
                        "User-Agent": "EverOS-Claude-Import/1.0",
                    }
                    extensions = {}
                    if self.transport is None:
                        addresses = sorted(
                            {
                                r[4][0]
                                for r in socket.getaddrinfo(
                                    host, 443, type=socket.SOCK_STREAM
                                )
                            }
                        )
                        if not addresses or any(
                            not ipaddress.ip_address(a).is_global for a in addresses
                        ):
                            raise ImportWorkflowError(
                                "CLAUDE_UNSAFE_URL",
                                "Export resolves to a restricted network.",
                            )
                        request_url = request_url.copy_with(host=addresses[0])
                        headers["Host"] = host
                        extensions["sni_hostname"] = host
                    with client.stream(
                        "GET", request_url, headers=headers, extensions=extensions
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = urljoin(url, response.headers.get("location", ""))
                            redirect_host = validate_download_url(url, redirect=True)
                            redirect_path = httpx.URL(url).path.rstrip("/")
                            if redirect_host in {
                                "claude.ai",
                                "claude.com",
                            } and redirect_path in {
                                "/app-unavailable-in-region",
                                "/unsupported-country",
                            }:
                                raise ImportWorkflowError(
                                    "CLAUDE_REGION_UNAVAILABLE",
                                    "Claude returned a region-unavailable page. "
                                    "Upload ZIPs downloaded with supported access.",
                                )
                            continue
                        if response.status_code in {401, 403, 404, 410}:
                            raise ImportWorkflowError(
                                "CLAUDE_LINK_EXPIRED",
                                "Export link is unavailable. "
                                "Supply a fresh manifest or the downloaded ZIP.",
                            )
                        if response.status_code != 200:
                            raise ImportWorkflowError(
                                "CLAUDE_DOWNLOAD_FAILED",
                                f"Export server returned HTTP {response.status_code}.",
                            )
                        media_type = (
                            response.headers.get("content-type", "")
                            .split(";")[0]
                            .strip()
                            .lower()
                        )
                        if media_type in {"text/html", "application/xhtml+xml"}:
                            raise ImportWorkflowError(
                                "CLAUDE_EXPORT_NOT_AVAILABLE",
                                "The link returned a web page instead of a ZIP. "
                                "Obtain a fresh manifest or upload the ZIP.",
                            )
                        size = 0
                        with partial.open("wb") as handle:
                            for chunk in response.iter_bytes(256 * 1024):
                                if cancelled():
                                    raise ImportWorkflowError(
                                        "CLAUDE_CANCELLED",
                                        "Import cancelled; "
                                        "complete downloads were retained.",
                                    )
                                size += len(chunk)
                                if size > self.max_bytes:
                                    raise ImportWorkflowError(
                                        "IMPORT_SECURITY_REJECTED",
                                        "Export exceeds the download size limit.",
                                    )
                                handle.write(chunk)
                                progress(size)
                            handle.flush()
                            os.fsync(handle.fileno())
                        with zipfile.ZipFile(partial) as archive:
                            # The bounded parser checks CRC and resource limits.
                            if not archive.infolist():
                                raise zipfile.BadZipFile
                        digest = self.checksum(partial)
                        os.replace(partial, target)
                        write_private(marker, digest.encode())
                        return digest
                raise ImportWorkflowError(
                    "CLAUDE_UNSAFE_URL", "Too many export redirects."
                )
        except (httpx.HTTPError, OSError) as exc:
            raise ImportWorkflowError(
                "CLAUDE_DOWNLOAD_INTERRUPTED",
                "Export download interrupted; complete downloads were retained.",
            ) from exc
        except zipfile.BadZipFile as exc:
            raise ImportWorkflowError(
                "CLAUDE_INVALID_ZIP", "Downloaded export is not a readable ZIP."
            ) from exc
        finally:
            _download_context.reset(token)
            partial.unlink(missing_ok=True)
