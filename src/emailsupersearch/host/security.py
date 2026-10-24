"""The host's security layer (spec 14.1): launch token, sessions, CSRF and the request checks in front of every route.

The launcher opens the browser at ``/auth?token=<launch token>``; the host answers with an ``HttpOnly`` session cookie
and a readable CSRF cookie. Every ``/api/*`` request needs the session cookie, and every one that is not a GET or
HEAD also needs the CSRF value echoed in the ``X-ESS-Token`` header. The Host header must be a loopback name with an
optional port (this defeats DNS rebinding), and an ``Origin``, when present, must be ``http://`` plus exactly that
Host value (same host, same port). ``/api/shutdown`` also accepts the launch token in ``X-ESS-Launch``, for the
``stop`` command, which has no browser session.

Nothing here logs a token, a cookie value or a request body.
"""

from __future__ import annotations

import logging
import re
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
# A whole Host header: one of LOOPBACK_HOSTS, then an optional port. Anything else is refused.
_HOST_RE = re.compile(
    "(?:" + "|".join(re.escape(h) for h in sorted(LOOPBACK_HOSTS)) + r")(?::\d{1,5})?", re.IGNORECASE
)


def new_token() -> str:
    """A fresh unguessable token: 32 random bytes, URL-safe base64."""
    return secrets.token_urlsafe(32)


def same_secret(given: str | None, expected: str) -> bool:
    """Whether ``given`` equals ``expected``, compared in constant time; a missing value never matches."""
    if not given:
        return False
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _host_ok(host: str) -> bool:
    """Whether a Host header value is a loopback name with an optional port, and nothing more."""
    return _HOST_RE.fullmatch(host) is not None


def _origin_ok(origin: str, host: str) -> bool:
    """Whether ``origin`` is ``http://<host>`` for this request's Host value: same host and port, no path."""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.netloc.lower() == host.lower()
        and not parts.path
        and not parts.query
        and not parts.fragment
    )


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
    cookie (401) and, for methods other than GET and HEAD, the CSRF header (401). Paths are the ASGI ``scope["path"]``
    the router matches on. An exception escaping an endpoint becomes a JSON 500, so it carries the headers too.
    """

    def __init__(self, app: ASGIApp, launch_token: str, sessions: SessionStore) -> None:
        super().__init__(app)
        self.launch_token = launch_token
        self.sessions = sessions

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = self._refusal(request)
        if response is None:
            try:
                response = await call_next(request)
            except Exception as e:
                endpoint = request.scope.get("endpoint")
                route = getattr(endpoint, "__name__", "(no route)")
                log.warning("%s %s failed: %s", request.method, route, type(e).__name__)
                log.debug("%s %s failed", request.method, route, exc_info=True)
                response = JSONResponse({"error": "internal error"}, status_code=500)
        response.headers.update(_SECURITY_HEADERS)
        return response

    def _refusal(self, request: Request) -> Response | None:
        """The error response for a request that fails a check, or None when it may proceed."""
        host = request.headers.get("host", "")
        if not _host_ok(host):
            return _refuse(400, "bad host", request)
        origin = request.headers.get("origin")
        if origin is not None and not _origin_ok(origin, host):
            return _refuse(403, "foreign origin", request)
        path = request.scope["path"]
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
