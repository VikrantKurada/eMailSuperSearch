"""The project supervisor: reconcile on start, on a timer and on watcher triggers; OCR dispatch; full re-index."""

import sqlite3
import threading
from pathlib import Path

import pytest
from helpers import make_eml_bytes, wait_until

from emailsupersearch.cli import main
from emailsupersearch.ingest.ocr.worker import OcrJobFailed
from emailsupersearch.ingest.watcher import is_network_path
from emailsupersearch.jobs.queue import pending_counts
from emailsupersearch.jobs.supervisor import build_project_supervisor


def _scan_id(sup) -> int:
    return sup.store.get_setting("last_scan_id", 0)


def _started_and_idle(sup, scans: int = 1):
    """A predicate: the supervisor has finished at least ``scans`` scans and is idle."""
    return lambda: _scan_id(sup) >= scans and sup.status().state == "idle"


def _supervisor_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("ess-") and t.is_alive()]


def test_run_once_indexes_corpus(sup, corpus_dir):
    r = sup.run_once()
    assert r.to_parse and sup.store.counts()["messages"] > 0 and sup.store.get_setting("last_synced_at")


def test_timer_rescans(sup_fast, corpus_dir):
    sup_fast.start()
    (corpus_dir / "new.eml").write_bytes(make_eml_bytes())
    wait_until(lambda: sup_fast.store.get_file(str(corpus_dir / "new.eml")), 10)
    seen = _scan_id(sup_fast)
    wait_until(lambda: _scan_id(sup_fast) >= seen + 2, 10)  # nothing changes now: only the timer scans
    sup_fast.stop()


def test_watcher_triggers_rescan(sup_slow, corpus_dir):
    sup_slow.start()
    wait_until(_started_and_idle(sup_slow), 30)  # the start-up scan is over: only the watcher can see the new file
    (corpus_dir / "w.eml").write_bytes(make_eml_bytes("w"))
    wait_until(lambda: sup_slow.store.get_file(str(corpus_dir / "w.eml")), 15)
    sup_slow.stop()


def test_ocr_jobs_are_drained(sup_with_ocr, corpus_dir):
    sup_with_ocr.start()

    def drained() -> bool:
        jobs = pending_counts(sup_with_ocr.store.read_conn())
        return jobs["queued"] == 0 and jobs["running"] == 0 and jobs["done"] >= 1 and sup_with_ocr.status().state == "idle"

    wait_until(drained, 120)  # the corpus's image attachment needs OCR
    sup_with_ocr.stop()


def test_status_reports_metadata_only(sup):
    sup.run_once()
    status = sup.status()
    assert status.metadata_only >= 0 and status.state == "idle"
    assert status.files_total > 0 and status.files_pending == 0 and status.last_scan_at


def test_changed_embedder_id_forces_full_reindex(tmp_store, corpus_dir, chunker, embedder):
    tmp_store.set_setting("ocr_enabled", False)  # OCR text would add attachment chunks and change the count
    s = build_project_supervisor("p", tmp_store, embedder, chunker, corpus_dir.parent / "cache", None)
    try:
        s.run_once()
    finally:
        s.pipeline.pool.close()  # not s.stop(): that would close the store the second supervisor uses
    n = tmp_store.counts()["chunks"]
    tmp_store.set_setting("embedder_id", "other-model")
    s2 = build_project_supervisor("p", tmp_store, embedder, chunker, corpus_dir.parent / "cache", None)
    try:
        assert tmp_store.get_setting("reindex_required") is True
        s2.start()
        # The flag clears once the index is emptied, and the state stays busy until the re-parse is over.
        wait_until(
            lambda: tmp_store.get_setting("reindex_required") is False
            and s2.status().state == "idle"
            and tmp_store.counts()["chunks"] == n,
            60,
        )
    finally:
        s2.stop()


def test_is_network_path():
    assert is_network_path(Path(r"\\server\share\x")) and not is_network_path(Path(r"C:\x"))


def test_cli_sync_refuses_onedrive_root(monkeypatch, capsys):
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", r"C:\Users\x\OneDrive\ess")
    assert main(["sync", "--project", "p", "--once"]) == 2
    assert "OneDrive" in capsys.readouterr().err


def test_stop_is_idempotent_and_closes_store(sup):
    sup.start()
    wait_until(_started_and_idle(sup), 60)
    assert _supervisor_threads()  # the worker, the timer and the watcher
    sup.stop()
    sup.stop()
    assert not _supervisor_threads()
    with pytest.raises(sqlite3.ProgrammingError):
        sup.store.conn.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        sup.store.read_conn()


def test_failed_rows_are_retried_once_on_start(sup, corpus_dir):
    sup.run_once()
    path = str(corpus_dir / "t000-m00.eml")
    rec = sup.store.get_file(path)
    sup.store.set_file_parse_result(path, rec.message_key, "failed", "timeout")
    sup.start()
    wait_until(lambda: sup.store.get_file(path).parse_status == "indexed", 60)
    sup.stop()


def test_orphan_messages_are_swept(sup, corpus_dir):
    sup.run_once()
    path = corpus_dir / "t000-m00.eml"
    key = sup.store.get_file(str(path)).message_key
    assert sup.store.message(key) is not None
    with sup.store.lock, sup.store.conn:
        sup.store.conn.execute("DELETE FROM files WHERE path = ?", (str(path),))
    path.unlink()  # else the next scan finds the file again and indexes the message anew
    sup.run_once()
    assert sup.store.message(key) is None and sup.store.chunk_rowids(key) == []


def test_ocr_failure_unavailable_is_final(sup_with_ocr, monkeypatch):
    calls: list[dict] = []

    def unavailable(payload: dict, language: str) -> str:
        calls.append(payload)
        raise OcrJobFailed("OcrUnavailable: x")

    monkeypatch.setattr(sup_with_ocr.ocr_runner, "run", unavailable)
    sup_with_ocr.start()
    wait_until(
        lambda: pending_counts(sup_with_ocr.store.read_conn())["failed"] >= 1 and sup_with_ocr.status().state == "idle",
        60,
    )
    jobs = sup_with_ocr.store.read_conn().execute("SELECT state, attempts FROM jobs").fetchall()
    sup_with_ocr.stop()
    assert jobs and all((state, attempts) == ("failed", 1) for state, attempts in jobs)
    assert len(calls) == len(jobs)


def test_request_rescan_coalesces(sup):
    for _ in range(5):
        sup.request_rescan()
    sup.start()
    wait_until(_started_and_idle(sup, scans=2), 60)
    sup.stop()  # every command queued before the stop is run first
    finished = [e for e in sup.events_for_tests if e["type"] == "scan_finished"]
    assert 1 <= len(finished) <= 2 and max(e["scan_id"] for e in finished) <= 2
