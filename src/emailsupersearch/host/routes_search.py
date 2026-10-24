"""The search route, and ``parse_filters``, which turns a JSON object into ``SearchFilters`` for it and the export.

``POST /api/projects/{id}/search`` takes the ``SearchFilters`` fields, each optional, plus ``page`` (from 0) and
``page_size`` (1 to 100), and answers one ``SearchResponse`` page as JSON. Unknown keys are ignored; a known key with
a value of the wrong type is a 400. Searches read on the store's per-thread read connection (``search`` does).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from emailsupersearch.host.routes_projects import (
    ApiError,
    read_json,
    services_of,
    supervisor_or_409,
)
from emailsupersearch.search.hybrid import PAGE_SIZE, search
from emailsupersearch.types import SearchFilters

MAX_PAGE_SIZE = 100

_LIST_FIELDS = ("from_addrs", "recipient_addrs", "attachment_kinds")
_DATE_FIELDS = ("date_from", "date_to")
_BOOL_FIELDS = ("group_by_chain", "only_with_conclusion")
_SORTS = ("relevance", "date")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def json_object(value: Any, what: str = "the body") -> dict[str, Any]:
    """``value`` when it is a JSON object, else ``ApiError(400)`` naming ``what``."""
    if not isinstance(value, dict):
        raise ApiError(400, f"{what} must be a JSON object")
    return value


def str_list(body: dict[str, Any], key: str) -> list[str]:
    """``body[key]`` checked to be a list of strings; ``[]`` when absent."""
    value = body.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ApiError(400, f"{key} must be a list of strings")
    return value


def parse_filters(body: dict[str, Any]) -> SearchFilters:
    """The ``SearchFilters`` in ``body``: absent fields take their defaults, other keys are ignored.

    ``ApiError(400)`` when a field has the wrong type: ``query`` a string; the address and kind lists lists of
    strings; the dates integers or null; ``has_attachments`` a boolean or null; ``group_by_chain`` and
    ``only_with_conclusion`` booleans; ``sort`` ``"relevance"`` or ``"date"``.
    """
    values: dict[str, Any] = {}
    if "query" in body:
        if not isinstance(body["query"], str):
            raise ApiError(400, "query must be a string")
        values["query"] = body["query"]
    for key in _LIST_FIELDS:
        if key in body:
            values[key] = list(str_list(body, key))
    for key in _DATE_FIELDS:
        if key in body:
            if body[key] is not None and not _is_int(body[key]):
                raise ApiError(400, f"{key} must be an integer or null")
            values[key] = body[key]
    if "has_attachments" in body:
        if body["has_attachments"] is not None and not isinstance(body["has_attachments"], bool):
            raise ApiError(400, "has_attachments must be a boolean or null")
        values["has_attachments"] = body["has_attachments"]
    for key in _BOOL_FIELDS:
        if key in body:
            if not isinstance(body[key], bool):
                raise ApiError(400, f"{key} must be a boolean")
            values[key] = body[key]
    if "sort" in body:
        if body["sort"] not in _SORTS:
            raise ApiError(400, "sort must be \"relevance\" or \"date\"")
        values["sort"] = body["sort"]
    return SearchFilters(**values)


def _page_int(body: dict[str, Any], key: str, default: int, low: int, high: int | None) -> int:
    value = body.get(key, default)
    if not _is_int(value) or value < low or (high is not None and value > high):
        bounds = f"from {low}" if high is None else f"from {low} to {high}"
        raise ApiError(400, f"{key} must be an integer {bounds}")
    return value


def search_route(request: Request) -> JSONResponse:
    services = services_of(request)
    sup = supervisor_or_409(services, request.path_params["project_id"])
    body = json_object(read_json(request))
    filters = parse_filters(body)
    page = _page_int(body, "page", 0, 0, None)
    page_size = _page_int(body, "page_size", PAGE_SIZE, 1, MAX_PAGE_SIZE)
    return JSONResponse(asdict(search(sup.store, services.embedder, filters, page, page_size)))


def routes() -> list[Route]:
    return [Route("/api/projects/{project_id}/search", search_route, methods=["POST"])]
