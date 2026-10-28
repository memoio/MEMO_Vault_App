"""Local owner bootstrap, short-lived HTTP-only sessions and CSRF protection."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware

from everos.component.utils.datetime import get_utc_now

COOKIE = "memo_vault_owner"
DEFAULT_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8765",
    "http://localhost:8765",
]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=256)


class OwnerSecurity:
    def __init__(
        self, data_dir: Path, *, allowed_origins: list[str] | None = None
    ) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.bootstrap_path = data_dir / "owner-bootstrap.token"
        try:
            fd = os.open(
                self.bootstrap_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            fd = os.open(self.bootstrap_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                self.secret = stream.read().strip()
            if not self.secret.startswith("mvo_") or len(self.secret) < 40:
                raise ValueError("Invalid owner bootstrap credential") from None
        else:
            self.secret = "mvo_" + secrets.token_urlsafe(32)
            with os.fdopen(fd, "w") as stream:
                stream.write(self.secret + "\n")
        self.allowed_origins = allowed_origins or os.environ.get(
            "MEMO_VAULT_UI_ORIGINS", ",".join(DEFAULT_ORIGINS)
        ).split(",")
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1"} | {
            urlsplit(o).hostname for o in self.allowed_origins
        }
        self.sessions: dict[str, float] = {}
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def origin_allowed(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        return (
            request.url.hostname in self.allowed_hosts
            and (not origin or origin in self.allowed_origins)
            and request.headers.get("sec-fetch-site") != "cross-site"
        )

    def csrf(self, cookie: str) -> str:
        return hmac.new(
            self.secret.encode(), cookie.encode(), hashlib.sha256
        ).hexdigest()

    def authenticated(self, request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if auth:
            return hmac.compare_digest(auth, "Bearer " + self.secret)
        cookie = request.cookies.get(COOKIE, "")
        return (
            self.sessions.get(hashlib.sha256(cookie.encode()).hexdigest(), 0)
            > get_utc_now().timestamp()
        )

    def check(self, request: Request) -> int | None:
        if not self.origin_allowed(request):
            return 403
        if not self.authenticated(request):
            return 401
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and not request.headers.get("authorization")
            and not hmac.compare_digest(
                request.headers.get("x-vault-csrf", ""),
                self.csrf(request.cookies.get(COOKIE, "")),
            )
        ):
            return 403
        return None


class OwnerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        security = getattr(request.app.state, "owner_security", None)
        if request.url.path != "/api/v1/auth/session":
            status = security.check(request) if security is not None else 503
            if status:
                return JSONResponse(
                    {
                        "detail": {
                            "code": "OWNER_AUTH_REQUIRED",
                            "message": "Owner authentication required.",
                        }
                    },
                    status_code=status,
                    headers={"Cache-Control": "no-store"},
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


def install_owner_security(app: FastAPI) -> None:
    app.add_middleware(OwnerMiddleware)

    @app.get("/api/v1/auth/session")
    async def session(request: Request):
        security = getattr(request.app.state, "owner_security", None)
        if security is None or not security.origin_allowed(request):
            return JSONResponse(
                {"authenticated": False}, status_code=503 if security is None else 403
            )
        authenticated = security.authenticated(request)
        return JSONResponse(
            {
                "authenticated": authenticated,
                "csrf_token": security.csrf(request.cookies.get(COOKIE, ""))
                if authenticated
                else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v1/auth/session")
    async def login(body: LoginRequest, request: Request):
        security = getattr(request.app.state, "owner_security", None)
        if security is None or not security.origin_allowed(request):
            return JSONResponse({"detail": "access denied"}, status_code=403)
        now = get_utc_now().timestamp()
        address = request.client.host if request.client else "local"
        attempts = security.attempts[address]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            return JSONResponse({"detail": "Too many attempts"}, status_code=429)
        attempts.append(now)
        if not hmac.compare_digest(body.token, security.secret):
            return JSONResponse({"detail": "access denied"}, status_code=401)
        security.sessions = {
            key: expiry for key, expiry in security.sessions.items() if expiry > now
        }
        if len(security.sessions) >= 64:
            security.sessions.pop(next(iter(security.sessions)))
        cookie = secrets.token_urlsafe(32)
        security.sessions[hashlib.sha256(cookie.encode()).hexdigest()] = now + 8 * 3600
        response = JSONResponse(
            {"authenticated": True, "csrf_token": security.csrf(cookie)},
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            COOKIE,
            cookie,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=8 * 3600,
        )
        return response
