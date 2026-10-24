"""The settings routes: the host's global settings, a project's settings, and the purge request.

- ``GET /api/settings``: ``{"data_root", "port", "log_level", "autostart": {"enabled", "available"}, "version"}``.
  ``data_root`` is the folder this host runs from; autostart is not available yet (a later milestone).
- ``PUT /api/settings`` with any of ``port`` (an integer from 1024 to 65535), ``log_level`` (``DEBUG``, ``INFO``,
  ``WARNING`` or ``ERROR``) and ``data_root`` (a folder ``validate_data_root`` accepts; refused with its reason
  otherwise): stores them in the catalog and answers the GET body plus ``restart_required``, true when the port or
  the data root changed. Both take effect at the next start; this host keeps its port and its data root. Other
  keys, such as the GET body's read-only ones, are ignored.
- ``GET /api/projects/{id}/settings``: ``reconcile_interval_s``, ``ocr_enabled``, ``ocr_engine_order``,
  ``export_dir`` and ``group_by_chain_default``, from the project's store.
- ``PUT /api/projects/{id}/settings`` with any of those: ``reconcile_interval_s`` an integer from 60 to 86400,
  ``ocr_enabled`` and ``group_by_chain_default`` booleans, ``ocr_engine_order`` a list of distinct engine names
  from ``windows`` and ``rapid``, ``export_dir`` null or an existing folder. Nothing is written unless the whole
  body is valid; a new reconcile interval applies from the supervisor's next timer. Answers the GET body.
- ``POST /api/purge``: mark the data root for removal once the host has stopped, and stop it; 202. The UI asks the
  user to confirm first; the launcher removes the folder after every store, the catalog and the lock are closed.

Errors answer ``{"error": "<message>"}`` with 400 for a bad body (an unknown project setting included), 404 for an
unknown project and 409 for a project with no running supervisor. No message names a path from the server.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from emailsupersearch import __version__
from emailsupersearch.host.routes_projects import (
    ApiError,
    read_json,
    services_of,
    supervisor_or_409,
)
from emailsupersearch.host.routes_search import json_object
from emailsupersearch.jobs.supervisor import DEFAULT_RECONCILE_INTERVAL_S
from emailsupersearch.paths import validate_data_root

if TYPE_CHECKING:
    from emailsupersearch.host.app import Services
    from emailsupersearch.jobs.supervisor import ProjectSupervisor

DEFAULT_PORT = 8765
PORT_MIN, PORT_MAX = 1024, 65535
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LOG_LEVEL = "INFO"
RECONCILE_MIN_S, RECONCILE_MAX_S = 60, 86400
OCR_ENGINES = ("windows", "rapid")

_BOOL_SETTINGS = ("ocr_enabled", "group_by_chain_default")


def _project_defaults() -> dict[str, Any]:
    """Each project setting with the value a project created by the catalog starts with."""
    return {
        "reconcile_interval_s": DEFAULT_RECONCILE_INTERVAL_S,
        "ocr_enabled": True,
        "ocr_engine_order": list(OCR_ENGINES),
        "export_dir": None,
        "group_by_chain_default": True,
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---- global settings ----


def _global_settings(services: Services) -> dict[str, Any]:
    catalog = services.catalog
    return {
        "data_root": str(catalog.root),
        "port": catalog.get_setting("port", DEFAULT_PORT),
        "log_level": catalog.get_setting("log_level", DEFAULT_LOG_LEVEL),
        "autostart": {"enabled": False, "available": False},
        "version": __version__,
    }


def get_settings(request: Request) -> JSONResponse:
    return JSONResponse(_global_settings(services_of(request)))


def put_settings(request: Request) -> JSONResponse:
    services = services_of(request)
    catalog = services.catalog
    body = json_object(read_json(request))
    updates: dict[str, Any] = {}
    if "port" in body:
        port = body["port"]
        if not _is_int(port) or not PORT_MIN <= port <= PORT_MAX:
            raise ApiError(400, f"port must be an integer from {PORT_MIN} to {PORT_MAX}")
        updates["port"] = port
    if "log_level" in body:
        if not isinstance(body["log_level"], str) or body["log_level"] not in LOG_LEVELS:
            raise ApiError(400, f"log_level must be one of {', '.join(LOG_LEVELS)}")
        updates["log_level"] = body["log_level"]
    if "data_root" in body:
        data_root = body["data_root"]
        if not isinstance(data_root, str) or not data_root.strip():
            raise ApiError(400, "data_root must be a non-empty string")
        reason = validate_data_root(Path(data_root))
        if reason is not None:
            raise ApiError(400, reason)  # the reason names the kind of folder refused, never the path
        updates["data_root"] = data_root
    restart_required = ("port" in updates and updates["port"] != catalog.get_setting("port", DEFAULT_PORT)) or (
        "data_root" in updates
        and Path(updates["data_root"]) != Path(catalog.get_setting("data_root", str(catalog.root)))
    )
    for key, value in updates.items():
        catalog.set_setting(key, value)
    return JSONResponse({**_global_settings(services), "restart_required": restart_required})


# ---- project settings ----


def _project_settings(sup: ProjectSupervisor) -> dict[str, Any]:
    return {key: sup.store.get_setting(key, default) for key, default in _project_defaults().items()}


def _check_project_setting(key: str, value: Any) -> None:
    """``ApiError(400)`` unless ``key`` is a project setting and ``value`` a valid value for it."""
    if key == "reconcile_interval_s":
        if not _is_int(value) or not RECONCILE_MIN_S <= value <= RECONCILE_MAX_S:
            raise ApiError(400, f"{key} must be an integer from {RECONCILE_MIN_S} to {RECONCILE_MAX_S}")
    elif key in _BOOL_SETTINGS:
        if not isinstance(value, bool):
            raise ApiError(400, f"{key} must be a boolean")
    elif key == "ocr_engine_order":
        if not (
            isinstance(value, list)
            and all(isinstance(engine, str) and engine in OCR_ENGINES for engine in value)
            and len(set(value)) == len(value)
        ):
            raise ApiError(400, f"{key} must be a list of distinct engines from {', '.join(OCR_ENGINES)}")
    elif key == "export_dir":
        if value is not None and not (isinstance(value, str) and os.path.isabs(value) and os.path.isdir(value)):
            raise ApiError(400, "export_dir is not an existing folder")
    else:
        raise ApiError(400, f"unknown setting: {key}")


def get_project_settings(request: Request) -> JSONResponse:
    sup = supervisor_or_409(services_of(request), request.path_params["project_id"])
    return JSONResponse(_project_settings(sup))


def put_project_settings(request: Request) -> JSONResponse:
    sup = supervisor_or_409(services_of(request), request.path_params["project_id"])
    body = json_object(read_json(request))
    for key, value in body.items():
        _check_project_setting(key, value)
    for key, value in body.items():
        sup.store.set_setting(key, value)
    interval = body.get("reconcile_interval_s")
    if interval is not None and interval != sup.reconcile_interval_s:
        sup.set_reconcile_interval(interval)
    return JSONResponse(_project_settings(sup))


# ---- purge ----


def purge(request: Request) -> JSONResponse:
    services = services_of(request)
    services.purge_requested = True
    services.request_shutdown()
    return JSONResponse({"accepted": True}, status_code=202)


def routes() -> list[Route]:
    project = "/api/projects/{project_id}/settings"
    return [
        Route("/api/settings", get_settings, methods=["GET"]),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route(project, get_project_settings, methods=["GET"]),
        Route(project, put_project_settings, methods=["PUT"]),
        Route("/api/purge", purge, methods=["POST"]),
    ]
