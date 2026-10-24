import json
import os
import re
import shutil
import sqlite3
import time

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


def test_delete_project_tolerates_a_missing_directory(catalog, corpus_dir):
    project = catalog.create_project("Gone", [corpus_dir])
    catalog.open_store(project.id).close()
    shutil.rmtree(project.dir)

    assert catalog.delete_project(project.id) is True
    assert catalog.get(project.id) is None


def test_create_project_failure_closes_its_store_and_registers_nothing(catalog, tmp_path, corpus_dir, monkeypatch):
    def boom(p):
        raise RuntimeError("boom")

    monkeypatch.setattr(catalog_module, "is_network_path", boom)
    with pytest.raises(RuntimeError, match="boom"):
        catalog.create_project("Broken", [corpus_dir])

    assert catalog.list_projects() == []
    (leftover,) = (tmp_path / "projects").iterdir()
    (leftover / "project.sqlite").unlink()  # the setup store was closed, so Windows lets go of the file
