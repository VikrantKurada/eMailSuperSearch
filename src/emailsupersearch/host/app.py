"""The host's Starlette app: the services it runs on, the auth/health/shutdown endpoints and the route modules.

``create_app`` wires ``SecurityMiddleware`` in front of everything (see ``security``), attaches the event bus to the
running loop at startup, and serves the built UI bundle from ``static_dir`` last, so every API route wins over it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from emailsupersearch.host import (
    routes_export,
    routes_messages,
    routes_projects,
    routes_search,
    routes_settings,
    routes_sync,
)
from emailsupersearch.host.routes_projects import ApiError, json_error, services_of
from emailsupersearch.host.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SecurityMiddleware,
    SessionStore,
    same_secret,
)

if TYPE_CHECKING:
    from emailsupersearch.catalog import Catalog
    from emailsupersearch.host.sse import EventBus
    from emailsupersearch.index.embedder import OnnxEmbedder
    from emailsupersearch.ingest.chunker import Chunker
    from emailsupersearch.jobs.supervisor import ProjectSupervisor

_NO_BUNDLE = "eMailSuperSearch host is running; the UI bundle is not built."


@dataclass
class Services:
    """Everything the routes need. ``blocked_projects`` maps a project id to why it has no supervisor (say,
    ``"schema too new"``); ``purge_requested`` asks the launcher to remove the data root on exit."""

    catalog: Catalog
    embedder: OnnxEmbedder | None
    chunker: Chunker
    supervisors: dict[str, ProjectSupervisor]
    bus: EventBus
    launch_token: str
    sessions: SessionStore
    start_supervisor: Callable[[str], ProjectSupervisor]
    static_dir: Path | None
    request_shutdown: Callable[[], None]
    blocked_projects: dict[str, str]
    purge_requested: bool = False


def auth(request: Request) -> Response:
    """Exchange the launch token for a session: set the session and CSRF cookies and go to the UI."""
    services = services_of(request)
    if not same_secret(request.query_params.get("token"), services.launch_token):
        return json_error(403, "bad launch token")
    session_id, csrf = services.sessions.issue()
    response = RedirectResponse("/", status_code=303)
    # Starlette writes ``samesite`` verbatim; "Strict" is the attribute's canonical spelling. No ``Secure``: the
    # origin is plain http on loopback.
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="Strict", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, samesite="Strict", path="/")
    return response


def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def shutdown(request: Request) -> Response:
    services_of(request).request_shutdown()
    return Response(status_code=204)


def no_bundle(request: Request) -> PlainTextResponse:
    return PlainTextResponse(_NO_BUNDLE)


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return json_error(exc.status, exc.message)


def create_app(services: Services) -> Starlette:
    """The host app over ``services``; see the module docstring."""

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        services.bus.attach_loop(asyncio.get_running_loop())
        yield

    routes: list[BaseRoute] = [
        Route("/auth", auth, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/api/shutdown", shutdown, methods=["POST"]),
        *routes_projects.routes(),
        *routes_search.routes(),
        *routes_messages.routes(),
        *routes_export.routes(),
        *routes_sync.routes(),
        *routes_settings.routes(),
    ]
    if services.static_dir is not None and services.static_dir.is_dir():
        routes.append(Mount("/", app=StaticFiles(directory=services.static_dir, html=True)))
    else:
        routes.append(Route("/", no_bundle, methods=["GET"]))
    app = Starlette(
        routes=routes,
        middleware=[Middleware(SecurityMiddleware, launch_token=services.launch_token, sessions=services.sessions)],
        exception_handlers={ApiError: _api_error},
        lifespan=lifespan,
    )
    app.state.services = services
    return app
