import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid

import pytest

from emailsupersearch import catalog as catalog_module
from emailsupersearch.catalog import Catalog, Project
from emailsupersearch.ingest.chunker import CHUNKER_VERSION
from emailsupersearch.paths import project_dir


@pytest.fixture
def catalog(tmp_path):
    """A catalog rooted at ``tmp_path``; closed at teardown."""
    c = Catalog(tmp_path)
    yield c
    c.close()


def _lock_is_free_for_other_threads(catalog) -> bool:
    """Whether a second thread could take the catalog lock right now (False while this thread holds it)."""
    outcome: list[bool] = []

    def probe() -> None:
        got = catalog._lock.acquire(blocking=False)
        if got:
            catalog._lock.release()
        outcome.append(got)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    return outcome[0]


def test_create_list_get_delete_project(catalog, tmp_path, corpus_dir):
    before = int(time.time())
    first = catalog.create_project("Alpha", [corpus_dir])
    second = catalog.create_project("Beta", [corpus_dir])

    assert isinstance(first, Project)
    assert re.fullmatch(r"[0-9a-f]{12}", first.id)
    assert first.id != second.id
    assert first.name == "Alpha"
    assert before <= first.created_at <= int(time.time())
    assert first.dir == project_dir(tmp_path, first.id)
    assert first.dir.is_dir()
    assert (first.dir / "exports").is_dir()
    assert (first.dir / "project.sqlite").is_file()

    assert catalog.list_projects() == sorted((first, second), key=lambda p: (p.created_at, p.id))
    assert catalog.get(first.id) == first
    assert catalog.get("nosuchproject") is None

    assert catalog.delete_project(first.id) is True
    assert not first.dir.exists()
    assert catalog.get(first.id) is None
    assert [p.id for p in catalog.list_projects()] == [second.id]
    assert second.dir.is_dir()

    assert catalog.delete_project(first.id) is False  # already gone


def test_create_project_initialises_store_settings_and_folders(catalog, tmp_path, corpus_dir):
    other = tmp_path / "more mail"
    other.mkdir()
    project = catalog.create_project("Mail", [corpus_dir, other], language="de", embedder_id="minilm", embedding_dim=384)

    store = catalog.open_store(project.id)
    try:
        assert store.list_folders() == sorted(os.path.normpath(str(p)) for p in (corpus_dir, other))
        kinds = {r["path"]: r["kind"] for r in store.conn.execute("SELECT path, kind FROM folders")}
        assert set(kinds.values()) == {"local"}
        assert store.get_setting("language") == "de"
        assert store.get_setting("embedder_id") == "minilm"
        assert store.get_setting("embedding_dim") == 384
        assert store.get_setting("chunker_version") == CHUNKER_VERSION
        assert store.get_setting("reconcile_interval_s") == 900
        assert store.get_setting("ocr_enabled") is True
        assert store.get_setting("ocr_engine_order") == ["windows", "rapid"]
        assert store.get_setting("group_by_chain_default") is True
        assert store.get_setting("export_dir", "missing") is None
        assert store.get_setting("saved_searches") == []
    finally:
        store.close()


def test_create_project_defaults_for_a_keyword_only_install(catalog, corpus_dir):
    project = catalog.create_project("Keyword only", [corpus_dir])

    store = catalog.open_store(project.id)
    try:
        assert store.get_setting("language") == "en"
        assert store.get_setting("embedder_id", "missing") is None
        assert store.get_setting("embedding_dim", "missing") is None
    finally:
        store.close()


def test_saved_searches_roundtrip_in_store_settings(catalog, corpus_dir):
    project = catalog.create_project("Saved", [corpus_dir])
    saved = [
        {"name": "renewals", "query": "renewal date", "filters": {"has_attachment": True}},
        {"name": "invoices", "query": "INV-2024", "filters": {}},
    ]

    store = catalog.open_store(project.id)
    store.set_setting("saved_searches", saved)
    store.close()

    reopened = catalog.open_store(project.id)
    try:
        assert reopened.get_setting("saved_searches") == saved
    finally:
        reopened.close()


def test_global_settings_roundtrip(catalog, tmp_path):
    assert catalog.get_setting("port") is None
    assert catalog.get_setting("port", 8765) == 8765

    catalog.set_setting("port", 9000)
    catalog.set_setting("log_level", "DEBUG")
    catalog.set_setting("autostart", True)
    catalog.set_setting("data_root", str(tmp_path / "elsewhere"))
    catalog.set_setting("port", 9001)  # overwrites

    assert catalog.get_setting("port") == 9001
    assert catalog.get_setting("log_level") == "DEBUG"
    assert catalog.get_setting("autostart") is True
    assert catalog.get_setting("data_root") == str(tmp_path / "elsewhere")

    catalog.close()
    reopened = Catalog(tmp_path)  # settings persist across a reopen
    try:
        assert reopened.get_setting("port") == 9001
    finally:
        reopened.close()


def test_record_doctor_run(catalog, tmp_path):
    assert catalog.latest_doctor_run() is None

    before = int(time.time())
    catalog.record_doctor_run(json.dumps({"ok": True, "checks": 3}))
    catalog.record_doctor_run(json.dumps({"ok": False, "checks": 4}))

    ts, report_json = catalog.latest_doctor_run()
    assert before <= ts <= int(time.time())
    assert json.loads(report_json) == {"ok": False, "checks": 4}

    catalog.close()
    raw = sqlite3.connect(tmp_path / "catalog.sqlite")
    try:
        rows = raw.execute("SELECT ts, report_json FROM doctor_runs ORDER BY rowid").fetchall()
    finally:
        raw.close()
    assert [json.loads(r[1])["checks"] for r in rows] == [3, 4]
    assert all(isinstance(r[0], int) for r in rows)


def test_rename_project(catalog, corpus_dir):
    project = catalog.create_project("Old name", [corpus_dir])

    catalog.rename(project.id, "  New name  ")

    renamed = catalog.get(project.id)
    assert renamed is not None
    assert renamed.name == "New name"
    assert (renamed.id, renamed.created_at, renamed.dir) == (project.id, project.created_at, project.dir)
    with pytest.raises(KeyError):
        catalog.rename("nosuchproject", "Anything")
    with pytest.raises(ValueError):
        catalog.rename(project.id, "   ")
    assert catalog.get(project.id).name == "New name"


def test_create_project_requires_a_name(catalog, corpus_dir):
    with pytest.raises(ValueError):
        catalog.create_project("   ", [corpus_dir])
    assert catalog.list_projects() == []
    assert list((catalog.root / "projects").iterdir()) == []


def test_open_store_unknown_id_raises(catalog):
    with pytest.raises(KeyError):
        catalog.open_store("nosuchproject")


def test_open_store_holds_the_catalog_lock_while_opening(catalog, corpus_dir, monkeypatch):
    project = catalog.create_project("Locked", [corpus_dir])
    real_open = catalog_module.ProjectStore.open
    lock_free: list[bool] = []

    def spy(directory):
        lock_free.append(_lock_is_free_for_other_threads(catalog))
        return real_open(directory)

    monkeypatch.setattr(catalog_module.ProjectStore, "open", spy)
    catalog.open_store(project.id).close()

    assert lock_free == [False]


def test_network_folder_kind(catalog, corpus_dir, monkeypatch):
    monkeypatch.setattr(catalog_module, "is_network_path", lambda p: True)
    project = catalog.create_project("Share", [corpus_dir])

    store = catalog.open_store(project.id)
    try:
        assert [r["kind"] for r in store.conn.execute("SELECT kind FROM folders")] == ["network"]
    finally:
        store.close()


def test_close_releases_catalog_file(tmp_path):
    c = Catalog(tmp_path)
    c.set_setting("port", 9000)
    c.close()
    c.close()  # idempotent

    (tmp_path / "catalog.sqlite").unlink()
    assert not (tmp_path / "catalog.sqlite").exists()


def test_delete_project_with_closed_store_removes_directory_on_windows(catalog, corpus_dir):
    project = catalog.create_project("Doomed", [corpus_dir])
    store = catalog.open_store(project.id)
    store.set_setting("saved_searches", [{"name": "x"}])
    store.close()

    assert catalog.delete_project(project.id) is True
    assert not project.dir.exists()
    assert catalog.get(project.id) is None
    with pytest.raises(KeyError):
        catalog.open_store(project.id)
    assert not project.dir.exists()  # a late open_store cannot bring the directory back


def test_delete_project_failure_keeps_the_row_and_can_be_retried(catalog, corpus_dir, monkeypatch):
    project = catalog.create_project("Stubborn", [corpus_dir])
    calls: list[bool] = []

    def refuse(path, *args, **kwargs):
        calls.append(_lock_is_free_for_other_threads(catalog))
        raise PermissionError("in use")

    with monkeypatch.context() as patch:
        patch.setattr(catalog_module.shutil, "rmtree", refuse)
        patch.setattr(catalog_module, "_RMTREE_RETRY_S", 0)
        with pytest.raises(PermissionError):
            catalog.delete_project(project.id)

    assert len(calls) == catalog_module._RMTREE_RETRIES + 1 == 6
    assert calls == [False] * 6  # the catalog lock is held while the directory is removed
    assert catalog.get(project.id) == project  # still listed, so the owner can retry
    assert project.dir.is_dir()

    assert catalog.delete_project(project.id) is True  # the second call, with rmtree working again
    assert not project.dir.exists()
    assert catalog.get(project.id) is None


def test_delete_project_retries_until_windows_lets_go(catalog, corpus_dir, monkeypatch):
    project = catalog.create_project("Slow release", [corpus_dir])
    real_rmtree = shutil.rmtree
    attempts: list[int] = []

    def release_late(path, *args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("in use")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(catalog_module.shutil, "rmtree", release_late)
    monkeypatch.setattr(catalog_module, "_RMTREE_RETRY_S", 0)

    assert catalog.delete_project(project.id) is True
    assert len(attempts) == 3
    assert not project.dir.exists()


def test_delete_project_tolerates_a_missing_directory(catalog, corpus_dir):
    project = catalog.create_project("Gone", [corpus_dir])
    catalog.open_store(project.id).close()
    shutil.rmtree(project.dir)

    assert catalog.delete_project(project.id) is True
    assert catalog.get(project.id) is None


def test_create_project_failure_closes_its_store_and_removes_the_directory(catalog, tmp_path, corpus_dir, monkeypatch):
    def boom(p):
        raise RuntimeError("boom")

    monkeypatch.setattr(catalog_module, "is_network_path", boom)
    with pytest.raises(RuntimeError, match="boom"):
        catalog.create_project("Broken", [corpus_dir])

    assert catalog.list_projects() == []
    # The setup store was closed first, otherwise Windows would have refused to delete project.sqlite.
    assert list((tmp_path / "projects").iterdir()) == []


def test_create_project_failure_after_the_store_is_set_up_removes_the_directory(
    catalog, tmp_path, corpus_dir, monkeypatch
):
    monkeypatch.setattr(catalog_module.uuid, "uuid4", lambda: uuid.UUID(int=1))
    catalog.create_project("First", [corpus_dir])  # takes the only id uuid4 now yields
    (taken,) = (tmp_path / "projects").iterdir()

    with pytest.raises(FileExistsError):  # the same id again: the directory already exists
        catalog.create_project("Second", [corpus_dir])

    assert [p.name for p in catalog.list_projects()] == ["First"]
    assert taken.is_dir() and (taken / "project.sqlite").is_file()  # the first project is untouched
