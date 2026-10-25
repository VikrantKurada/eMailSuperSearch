"""The message viewer's routes: one message with its chain, an attachment's bytes, and opening the original file.

- ``GET /api/projects/{id}/messages/{key}``: the message, its sanitised HTML body (``sanitize_html``), its
  attachments, its live paths and its chain's live messages as cards.
- ``GET .../messages/{key}/attachments/{ordinal}``: the attachment re-extracted from the message's first live file.
- ``POST .../messages/{key}/open``: open the first live file with its default program (Outlook) -> 204.
- ``POST .../messages/{key}/reveal``: show the first live file selected in Explorer -> 204.

A message key is a Message-ID, which may hold ``/``; the UI percent-encodes it and the routes match it whole. A
message is found only while it has a live (not tombstoned) file: 404 otherwise. Every read runs on the store's
per-thread read connection, and the only file paths ever opened come from the project's own ``files`` table.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from emailsupersearch.host.routes_projects import (
    ApiError,
    services_of,
    supervisor_or_409,
)
from emailsupersearch.host.sanitize import sanitize_html
from emailsupersearch.ingest.parse import ParseError, parse_file
from emailsupersearch.search.hybrid import cards_for_keys, chain_member_keys

if TYPE_CHECKING:
    import sqlite3

_MESSAGE_SQL = (
    "SELECT subject, message_id, sent_ts, received_ts, date_source, chain_id, body_text, body_html, new_text"
    " FROM messages WHERE message_key = ?"
)
_FIRST_LIVE_PATH_SQL = "SELECT path FROM files WHERE message_key = ? AND tombstoned_at IS NULL ORDER BY path LIMIT 1"
# Characters that may not appear in the quoted ASCII ``filename``: anything outside printable ASCII, ``"``, ``;``, ``\``.
_UNSAFE_FILENAME_CHARS = re.compile(r'[^\x20-\x7e]|["\\;]')
_MIME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*")


def attachment_disposition(filename: str, ordinal: int) -> str:
    """The ``Content-Disposition`` of a downloaded attachment: an ASCII-safe ``filename`` and the exact UTF-8 name.

    In the ASCII name every non-ASCII or control character, ``"``, ``;`` and ``\\`` becomes ``_``. A blank filename
    is ``attachment-<ordinal>``.
    """
    name = filename if filename.strip() else f"attachment-{ordinal}"
    ascii_name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _conn_and_key(request: Request) -> tuple[sqlite3.Connection, str]:
    """The project's read connection and the message key; 404/409 for an unknown or blocked project."""
    sup = supervisor_or_409(services_of(request), request.path_params["project_id"])
    return sup.store.read_conn(), request.path_params["key"]


def _first_live_path(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute(_FIRST_LIVE_PATH_SQL, (key,)).fetchone()
    if row is None:
        raise ApiError(404, "no such message")
    return row[0]


def get_message(request: Request) -> JSONResponse:
    conn, key = _conn_and_key(request)
    row = conn.execute(_MESSAGE_SQL, (key,)).fetchone()
    cards = cards_for_keys(conn, [key]) if row is not None else []
    if not cards:
        raise ApiError(404, "no such message")
    card = cards[0]
    chain_id = row["chain_id"]
    chain = cards_for_keys(conn, chain_member_keys(conn, chain_id)) if chain_id is not None else []
    body_html = row["body_html"]
    return JSONResponse(
        {
            "message": {
                "headers": {
                    "subject": row["subject"],
                    "message_id": row["message_id"],
                    "sent_ts": row["sent_ts"],
                    "received_ts": row["received_ts"],
                    "date_source": row["date_source"],
                },
                "sender": asdict(card.sender),
                "to": [asdict(person) for person in card.to],
                "cc": [asdict(person) for person in card.cc],
                "sent_ts": row["sent_ts"],
                "body_html_sanitized": (sanitize_html(body_html) if body_html else "") or None,
                "body_text": row["body_text"],
                "new_text": row["new_text"],
            },
            "attachments": [asdict(att) for att in card.attachments],
            "paths": card.paths,
            "chain": {"chain_id": chain_id, "messages": [asdict(member) for member in chain]},
        }
    )


def get_attachment(request: Request) -> Response:
    conn, key = _conn_and_key(request)
    ordinal = request.path_params["ordinal"]
    path = _first_live_path(conn, key)
    try:
        parsed = parse_file(Path(path))
    except ParseError:
        raise ApiError(502, "could not re-open the message file") from None
    att = next((a for a in parsed.attachments if a.ordinal == ordinal), None)
    if att is None or att.data is None:
        raise ApiError(404, "no such attachment")
    media_type = att.mime if att.mime and _MIME_RE.fullmatch(att.mime) else "application/octet-stream"
    return Response(
        att.data, media_type=media_type, headers={"Content-Disposition": attachment_disposition(att.filename, ordinal)}
    )


def open_message(request: Request) -> Response:
    conn, key = _conn_and_key(request)
    path = _first_live_path(conn, key)
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise ApiError(501, "opening files is supported on Windows only")
    try:
        startfile(path)
    except OSError:
        raise ApiError(502, "could not open the message file") from None
    return Response(status_code=204)


def reveal_message(request: Request) -> Response:
    conn, key = _conn_and_key(request)
    path = _first_live_path(conn, key)
    if sys.platform != "win32":
        raise ApiError(501, "showing files in Explorer is supported on Windows only")
    # One command line, no shell: Explorer parses its own arguments and wants the path quoted right after
    # "/select," (a list would quote the whole "/select,<path>" token). A Windows path cannot contain a quote.
    try:
        process = subprocess.Popen(f'explorer /select,"{path}"')
    except OSError:
        raise ApiError(502, "could not start Explorer") from None
    _keep(process)
    return Response(status_code=204)


_explorers: list[subprocess.Popen] = []  # started Explorer processes, held until they exit
_explorers_lock = threading.Lock()


def _keep(process: subprocess.Popen) -> None:
    """Hold a reference to ``process`` until it has exited (polled whenever another starts), so it is reaped instead
    of being garbage-collected while running."""
    with _explorers_lock:
        _explorers[:] = [p for p in _explorers if p.poll() is None]
        _explorers.append(process)


def routes() -> list[Route]:
    # ``{key:path}`` matches a key holding ``/``; the routes with a suffix come first so the bare one cannot take them.
    message = "/api/projects/{project_id}/messages/{key:path}"
    return [
        Route(f"{message}/attachments/{{ordinal:int}}", get_attachment, methods=["GET"]),
        Route(f"{message}/open", open_message, methods=["POST"]),
        Route(f"{message}/reveal", reveal_message, methods=["POST"]),
        Route(message, get_message, methods=["GET"]),
    ]
