"""SQLite connection helper: WAL, foreign keys, sqlite-vec loaded, FTS5 required (spec 8)."""

import sqlite3
from pathlib import Path

import sqlite_vec


class MissingSqliteFeature(Exception):
    """The SQLite build lacks a feature the index needs; the message names it."""


def _require_fts5(conn: sqlite3.Connection) -> None:
    options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    if "ENABLE_FTS5" not in options:
        raise MissingSqliteFeature("fts5")


def connect(path: Path) -> sqlite3.Connection:
    """Open a project database.

    check_same_thread is off because the supervisor thread and request handlers
    share one connection per project. Python's default transaction handling is
    kept, so ``with conn:`` commits.
    """
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        _require_fts5(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except BaseException:
        conn.close()
        raise
    return conn
