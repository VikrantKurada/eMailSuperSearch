"""The sync routes: trigger a rescan, read the sync status, stream sync progress, and the native folder picker.

- ``POST /api/projects/{id}/sync``: ask the project's supervisor for a rescan; 202 ``{"accepted": true}``.
- ``GET /api/projects/{id}/sync/status``: the supervisor's ``SyncStatus``.
- ``GET /api/projects/{id}/sync/events``: a ``text/event-stream`` that first sends the current ``SyncStatus`` as a
  ``status`` event, then every event the supervisor publishes on the bus topic ``sync:<id>``, named by its
  ``type`` and carrying ``public_event(event)`` as JSON data: never a path or an error text. The stream subscribes
  before it reads the status, so nothing published in between is lost, and it closes the subscription however it
  ends, a client disconnecting included. sse-starlette sends a keepalive comment every 15 s.
- ``POST /api/pick-folder``: show the native folder dialog on the host's desktop; ``{"path": <folder> | null}``,
  null when it was cancelled or could not be shown.

Errors are those of ``routes_projects``: 404 for an unknown project, 409 for one with no running supervisor.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from sse_starlette import EventSourceResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from emailsupersearch.host.folder_picker import pick_folder
from emailsupersearch.host.routes_projects import services_of, supervisor_or_409

if TYPE_CHECKING:
    from emailsupersearch.host.sse import EventBus
    from emailsupersearch.jobs.supervisor import ProjectSupervisor

PING_INTERVAL_S = 15

_PUBLIC_FIELDS = frozenset({"type", "status", "message_key", "scan_id"})


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    """The part of a supervisor or pipeline event a progress stream may carry: ``type``, ``status``,
    ``message_key``, ``scan_id`` and every integer field (the counts). Everything else, the ``path``, ``old`` and
    ``new`` of a file event and any error text above all, is dropped."""
    return {
        key: value
        for key, value in event.items()
        if key in _PUBLIC_FIELDS or (isinstance(value, int) and not isinstance(value, bool))
    }


def _project_id(request: Request) -> str:
    return request.path_params["project_id"]


def trigger_sync(request: Request) -> JSONResponse:
    sup = supervisor_or_409(services_of(request), _project_id(request))
    sup.request_rescan()
    return JSONResponse({"accepted": True}, status_code=202)


def sync_status(request: Request) -> JSONResponse:
    sup = supervisor_or_409(services_of(request), _project_id(request))
    return JSONResponse(asdict(sup.status()))


async def sync_events(request: Request) -> EventSourceResponse:
    services = services_of(request)
    project_id = _project_id(request)
    sup = await run_in_threadpool(supervisor_or_409, services, project_id)  # the catalog lookup may block
    return EventSourceResponse(_sync_event_stream(services.bus, sup, f"sync:{project_id}"), ping=PING_INTERVAL_S)


async def _sync_event_stream(
    bus: EventBus, sup: ProjectSupervisor, topic: str
) -> AsyncIterator[dict[str, str]]:
    """The ``status`` event, then the topic's events, as sse-starlette event dicts; see the module docstring."""
    sub = bus.subscribe(topic)  # before the status is read, so an event published meanwhile is still delivered
    try:
        status = await run_in_threadpool(sup.status)  # it reads the store: keep the event loop free
        yield {"event": "status", "data": json.dumps(asdict(status))}
        async for event in sub:
            public = public_event(event)
            yield {"event": public.get("type", "event"), "data": json.dumps(public)}
    finally:
        await sub.aclose()


def pick_folder_route(request: Request) -> JSONResponse:
    path = pick_folder()  # blocks this threadpool worker until the dialog closes
    return JSONResponse({"path": str(path) if path is not None else None})


def routes() -> list[Route]:
    project = "/api/projects/{project_id}"
    return [
        Route(f"{project}/sync", trigger_sync, methods=["POST"]),
        Route(f"{project}/sync/status", sync_status, methods=["GET"]),
        Route(f"{project}/sync/events", sync_events, methods=["GET"]),
        Route("/api/pick-folder", pick_folder_route, methods=["POST"]),
    ]
