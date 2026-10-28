"""Bound request bytes before JSON decoding, including chunked HTTP bodies."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = 65536) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        try:
            async with asyncio.timeout(5):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.max_bytes:
                        await JSONResponse(
                            {"detail": "Request too large"}, status_code=413
                        )(scope, receive, send)
                        return
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "Request timeout"}, status_code=408)(
                scope, receive, send
            )
            return
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": b"".join(chunks),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, bounded_receive, send)


class _LocalUploadTooLargeError(Exception):
    pass


class ClaudeLocalUploadLimitMiddleware:
    """Limit local ZIP multipart bytes before Starlette spools UploadFile parts."""

    def __init__(self, app: ASGIApp, overhead_bytes: int = 4 * 1024 * 1024) -> None:
        self.app = app
        self.overhead_bytes = overhead_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/api/v1/import-jobs/local"
        ):
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        manager = getattr(getattr(app, "state", None), "import_jobs", None)
        if manager is None:
            await self.app(scope, receive, send)
            return
        maximum = manager.max_total_bytes + self.overhead_bytes
        headers = dict(scope.get("headers", ()))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > maximum:
            await JSONResponse({"detail": "Upload too large"}, status_code=413)(
                scope, receive, send
            )
            return
        used = 0
        response_started = False
        limit_exceeded = False

        async def limited_receive() -> Message:
            nonlocal used, limit_exceeded
            message = await receive()
            if message["type"] == "http.request":
                used += len(message.get("body", b""))
                if used > maximum:
                    limit_exceeded = True
                    raise _LocalUploadTooLargeError
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if limit_exceeded:
                # Starlette may turn receive errors into a multipart 400.
                # Suppress that response and return the correct quota status.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        with suppress(_LocalUploadTooLargeError):
            await self.app(scope, limited_receive, tracked_send)
        if limit_exceeded:
            if response_started:
                raise _LocalUploadTooLargeError
            await JSONResponse({"detail": "Upload too large"}, status_code=413)(
                scope, receive, send
            )
