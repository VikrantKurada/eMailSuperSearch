"""The project supervisor: reconcile on start, on a timer and on watcher triggers; OCR dispatch; full re-index."""

import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from helpers import make_eml_bytes, wait_until

from emailsupersearch.cli import main
from emailsupersearch.ingest.ocr.worker import OcrJobFailed
from emailsupersearch.ingest.watcher import FolderWatcher, is_network_path
from emailsupersearch.jobs import supervisor as supervisor_module
from emailsupersearch.jobs.queue import enqueue, pending_counts
from emailsupersearch.jobs.supervisor import build_project_supervisor


def _scan_id(sup) -> int:
    return sup.store.get_setting("last_scan_id", 0)


def _started_and_idle(sup, scans: int = 1):
    """A predicate: the supervisor has finished at least ``scans`` scans and is idle."""
    return lambda: _scan_id(sup) >= scans and sup.status().state == "idle"


def _supervisor_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("ess-") and t.is_alive()]


def _assert_closed(store) -> None:
    with pytest.raises(sqlite3.ProgrammingError):
        store.conn.execute("SELECT 1")


def test_run_once_indexes_corpus(sup, corpus_dir):
    r = sup.run_once()
    assert r.to_parse and sup.store.counts()["messages"] > 0 and sup.store.get_setting("last_synced_at")


def test_timer_rescans(sup_fast, corpus_dir):
    sup_fast.start()
    (corpus_dir / "new.eml").write_bytes(make_eml_bytes())
    wait_until(lambda: sup_fast.store.get_file(str(corpus_dir / "new.eml")), 10)
    seen = _scan_id(sup_fast)
    # Nothing changes now; at most one more scan can come from the watcher, so three prove the timer re-arms.
    wait_until(lambda: _scan_id(sup_fast) >= seen + 3, 10)
    sup_fast.stop()


def test_watcher_triggers_rescan(sup_slow, corpus_dir):
    sup_slow.start()
    wait_until(_started_and_idle(sup_slow), 30)  # the start-up scan is over: only the watcher can see the new file
    (corpus_dir / "w.eml").write_bytes(make_eml_bytes("w"))
    wait_until(lambda: sup_slow.store.get_file(str(corpus_dir / "w.eml")), 15)
    sup_slow.stop()


def test_refresh_watchers_watches_a_new_folder(sup_slow, corpus_dir):
    sup_slow.start()
    wait_until(_started_and_idle(sup_slow), 30)
    more = corpus_dir.parent / "more"
    more.mkdir()
    sup_slow.store.add_folder(str(more), "local")
    sup_slow.refresh_watchers()
    new_file, data = more / "x.eml", make_eml_bytes("x", seed=7)
    # Written until it is indexed, so a watcher still setting up cannot miss it; only the new watcher sees "more".
    wait_until(lambda: (new_file.write_bytes(data), sup_slow.store.get_file(str(new_file)))[1], 15)
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
        # The flag clears once the index is emptied (0 chunks), so this holds only after the rebuild.
        wait_until(
            lambda: tmp_store.get_setting("reindex_required") is False
            and s2.status().state == "idle"
            and tmp_store.counts()["chunks"] == n,
            60,
        )
    finally:
        s2.stop()


def test_run_once_after_prepare_reindexes(sup):
    sup.run_once()
    n = sup.store.counts()["chunks"]
    sup.store.set_setting("reindex_required", True)
    sup.prepare()
    counts = sup.store.counts()
    assert counts["chunks"] == 0 and sup.status().files_pending == counts["files"] > 0
    assert sup.store.get_setting("reindex_required") is False
    sup.run_once()
    assert sup.store.counts()["chunks"] == n
    sup.store.set_setting("reindex_required", True)
    sup.prepare()  # only the first call acts
    assert sup.store.counts()["chunks"] == n


def test_is_network_path():
    assert is_network_path(Path(r"\\server\share\x")) and not is_network_path(Path(r"C:\x"))


def test_cli_sync_refuses_onedrive_root(monkeypatch, capsys):
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", r"C:\Users\x\OneDrive\ess")
    assert main(["sync", "--project", "p", "--once"]) == 2
    assert "OneDrive" in capsys.readouterr().err


def test_cli_sync_refuses_an_unknown_project(monkeypatch, tmp_path, capsys):
    root = tmp_path / "data"
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(root))
    assert main(["sync", "--project", "nope", "--once"]) == 2
    assert "unknown project: nope" in capsys.readouterr().err
    assert not root.exists()  # nothing was created


def test_stop_is_idempotent_and_closes_store(sup):
    sup.start()
    wait_until(_started_and_idle(sup), 60)
    assert _supervisor_threads()  # the worker, the timer and the watcher
    sup.stop()
    sup.stop()
    assert not _supervisor_threads()
    _assert_closed(sup.store)
    with pytest.raises(RuntimeError):
        sup.store.read_conn()
    assert sup.store.closed
    status = sup.status()  # the last status known, marked closed
    assert (status.state, status.last_error) == ("idle", "closed") and status.files_total > 0


def test_status_raises_store_errors_while_the_store_is_open(sup, monkeypatch):
    def broken() -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(sup.store, "counts", broken)
    with pytest.raises(RuntimeError, match="boom"):
        sup.status()


def test_stop_during_long_scan_returns_quickly(sup, monkeypatch):
    monkeypatch.setattr(supervisor_module, "_APPLY_SLICE", 1)  # the corpus is smaller than one real slice
    ingest = sup.pipeline.ingest_file

    def slow_ingest(path, scan_id):
        time.sleep(0.5)
        return ingest(path, scan_id)

    monkeypatch.setattr(sup.pipeline, "ingest_file", slow_ingest)
    sup.start()
    wait_until(lambda: any(e["type"] == "file_indexed" for e in sup.events_for_tests), 30)
    began = time.monotonic()
    sup.stop()
    assert time.monotonic() - began < 5
    assert not _supervisor_threads()
    _assert_closed(sup.store)


def test_stop_from_an_event_callback_closes_on_exit(sup):
    def stop_on_scan(event: dict) -> None:
        if event["type"] == "scan_started":
            sup.stop()  # on the worker thread: it must not wait for itself

    sup.on_event = stop_on_scan
    sup.start()
    wait_until(lambda: not _supervisor_threads(), 30)
    _assert_closed(sup.store)
    # The stop came before the reconcile ended, so nothing was parsed (the rows stay pending for the next start).
    assert not [e for e in sup.events_for_tests if e["type"] == "file_indexed"]


def test_watcher_stop_from_on_change_does_not_wait(tmp_path, caplog):
    seen: list[set[Path]] = []

    def on_change(dirs: set[Path]) -> None:
        seen.append(dirs)
        watcher.stop()

    watcher = FolderWatcher([tmp_path], on_change, debounce_s=0.1)
    watcher.start()
    with caplog.at_level(logging.WARNING, logger="emailsupersearch"):
        wait_until(lambda: ((tmp_path / "a.eml").write_bytes(b"x"), seen)[1], 10)
        wait_until(lambda: not _supervisor_threads(), 5)
    assert not [r for r in caplog.records if "folder watcher" in r.getMessage()]


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


def _ocr_jobs(sup) -> list[tuple]:
    return [tuple(r) for r in sup.store.read_conn().execute("SELECT state, attempts, last_error FROM jobs")]


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
    jobs = _ocr_jobs(sup_with_ocr)
    sup_with_ocr.stop()
    assert jobs and all(job[:2] == ("failed", 1) for job in jobs)
    assert len(calls) == len(jobs)


def test_ocr_failure_timeout_is_retried(sup_with_ocr, monkeypatch):
    calls: list[dict] = []

    def times_out_once(payload: dict, language: str) -> str:
        calls.append(payload)
        if len(calls) == 1:
            raise OcrJobFailed("timeout")
        return "recovered text"

    monkeypatch.setattr(sup_with_ocr.ocr_runner, "run", times_out_once)
    sup_with_ocr.start()
    wait_until(
        lambda: pending_counts(sup_with_ocr.store.read_conn())["done"] >= 1 and sup_with_ocr.status().state == "idle",
        60,
    )
    jobs = _ocr_jobs(sup_with_ocr)
    sup_with_ocr.stop()
    # The timeout put the job back in the queue with one attempt counted; the second run completed it.
    assert jobs == [("done", 1, "timeout")] and len(calls) == 2


def test_dropped_rescan_still_drains_ocr(sup_slow, monkeypatch):
    class StubRunner:
        def run(self, payload: dict, language: str) -> str:
            return "stub text"

    attached: list[tuple] = []
    sup_slow.ocr_runner = StubRunner()
    monkeypatch.setattr(sup_slow.pipeline, "attach_ocr_text", lambda *args: attached.append(args))
    with sup_slow.store.lock:
        enqueue(sup_slow.store.conn, "ocr", {"path": "x", "sha256": "abc", "ordinal": 0, "pages": [0]})
    # A timer scan, then a rescan that the scan serves: each scan's drain stops for the command behind it, and the
    # rescan is dropped when it comes up. The timer (3600 s) cannot fire during the test.
    sup_slow._commands.put(("scan", None))
    sup_slow.request_rescan()
    sup_slow.start()
    wait_until(lambda: pending_counts(sup_slow.store.read_conn())["done"] == 1, 30)
    assert attached == [("abc", 0, "stub text")] and _scan_id(sup_slow) == 2  # the rescan ran no scan
    sup_slow.stop()


def test_request_rescan_coalesces(sup):
    for _ in range(5):
        sup.request_rescan()
    assert sup._commands.qsize() == 1  # the burst queued one command
    sup.start()
    wait_until(_started_and_idle(sup, scans=2), 60)  # the start-up scan, then the one rescan
    sup.stop()
    finished = [e for e in sup.events_for_tests if e["type"] == "scan_finished"]
    assert 1 <= len(finished) <= 2 and max(e["scan_id"] for e in finished) <= 2


def test_set_reconcile_interval_changes_next_timer(sup):
    sup.set_reconcile_interval(120)
    assert sup.reconcile_interval_s == 120  # what _arm_timer reads
    sup._arm_timer()
    assert sup._timer.interval == 120  # stop() at teardown cancels it
