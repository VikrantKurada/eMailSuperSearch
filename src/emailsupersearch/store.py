"""ProjectStore: the one door to a project's ``project.sqlite`` (spec 8).

The scanner, the ingest pipeline, the supervisor and the HTTP routes all read and write the project
database through this class. One writer connection is shared by the supervisor thread and the request
handlers, so every write runs under an ``RLock`` and inside a transaction (``with self._lock, self.conn``).
sqlite3 transactions belong to a connection, so a read that does ``with conn:`` on the shared writer would
commit the supervisor's half-finished transaction; search and route reads therefore use ``read_conn()``,
a read-only connection per thread, and never the writer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from emailsupersearch.db.connection import connect
from emailsupersearch.db.schema import migrate
from emailsupersearch.ingest.identity import normalize_subject
from emailsupersearch.types import AttachmentText, ParsedMessage

if TYPE_CHECKING:
    from emailsupersearch.ingest.chunker import ChunkText

# SQLite caps the number of bound parameters per statement, so long ``IN (...)`` lists go in batches.
_BATCH = 500


@dataclass
class FileRecord:
    """One row of the file manifest: what the scanner saw at ``path`` and what the pipeline made of it."""

    path: str
    size: int
    mtime_ns: int
    sha256: str | None
    file_id: str | None
    message_key: str | None
    parse_status: str
    status_reason: str
    last_seen_scan_id: int
    tombstoned_at: int | None


@dataclass(frozen=True)
class ScanUpdate:
    """What the reconcile scan changes on an existing manifest row; ``reparse`` also sends the row back to ``pending``."""

    path: str
    size: int
    mtime_ns: int
    sha256: str
    file_id: str | None
    last_seen_scan_id: int
    reparse: bool


def _batches(items: Sequence[Any]) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), _BATCH):
        yield items[start : start + _BATCH]


def _marks(n: int) -> str:
    """``n`` comma-separated ``?`` placeholders."""
    return ",".join("?" * n)


def _file(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        path=row["path"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        sha256=row["sha256"],
        file_id=row["file_id"],
        message_key=row["message_key"],
        parse_status=row["parse_status"],
        status_reason=row["status_reason"],
        last_seen_scan_id=row["last_seen_scan_id"],
        tombstoned_at=row["tombstoned_at"],
    )


_UPSERT_FILE = (
    "INSERT INTO files(path, size, mtime_ns, sha256, file_id, message_key, parse_status, status_reason,"
    " last_seen_scan_id, tombstoned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns,"
    " sha256 = excluded.sha256, file_id = excluded.file_id, message_key = excluded.message_key,"
    " parse_status = excluded.parse_status, status_reason = excluded.status_reason,"
    " last_seen_scan_id = excluded.last_seen_scan_id, tombstoned_at = excluded.tombstoned_at"
)


# The columns the scan owns. message_key, and parse_status and status_reason unless the scan re-parses, belong to
# the ingest pipeline, which may have changed them since the scan read the row, so the scan never writes them back.
_UPDATE_SCAN_FIELDS = (
    "UPDATE files SET size = ?, mtime_ns = ?, sha256 = ?, file_id = ?, last_seen_scan_id = ?,"
    " parse_status = CASE WHEN ? THEN 'pending' ELSE parse_status END,"
    " status_reason = CASE WHEN ? THEN '' ELSE status_reason END WHERE path = ?"
)


def _file_values(rec: FileRecord) -> tuple[Any, ...]:
    """The ``_UPSERT_FILE`` parameters for ``rec``."""
    return (
        rec.path, rec.size, rec.mtime_ns, rec.sha256, rec.file_id, rec.message_key,
        rec.parse_status, rec.status_reason, rec.last_seen_scan_id, rec.tombstoned_at,
    )


def _under_roots(roots: Sequence[str]) -> tuple[str, list[str]]:
    """SQL condition (and its parameters) for "the path is one of ``roots`` or lies below one".

    A root is matched as a prefix that ends in a separator, so ``D:\\mail`` does not match ``D:\\mail-old\\x``.
    The scanner passes roots spelled as ``add_folder`` stored them (already normalised) and builds file paths by
    joining a root with ``os.path.join``, so the two share a spelling and there is no case folding.
    """
    clauses: list[str] = []
    params: list[str] = []
    for root in roots:
        norm = os.path.normpath(root)
        prefix = norm if norm.endswith(os.sep) else norm + os.sep
        clauses.append("(path = ? OR substr(path, 1, length(?)) = ?)")
        params.extend((norm, prefix, prefix))
    return "(" + " OR ".join(clauses) + ")", params


class ProjectStore:
    """Facade over one project's SQLite database.

    Paths: ``add_folder`` stores each root as ``os.path.normpath(path)``, and the scanner builds every
    ``files.path`` by joining a stored root with ``os.path.join``, so a file path shares its root's spelling.
    Roots are matched against ``files.path`` as exact string prefixes (``_under_roots``); no case folding is applied.
    """

    def __init__(self, path: Path, conn: sqlite3.Connection) -> None:
        self.path = path
        self.conn = conn
        self._lock = threading.RLock()
        self._local = threading.local()
        self._readers: dict[threading.Thread, sqlite3.Connection] = {}
        self._closed = False

    @classmethod
    def open(cls, project_dir: Path) -> ProjectStore:
        """Open (creating if missing) the project in ``project_dir`` and bring its schema up to date."""
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / "project.sqlite"
        conn = connect(path)
        try:
            migrate(conn)
        except BaseException:
            conn.close()
            raise
        return cls(path, conn)

    @property
    def lock(self) -> threading.RLock:
        """The writer lock. Collaborators that read or write through ``conn`` hold it, as the store's own methods do."""
        return self._lock

    def close(self) -> None:
        """Close every connection so the database file can be deleted on Windows. Safe to call twice."""
        with self._lock:
            self._closed = True
            for reader in self._readers.values():
                reader.close()
            self._readers.clear()
            self.conn.close()

    def read_conn(self) -> sqlite3.Connection:
        """A read-only connection owned by the calling thread, for searches and route reads.

        It sees every committed write and cannot disturb the writer's transaction. Writes never use it.
        The connection is opened outside the writer lock, so a thread's first read does not wait for a write.
        Connections of threads that have exited (a server's worker pool retires idle threads) are closed
        whenever another thread opens its own, so their number stays near the number of live threads.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None and not self._closed:
            return conn
        if self._closed:
            raise RuntimeError("the project store is closed")
        conn = connect(self.path)
        try:
            conn.execute("PRAGMA query_only=1")
            with self._lock:
                if self._closed:
                    raise RuntimeError("the project store is closed")
                for thread in [t for t in self._readers if not t.is_alive()]:
                    self._readers.pop(thread).close()
                self._readers[threading.current_thread()] = conn
        except BaseException:
            conn.close()
            raise
        self._local.conn = conn
        return conn

    # ---- settings ----

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # ---- folders ----

    def list_folders(self) -> list[str]:
        with self._lock:
            return [r["path"] for r in self.conn.execute("SELECT path FROM folders ORDER BY path")]

    def add_folder(self, path: str, kind: Literal["local", "network"]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO folders(path, kind, last_scan_id, reachable) VALUES (?, ?, 0, 1)",
                (os.path.normpath(path), kind),
            )

    def remove_folder(self, path: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM folders WHERE path = ?", (os.path.normpath(path),))

    # ---- file manifest ----

    def get_file(self, path: str) -> FileRecord | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return None if row is None else _file(row)

    def find_by_file_id(self, file_id: str) -> FileRecord | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM files WHERE file_id = ? ORDER BY path LIMIT 1", (file_id,)).fetchone()
        return None if row is None else _file(row)

    def upsert_file(self, rec: FileRecord) -> None:
        with self._lock, self.conn:
            self.conn.execute(_UPSERT_FILE, _file_values(rec))

    def upsert_files(self, recs: list[FileRecord]) -> None:
        """Write many manifest rows in one transaction; the scanner flushes its buffered rows through this."""
        with self._lock, self.conn:
            self.conn.executemany(_UPSERT_FILE, [_file_values(rec) for rec in recs])

    def update_file_scan_fields(self, updates: list[ScanUpdate]) -> None:
        """Apply the scan's changes to existing rows in one transaction, leaving the pipeline's columns as they are."""
        with self._lock, self.conn:
            self.conn.executemany(
                _UPDATE_SCAN_FIELDS,
                [
                    (
                        u.size, u.mtime_ns, u.sha256, u.file_id, u.last_seen_scan_id,
                        int(u.reparse), int(u.reparse), u.path,
                    )
                    for u in updates
                ],
            )

    def move_file(self, old_path: str, new_path: str) -> None:
        """Re-key the manifest row at ``old_path`` to ``new_path``, dropping any row already at ``new_path``."""
        if old_path == new_path:
            return
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM files WHERE path = ?", (new_path,))
            self.conn.execute("UPDATE files SET path = ? WHERE path = ?", (new_path, old_path))

    def stamp_seen(self, paths: list[str], scan_id: int) -> None:
        """Record that ``paths`` were seen in ``scan_id``; a tombstoned path that reappears is revived."""
        with self._lock, self.conn:
            for batch in _batches(paths):
                self.conn.execute(
                    f"UPDATE files SET last_seen_scan_id = ?, tombstoned_at = NULL WHERE path IN ({_marks(len(batch))})",
                    (scan_id, *batch),
                )

    def unseen_under(self, roots: list[str], scan_id: int) -> list[FileRecord]:
        """Live files below ``roots`` that scan ``scan_id`` did not see."""
        if not roots:
            return []
        condition, params = _under_roots(roots)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM files WHERE tombstoned_at IS NULL AND last_seen_scan_id < ? AND {condition} ORDER BY path",
                (scan_id, *params),
            ).fetchall()
        return [_file(r) for r in rows]

    def tombstone(self, paths: list[str], now: int) -> None:
        """Mark ``paths`` as gone at ``now``; a path that is already tombstoned keeps its first time."""
        with self._lock, self.conn:
            for batch in _batches(paths):
                self.conn.execute(
                    f"UPDATE files SET tombstoned_at = ? WHERE tombstoned_at IS NULL AND path IN ({_marks(len(batch))})",
                    (now, *batch),
                )

    def purge_tombstoned_under(self, roots: list[str], scan_id: int, skip: Sequence[str] = ()) -> list[FileRecord]:
        """Delete and return tombstoned rows below ``roots`` that have been unseen for two scans.

        Rows below any path in ``skip`` (a subtree the scan could not read) are left alone.
        """
        if not roots:
            return []
        condition, params = _under_roots(roots)
        if skip:
            skip_condition, skip_params = _under_roots(skip)
            condition += f" AND NOT {skip_condition}"
            params += skip_params
        with self._lock, self.conn:
            rows = self.conn.execute(
                f"SELECT * FROM files WHERE tombstoned_at IS NOT NULL AND last_seen_scan_id < ? AND {condition} ORDER BY path",
                (scan_id - 1, *params),
            ).fetchall()
            purged = [_file(r) for r in rows]
            for batch in _batches([r.path for r in purged]):
                self.conn.execute(f"DELETE FROM files WHERE path IN ({_marks(len(batch))})", tuple(batch))
        return purged

    def paths_for_message(self, message_key: str, live_only: bool = True) -> list[str]:
        sql = "SELECT path FROM files WHERE message_key = ?"
        if live_only:
            sql += " AND tombstoned_at IS NULL"
        with self._lock:
            return [r["path"] for r in self.conn.execute(sql + " ORDER BY path", (message_key,))]

    # ---- messages ----

    def upsert_message(
        self, m: ParsedMessage, message_key: str, new_text: str, chain_id: str | None, chain_depth: int
    ) -> None:
        """Write the message row and replace its recipients."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO messages(message_key, message_id, sent_ts, received_ts, date_source, subject,"
                " normalized_subject, sender_name, sender_addr, chain_id, chain_depth, body_text, body_html,"
                " new_text, has_attachments, flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(message_key) DO UPDATE SET message_id = excluded.message_id,"
                " sent_ts = excluded.sent_ts, received_ts = excluded.received_ts, date_source = excluded.date_source,"
                " subject = excluded.subject, normalized_subject = excluded.normalized_subject,"
                " sender_name = excluded.sender_name, sender_addr = excluded.sender_addr,"
                " chain_id = excluded.chain_id, chain_depth = excluded.chain_depth, body_text = excluded.body_text,"
                " body_html = excluded.body_html, new_text = excluded.new_text,"
                " has_attachments = excluded.has_attachments, flags = excluded.flags",
                (
                    message_key, m.message_id, m.sent_ts, m.received_ts, m.date_source, m.subject,
                    normalize_subject(m.subject), m.sender_name, m.sender_addr, chain_id, chain_depth,
                    m.body_text, m.body_html, new_text, int(bool(m.attachments)), "{}",
                ),
            )
            self.conn.execute("DELETE FROM recipients WHERE message_key = ?", (message_key,))
            self.conn.executemany(
                "INSERT INTO recipients(message_key, kind, name, addr) VALUES (?, ?, ?, ?)",
                [(message_key, r.kind, r.name, r.addr) for r in m.recipients],
            )

    def upsert_attachments(self, message_key: str, atts: list[AttachmentText], ocr_status_default: str) -> None:
        """Replace the message's attachment rows; an attachment with pages to OCR starts at ``ocr_status_default``."""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM attachments WHERE message_key = ?", (message_key,))
            self.conn.executemany(
                "INSERT INTO attachments(message_key, ordinal, filename, mime, size, kind, sha256, extract_status,"
                " ocr_status, embedded_partial, page_count, text_chars) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 0, ?, ?)",
                [
                    (
                        message_key, a.ordinal, a.filename, a.size, a.kind, a.sha256, a.status,
                        ocr_status_default if a.needs_ocr_pages else "none", a.page_count, len(a.text),
                    )
                    for a in atts
                ],
            )

    def set_attachment_status(
        self,
        message_key: str,
        ordinal: int,
        extract_status: str | None = None,
        ocr_status: str | None = None,
        embedded_partial: bool | None = None,
    ) -> None:
        """Update the given fields of one attachment; fields passed as None keep their value."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE attachments SET extract_status = COALESCE(?, extract_status),"
                " ocr_status = COALESCE(?, ocr_status), embedded_partial = COALESCE(?, embedded_partial)"
                " WHERE message_key = ? AND ordinal = ?",
                (
                    extract_status, ocr_status, None if embedded_partial is None else int(embedded_partial),
                    message_key, ordinal,
                ),
            )

    def delete_message(self, message_key: str) -> None:
        """Remove the message with its recipients, attachments, chunks and vectors; ``files`` rows stay."""
        with self._lock, self.conn:
            ids = [r["id"] for r in self.conn.execute("SELECT id FROM chunks WHERE message_key = ?", (message_key,))]
            self._delete_vectors(ids)
            self.conn.execute("DELETE FROM chunks WHERE message_key = ?", (message_key,))
            self.conn.execute("DELETE FROM recipients WHERE message_key = ?", (message_key,))
            self.conn.execute("DELETE FROM attachments WHERE message_key = ?", (message_key,))
            self.conn.execute("DELETE FROM messages WHERE message_key = ?", (message_key,))

    def message(self, message_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM messages WHERE message_key = ?", (message_key,)).fetchone()

    # ---- chunks ----

    def _delete_vectors(self, chunk_ids: list[int]) -> None:
        """Remove the given chunks' rows from ``chunks_vec``, which no trigger maintains.

        The caller holds the lock and has a transaction open.
        """
        for batch in _batches(chunk_ids):
            self.conn.execute(f"DELETE FROM chunks_vec WHERE chunk_rowid IN ({_marks(len(batch))})", tuple(batch))

    def replace_chunks(self, message_key: str, part: str, chunks: list[ChunkText]) -> list[int]:
        """Replace one part's chunks (``embedded`` = 0) and return the new rowids in input order."""
        with self._lock, self.conn:
            old = self.conn.execute("SELECT id FROM chunks WHERE message_key = ? AND part = ?", (message_key, part))
            self._delete_vectors([r["id"] for r in old])
            self.conn.execute("DELETE FROM chunks WHERE message_key = ? AND part = ?", (message_key, part))
            rowids: list[int] = []
            for c in chunks:
                cur = self.conn.execute(
                    "INSERT INTO chunks(message_key, part, idx, text, token_count, embedded) VALUES (?, ?, ?, ?, ?, 0)",
                    (message_key, part, c.idx, c.text, c.token_count),
                )
                rowids.append(cur.lastrowid)
            return rowids

    def mark_embedded(self, rowids: list[int]) -> None:
        with self._lock, self.conn:
            for batch in _batches(rowids):
                self.conn.execute(f"UPDATE chunks SET embedded = 1 WHERE id IN ({_marks(len(batch))})", tuple(batch))

    def chunk_rowids(self, message_key: str) -> list[int]:
        with self._lock:
            rows = self.conn.execute("SELECT id FROM chunks WHERE message_key = ? ORDER BY part, idx", (message_key,))
            return [r["id"] for r in rows]

    # ---- counts ----

    def counts(self) -> dict[str, int]:
        """Row counts for the status screen; ``metadata_only`` messages are searchable by header fields only."""
        with self._lock:
            row = self.conn.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM messages),"
                " (SELECT COUNT(*) FROM chunks),"
                " (SELECT COUNT(*) FROM files WHERE tombstoned_at IS NULL),"
                " (SELECT COUNT(*) FROM files WHERE tombstoned_at IS NOT NULL),"
                " (SELECT COUNT(*) FROM messages m"
                "  WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.message_key = m.message_key)"
                "  OR EXISTS (SELECT 1 FROM files f WHERE f.message_key = m.message_key"
                "             AND f.parse_status IN ('failed', 'indexed_partial')))"
            ).fetchone()
        return dict(zip(("messages", "chunks", "files", "tombstoned", "metadata_only"), tuple(row), strict=True))
