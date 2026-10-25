"""The Excel export routes: write the results (or the pinned messages) to a workbook, and download one back.

- ``POST /api/projects/{id}/export/excel`` with ``{"filters": {...}, "pinned_keys": [...], "pinned_chain_ids": [...]}``
  (both lists optional): when either list is non-empty the pinned messages and every live message of the pinned
  chains are exported, newest first; otherwise the results of the filters, ungrouped, up to ``EXPORT_MAX_ROWS``: with
  an empty query every matching message, with a query its top matches (``query_matches``, ``K_EXPORT`` messages per
  retriever). Answers ``{"path", "name", "rows", "truncated"}``; ``truncated`` says that more emails match than the
  workbook holds (the row limit was reached, or a retriever came back full).
- ``GET /api/projects/{id}/exports/{name}``: the workbook ``name``, which must look exactly like a name this module
  makes (``export-YYYYMMDD-HHMMSS[-N].xlsx``), from the same export folder.

The export folder is the project setting ``export_dir`` when it names an existing directory, else the project's own
``exports`` folder. Files are named by the UTC time, with ``-N`` added when that name is taken. Reads run on the
store's per-thread read connection; the client never supplies a path.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from emailsupersearch.export.excel import EXPORT_MAX_ROWS, export_results
from emailsupersearch.host.routes_projects import (
    ApiError,
    project_or_404,
    read_json,
    services_of,
    supervisor_or_409,
)
from emailsupersearch.host.routes_search import json_object, parse_filters, str_list
from emailsupersearch.search.hybrid import (
    K_EXPORT,
    cards_for_keys,
    chain_member_keys,
    query_matches,
    search,
)

if TYPE_CHECKING:
    import sqlite3

    from emailsupersearch.catalog import Project
    from emailsupersearch.search.results import MessageCard

_EXPORT_NAME = re.compile(r"export-\d{8}-\d{6}(-\d+)?\.xlsx", re.ASCII)  # always used with fullmatch
_SETTING_SQL = "SELECT value FROM settings WHERE key = ?"
_CHAIN_SUBJECTS_SQL = "SELECT chain_id, subject FROM chains WHERE chain_id IN (SELECT value FROM json_each(?))"
_export_lock = threading.Lock()  # choosing a free name and writing it are one step


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _export_dir(conn: sqlite3.Connection, project: Project) -> Path:
    """The ``export_dir`` setting when it is an existing absolute directory, else ``<project dir>/exports``."""
    row = conn.execute(_SETTING_SQL, ("export_dir",)).fetchone()
    try:
        value = json.loads(row[0]) if row is not None else None
    except (TypeError, ValueError):  # a NULL or a value that is not JSON: no setting
        value = None
    if isinstance(value, str) and os.path.isabs(value) and os.path.isdir(value):
        return Path(value)
    return project.dir / "exports"


def _free_name(directory: Path, now: datetime) -> str:
    stem = now.strftime("export-%Y%m%d-%H%M%S")
    name, n = f"{stem}.xlsx", 0
    while (directory / name).exists():
        n += 1
        name = f"{stem}-{n}.xlsx"
    return name


def _newest_first(cards: list[MessageCard]) -> list[MessageCard]:
    return sorted(cards, key=lambda c: (c.sent_ts is None, -(c.sent_ts or 0)))


def export_excel(request: Request) -> JSONResponse:
    services = services_of(request)
    project_id = request.path_params["project_id"]
    project = project_or_404(services, project_id)
    sup = supervisor_or_409(services, project_id)
    body = json_object(read_json(request))
    filters = parse_filters(json_object(body.get("filters", {}), "filters"))
    pinned_keys = str_list(body, "pinned_keys")
    pinned_chain_ids = str_list(body, "pinned_chain_ids")
    conn = sup.store.read_conn()
    if pinned_keys or pinned_chain_ids:
        keys = list(pinned_keys)
        for chain_id in dict.fromkeys(pinned_chain_ids):
            keys += chain_member_keys(conn, chain_id)
        messages = _newest_first(cards_for_keys(conn, keys))
        total, capped = len(messages), False
    elif filters.query.strip():
        messages, capped = query_matches(sup.store, services.embedder, filters, k_each=K_EXPORT)
        total = len(messages)
    else:
        unpaged = replace(filters, group_by_chain=False)
        response = search(sup.store, services.embedder, unpaged, page=0, page_size=EXPORT_MAX_ROWS)
        messages, total, capped = response.messages, response.total_messages, False
    chain_ids = sorted({card.chain_id for card in messages if card.chain_id})
    chain_subjects = {
        chain_id: subject
        for chain_id, subject in conn.execute(_CHAIN_SUBJECTS_SQL, (json.dumps(chain_ids),)).fetchall()
        if subject
    }
    with _export_lock:
        directory = _export_dir(conn, project)
        directory.mkdir(parents=True, exist_ok=True)
        name = _free_name(directory, _utcnow())
        try:
            info = export_results(messages, directory / name, chain_subjects=chain_subjects)
        except PermissionError:
            raise ApiError(409, "the export file is open in another program; close it and retry") from None
    result: dict[str, Any] = {
        "path": str(info.path),
        "name": name,
        "rows": info.rows,
        "truncated": info.truncated or capped or total > info.rows,
    }
    return JSONResponse(result)


def download_export(request: Request) -> FileResponse:
    services = services_of(request)
    project_id = request.path_params["project_id"]
    project = project_or_404(services, project_id)
    sup = supervisor_or_409(services, project_id)
    name = request.path_params["name"]
    if _EXPORT_NAME.fullmatch(name) is None:
        raise ApiError(404, "no such export")
    path = _export_dir(sup.store.read_conn(), project) / name
    if not path.is_file():
        raise ApiError(404, "no such export")
    return FileResponse(path, filename=name)


def routes() -> list[Route]:
    project = "/api/projects/{project_id}"
    return [
        Route(f"{project}/export/excel", export_excel, methods=["POST"]),
        Route(f"{project}/exports/{{name}}", download_export, methods=["GET"]),
    ]
