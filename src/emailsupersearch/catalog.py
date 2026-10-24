"""The catalog: the small global database that lists projects, holds global settings and records doctor runs.

``catalog.sqlite`` sits in the data root next to the ``projects/`` folder. Each project has its own directory
(``projects/<id>``) holding its ``project.sqlite``; the catalog creates, renames and deletes those projects and opens
their ``ProjectStore``. It needs neither FTS5 nor sqlite-vec, so it opens with plain ``sqlite3``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from emailsupersearch.ingest.chunker import CHUNKER_VERSION
from emailsupersearch.ingest.watcher import is_network_path
from emailsupersearch.paths import ensure_layout, project_dir
from emailsupersearch.store import ProjectStore

_RMTREE_ATTEMPTS = 5
_RMTREE_RETRY_S = 0.2

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, created_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS doctor_runs(ts INTEGER, report_json TEXT)",
    "CREATE TABLE IF NOT EXISTS catalog_version(version INTEGER)",
    "INSERT INTO catalog_version(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM catalog_version)",
)


@dataclass
class Project:
    id: str
    name: str
    created_at: int
    dir: Path


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("a project needs a name")
    return cleaned


def _remove_tree(path: Path) -> None:
    """Remove ``path``, retrying on ``PermissionError``: Windows releases a closed database's file handles late."""
    for attempt in range(_RMTREE_ATTEMPTS):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == _RMTREE_ATTEMPTS - 1:
                raise
            time.sleep(_RMTREE_RETRY_S)


class Catalog:
    """The global database of projects and settings. Thread-safe: one connection, every method under an ``RLock``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        ensure_layout(root)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(root / "catalog.sqlite"), check_same_thread=False)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.row_factory = sqlite3.Row
            with self._conn:
                for statement in _SCHEMA:
                    self._conn.execute(statement)
        except BaseException:
            self._conn.close()
            raise

    # ---- projects ----

    def create_project(
        self,
        name: str,
        folders: list[Path],
        language: str = "en",
        embedder_id: str | None = None,
        embedding_dim: int | None = None,
    ) -> Project:
        """Create a project: its directory, its ``project.sqlite`` with the default settings and the watched folders.

        The store used to set it up is closed before this returns; open the project's own through ``open_store``.
        """
        name = _clean_name(name)
        project_id = uuid.uuid4().hex[:12]
        directory = project_dir(self.root, project_id)
        (directory / "exports").mkdir(parents=True)
        store = ProjectStore.open(directory)
        try:
            for folder in folders:
                kind = "network" if is_network_path(Path(folder)) else "local"
                store.add_folder(os.path.normpath(str(folder)), kind)
            for key, value in (
                ("language", language),
                ("embedder_id", embedder_id),
                ("embedding_dim", embedding_dim),
                ("chunker_version", CHUNKER_VERSION),
                ("reconcile_interval_s", 900),
                ("ocr_enabled", True),
                ("ocr_engine_order", ["windows", "rapid"]),
                ("group_by_chain_default", True),
                ("export_dir", None),
                ("saved_searches", []),
            ):
                store.set_setting(key, value)
        finally:
            store.close()
        created_at = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)", (project_id, name, created_at)
            )
        return Project(project_id, name, created_at, directory)

    def list_projects(self) -> list[Project]:
        with self._lock:
            rows = self._conn.execute("SELECT id, name, created_at FROM projects ORDER BY created_at, id").fetchall()
        return [self._project(row) for row in rows]

    def get(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, created_at FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return None if row is None else self._project(row)

    def rename(self, project_id: str, name: str) -> None:
        """Give the project a new name. ``KeyError`` when no such project exists."""
        name = _clean_name(name)
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
            if cur.rowcount == 0:
                raise KeyError(project_id)

    def delete_project(self, project_id: str) -> bool:
        """Delete the project's catalog row and its directory; True when a row existed.

        Every ``ProjectStore`` opened on the project must be closed first, or Windows refuses to delete the
        database files (the removal retries briefly because it releases handles late, then raises
        ``PermissionError``). A missing directory is not an error.
        """
        with self._lock, self._conn:
            existed = self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount > 0
        if existed:
            _remove_tree(project_dir(self.root, project_id))
        return existed

    def open_store(self, project_id: str) -> ProjectStore:
        """Open the project's ``ProjectStore``; ``KeyError`` for an unknown id, ``SchemaTooNew`` for a newer database.

        Every call opens a new store and the caller owns it: close it when done (the host keeps one per supervisor).
        """
        project = self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        return ProjectStore.open(project.dir)

    def _project(self, row: sqlite3.Row) -> Project:
        return Project(row["id"], row["name"], row["created_at"], project_dir(self.root, row["id"]))

    # ---- global settings ----

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # ---- doctor runs ----

    def record_doctor_run(self, report_json: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO doctor_runs(ts, report_json) VALUES (?, ?)", (int(time.time()), report_json)
            )

    def latest_doctor_run(self) -> tuple[int, str] | None:
        """The most recent doctor run as ``(timestamp, report_json)``, or None when there has been none."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, report_json FROM doctor_runs ORDER BY ts DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return None if row is None else (row["ts"], row["report_json"])

    def close(self) -> None:
        """Close the database so ``catalog.sqlite`` can be deleted on Windows. Safe to call twice."""
        with self._lock:
            self._conn.close()
