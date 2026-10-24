"""The status and project routes, plus the small JSON helpers the other route modules share.

Endpoints are plain ``def`` functions, which Starlette runs in its threadpool: they may block on the catalog, a store
or a supervisor's ``stop``. Errors answer ``{"error": "<message>"}``: 400 for a body that is not valid JSON or has
fields of the wrong type, 404 for an unknown project, 409 for a project that has no running supervisor (blocked).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio.from_thread
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from emailsupersearch import __version__
from emailsupersearch.index.embedder import MODEL_DIM
from emailsupersearch.ingest.ocr.base import select_engine
from emailsupersearch.ingest.watcher import is_network_path

if TYPE_CHECKING:
    from emailsupersearch.catalog import Project
    from emailsupersearch.host.app import Services
    from emailsupersearch.jobs.supervisor import ProjectSupervisor

_REQUIRED = object()
_ocr_lock = threading.Lock()
_ocr_engine_found: bool | None = None  # select_engine("en") is slow the first time: asked once per process


class ApiError(Exception):
    """Raised by an endpoint to answer ``{"error": message}`` with ``status``; the app renders it."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def json_error(status: int, message: str) -> JSONResponse:
    """The error response ``{"error": message}`` with ``status``."""
    return JSONResponse({"error": message}, status_code=status)


def project_dict(project: Project) -> dict[str, Any]:
    """A ``Project`` as JSON-ready data, its directory as a string."""
    return {"id": project.id, "name": project.name, "created_at": project.created_at, "dir": str(project.dir)}


def services_of(request: Request) -> Services:
    """The ``Services`` the app was created with."""
    return request.app.state.services


def read_json(request: Request) -> Any:
    """The request's JSON body, from a plain ``def`` endpoint; ``ApiError(400)`` when it is not valid JSON.

    The body is read on the event loop through anyio's portal, which exists in Starlette's threadpool threads.
    """
    body = anyio.from_thread.run(request.body)
    try:
        return json.loads(body)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError both are
        raise ApiError(400, "the body is not valid JSON") from None


def project_or_404(services: Services, project_id: str) -> Project:
    project = services.catalog.get(project_id)
    if project is None:
        raise ApiError(404, "no such project")
    return project


def supervisor_or_409(services: Services, project_id: str) -> ProjectSupervisor:
    """The project's running supervisor; 404 for an unknown project, 409 when it has none (it is blocked)."""
    project_or_404(services, project_id)
    sup = services.supervisors.get(project_id)
    if sup is None:
        reason = services.blocked_projects.get(project_id)
        raise ApiError(409, f"the project is not open: {reason}" if reason else "the project is not open")
    return sup


def _object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(400, "the body must be a JSON object")
    return body


def _field(body: dict[str, Any], key: str, kind: type, default: Any = _REQUIRED) -> Any:
    """``body[key]`` checked to be a ``kind``; ``default`` when absent (400 when there is none)."""
    if key not in body:
        if default is _REQUIRED:
            raise ApiError(400, f"{key} is required")
        return default
    value = body[key]
    if not isinstance(value, kind):
        raise ApiError(400, f"{key} must be of type {kind.__name__}")
    return value


def _is_folder(value: Any) -> bool:
    """Whether ``value`` is an absolute path string naming an existing directory."""
    return isinstance(value, str) and os.path.isabs(value) and os.path.isdir(value)


def _ocr_available() -> bool:
    global _ocr_engine_found
    with _ocr_lock:
        if _ocr_engine_found is None:
            _ocr_engine_found = select_engine("en") is not None
        return _ocr_engine_found


def _project_id(request: Request) -> str:
    return request.path_params["project_id"]


# ---- status ----


def status(request: Request) -> JSONResponse:
    services = services_of(request)
    degraded = []
    if services.embedder is None:
        degraded.append("embedder")
    if not _ocr_available():
        degraded.append("ocr")
    projects = []
    for project in services.catalog.list_projects():
        sup = services.supervisors.get(project.id)
        projects.append(
            {
                "id": project.id,
                "name": project.name,
                "created_at": project.created_at,
                "sync": asdict(sup.status()) if sup is not None else None,
                "blocked": services.blocked_projects.get(project.id),
            }
        )
    return JSONResponse({"version": __version__, "degraded": degraded, "projects": projects})


# ---- projects ----


def list_projects(request: Request) -> JSONResponse:
    return JSONResponse([project_dict(p) for p in services_of(request).catalog.list_projects()])


def create_project(request: Request) -> JSONResponse:
    services = services_of(request)
    body = _object(read_json(request))
    name = _field(body, "name", str)
    folders = _field(body, "folders", list)
    language = _field(body, "language", str, "en")
    for index, folder in enumerate(folders):
        if not _is_folder(folder):
            raise ApiError(400, f"folders[{index}] is not an existing folder")
    embedder = services.embedder
    try:
        project = services.catalog.create_project(
            name,
            [Path(folder) for folder in folders],
            language,
            embedder_id=embedder.id if embedder is not None else None,
            embedding_dim=MODEL_DIM if embedder is not None else None,
        )
    except ValueError as e:  # a blank name
        raise ApiError(400, str(e)) from None
    sup = services.start_supervisor(project.id)
    services.supervisors[project.id] = sup
    sup.start()
    return JSONResponse(project_dict(project), status_code=201)


def get_project(request: Request) -> JSONResponse:
    services = services_of(request)
    project = project_or_404(services, _project_id(request))
    sup = services.supervisors.get(project.id)
    folders = sup.store.list_folders() if sup is not None else []
    return JSONResponse({**project_dict(project), "folders": folders})


def rename_project(request: Request) -> JSONResponse:
    services = services_of(request)
    project_id = project_or_404(services, _project_id(request)).id
    name = _field(_object(read_json(request)), "name", str, None)
    if name is not None:
        try:
            services.catalog.rename(project_id, name)
        except ValueError as e:  # a blank name
            raise ApiError(400, str(e)) from None
        except KeyError:
            raise ApiError(404, "no such project") from None
    return JSONResponse(project_dict(project_or_404(services, project_id)))


def delete_project(request: Request) -> Response:
    services = services_of(request)
    project_id = project_or_404(services, _project_id(request)).id
    sup = services.supervisors.pop(project_id, None)
    if sup is not None:
        sup.stop()  # closes the project's store, so Windows lets the directory go
    try:
        deleted = services.catalog.delete_project(project_id)
    except PermissionError:
        return json_error(409, "project files are still in use; retry")
    if not deleted:
        raise ApiError(404, "no such project")
    services.blocked_projects.pop(project_id, None)
    return Response(status_code=204)


# ---- folders ----


def add_folder(request: Request) -> JSONResponse:
    sup = supervisor_or_409(services_of(request), _project_id(request))
    path = _field(_object(read_json(request)), "path", str)
    if not _is_folder(path):
        raise ApiError(400, "path is not an existing folder")
    sup.store.add_folder(os.path.normpath(path), "network" if is_network_path(Path(path)) else "local")
    return _folders_changed(sup)


def remove_folder(request: Request) -> JSONResponse:
    sup = supervisor_or_409(services_of(request), _project_id(request))
    path = _field(_object(read_json(request)), "path", str)
    sup.store.remove_folder(path)
    return _folders_changed(sup)


def _folders_changed(sup: ProjectSupervisor) -> JSONResponse:
    """Watch the new folder set, rescan, and answer with the folders."""
    sup.refresh_watchers()
    sup.request_rescan()
    return JSONResponse({"folders": sup.store.list_folders()})


# ---- saved searches ----


def get_saved_searches(request: Request) -> JSONResponse:
    sup = supervisor_or_409(services_of(request), _project_id(request))
    return JSONResponse(sup.store.get_setting("saved_searches", []))


def put_saved_searches(request: Request) -> JSONResponse:
    sup = supervisor_or_409(services_of(request), _project_id(request))
    searches = read_json(request)
    if not _valid_saved_searches(searches):
        raise ApiError(400, "saved searches must be a list of {name, filters} objects")
    sup.store.set_setting("saved_searches", searches)
    return JSONResponse(searches)


def _valid_saved_searches(value: Any) -> bool:
    """Whether ``value`` is a list of ``{"name": str, "filters": object}``."""
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and item.keys() == {"name", "filters"}
        and isinstance(item["name"], str)
        and isinstance(item["filters"], dict)
        for item in value
    )


def routes() -> list[Route]:
    project = "/api/projects/{project_id}"
    return [
        Route("/api/status", status, methods=["GET"]),
        Route("/api/projects", list_projects, methods=["GET"]),
        Route("/api/projects", create_project, methods=["POST"]),
        Route(project, get_project, methods=["GET"]),
        Route(project, rename_project, methods=["PATCH"]),
        Route(project, delete_project, methods=["DELETE"]),
        Route(f"{project}/folders", add_folder, methods=["POST"]),
        Route(f"{project}/folders", remove_folder, methods=["DELETE"]),
        Route(f"{project}/saved-searches", get_saved_searches, methods=["GET"]),
        Route(f"{project}/saved-searches", put_saved_searches, methods=["PUT"]),
    ]
