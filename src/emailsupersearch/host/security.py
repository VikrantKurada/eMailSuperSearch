"""The host's security layer (spec 14.1): launch token, sessions, CSRF and the request checks in front of every route.

The launcher opens the browser at ``/auth?token=<launch token>``; the host answers with an ``HttpOnly`` session cookie
and a readable CSRF cookie. Every ``/api/*`` request needs the session cookie, and every one that is not a GET or
HEAD also needs the CSRF value echoed in the ``X-ESS-Token`` header. The Host header must name a loopback address
(this defeats DNS rebinding) and an ``Origin``, when present, must be a loopback one. ``/api/shutdown`` also accepts
the launch token in ``X-ESS-Launch``, for the ``stop`` command, which has no browser session.

Nothing here logs a token or a cookie value.
"""

from __future__ import annotations

import logging
import secrets
import threading
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

log = logging.getLogger(__name__)

SESSION_COOKIE = "ess_session"
CSRF_COOKIE = "ess_csrf"
CSRF_HEADER = "X-ESS-Token"
LAUNCH_HEADER = "X-ESS-Launch"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
_EXEMPT_PATHS = {"/auth", "/health"}
_SAFE_METHODS = {"GET", "HEAD"}


def new_token() -> str:
    """A fresh unguessable token: 32 random bytes, URL-safe base64."""
    return secrets.token_urlsafe(32)


def same_secret(given: str | None, expected: str) -> bool:
    """Whether ``given`` equals ``expected``, compared in constant time; a missing value never matches."""
    if not given:
        return False
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _host_without_port(value: str) -> str:
    """The host part of a ``Host`` header or URL authority, lower-cased; ``[::1]`` keeps its brackets."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value if end == -1 else value[: end + 1]
    return value.rsplit(":", 1)[0] if ":" in value else value


class SessionStore:
    """Browser sessions for the life of the host process: session id -> CSRF value. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, str] = {}

    def issue(self) -> tuple[str, str]:
        """Start a session; returns ``(session id, csrf value)``."""
        session_id, csrf = new_token(), new_token()
        with self._lock:
            self._sessions[session_id] = csrf
        return session_id, csrf

    def csrf_for(self, session_id: str) -> str | None:
        """The CSRF value of a live session, or None when ``session_id`` is unknown."""
        with self._lock:
            return self._sessions.get(session_id)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Refuses what does not pass the checks in the module docstring and adds the security headers to every response.

    Order: Host (400), Origin (403), then for ``/api/*`` only: the launch header on ``/api/shutdown``, the session
    cookie (401) and, for methods other than GET and HEAD, the CSRF header (401).
    """

    def __init__(self, app: ASGIApp, launch_token: str, sessions: SessionStore) -> None:
        super().__init__(app)
        self.launch_token = launch_token
        self.sessions = sessions

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        refusal = self._refusal(request)
        response = refusal if refusal is not None else await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    def _refusal(self, request: Request) -> Response | None:
        """The error response for a request that fails a check, or None when it may proceed."""
        if _host_without_port(request.headers.get("host", "")) not in LOOPBACK_HOSTS:
            return _refuse(400, "bad host", request)
        origin = request.headers.get("origin")
        if origin is not None and _host_without_port(urlsplit(origin).netloc) not in LOOPBACK_HOSTS:
            return _refuse(403, "foreign origin", request)
        path = request.url.path
        if path in _EXEMPT_PATHS or not path.startswith("/api/"):
            return None
        if path == "/api/shutdown" and same_secret(request.headers.get(LAUNCH_HEADER), self.launch_token):
            return None
        session_id = request.cookies.get(SESSION_COOKIE)
        csrf = self.sessions.csrf_for(session_id) if session_id else None
        if csrf is None:
            return _refuse(401, "no session", request)
        if request.method not in _SAFE_METHODS and not same_secret(request.headers.get(CSRF_HEADER), csrf):
            return _refuse(401, "bad csrf", request)
        return None


def _refuse(status: int, message: str, request: Request) -> JSONResponse:
    log.debug("refused %s request: %s", request.method, message)
    return JSONResponse({"error": message}, status_code=status)
