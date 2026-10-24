"""The project supervisor (spec 9): the one thread that writes a project's database.

One ``ProjectSupervisor`` runs per open project. Its worker thread reconciles the mail folders when it starts,
every ``reconcile_interval_s`` seconds and whenever a folder watcher reports a change, and drives the ingest
pipeline over what each scan found. Between scans it drains the OCR queue one job at a time, and on start it
rebuilds the whole index when the embedder or the chunker changed. Request handlers only read (``status``) or ask
for a rescan (``request_rescan``, ``refresh_watchers``); their settings and folder edits are serialised with the
worker's writes by the store's lock. Chain assignment and message writes run on the worker thread only, or on the
caller's thread for a direct ``prepare``/``run_once`` (the command line), which hold the same run lock so the two
never overlap.

``stop`` pre-empts the worker: it is checked before every start-up step, before every queued command, between
slices of a scan's files and before every OCR job, so closing a project waits for one slice or one job at most.

INFO and WARNING logs carry counts and exception type names only: never a path, a subject or an address.
"""

from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import logging
import os
import queue
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from emailsupersearch.index.vectors import VectorIndex
from emailsupersearch.ingest.chunker import CHUNKER_VERSION, Chunker
from emailsupersearch.ingest.ocr.base import select_engine
from emailsupersearch.ingest.ocr.worker import OcrJobFailed, OcrRunner
from emailsupersearch.ingest.parse_cache import ParseCache
from emailsupersearch.ingest.pipeline import IngestPipeline
from emailsupersearch.ingest.scan import ScanResult, reconcile
from emailsupersearch.ingest.watcher import FolderWatcher, is_network_path
from emailsupersearch.ingest.workers import ParsePool
from emailsupersearch.jobs.queue import (
    Job,
    claim_next,
    complete,
    enqueue,
    fail,
    pending_counts,
    requeue_running,
)

if TYPE_CHECKING:
    from emailsupersearch.index.embedder import OnnxEmbedder
    from emailsupersearch.store import ProjectStore

log = logging.getLogger(__name__)

State = Literal["idle", "scanning", "parsing", "ocr", "reindexing"]

DEFAULT_RECONCILE_INTERVAL_S = 900
# Failure reasons worth one more try per start: the parse worker ran out of time or memory, or died, or the pool
# was closed under it by a stop.
RETRY_ON_START_PREFIXES = ("timeout", "memory_cap", "worker", "pool closed")
NETWORK_POLL_DELAY_MS = 30_000  # network shares are polled every 30 s, not every 0.3 s
_APPLY_SLICE = 50  # files parsed between two checks for a stop
_STOP_JOIN_S = 30.0
_TIMER_JOIN_S = 5.0
_FREE_RAM_FOR_TWO_WORKERS = 8 * 1024**3
_OCR_ERROR_LOG_CHARS = 80


@dataclass
class SyncStatus:
    """What the status screen shows for one project; ``ocr_pending`` counts queued and running OCR jobs."""

    project_id: str
    state: State
    last_scan_at: int | None
    files_total: int
    files_pending: int
    ocr_pending: int
    metadata_only: int
    last_error: str | None


def _error_type(reason: str) -> str:
    """The exception type a scan error reason starts with (``"PermissionError: ..."``), without its text."""
    return reason.split(":", 1)[0]


class ProjectSupervisor:
    """Owns one project's writer thread: reconcile, ingest, OCR dispatch and re-index. See the module docstring.

    Events (``on_event``): ``scan_started``, ``scan_finished`` (with counts), ``ocr_done`` (with counts) and
    ``error`` (with the exception type), plus the pipeline's own ``file_indexed`` and ``renamed``.
    """

    def __init__(
        self,
        project_id: str,
        store: ProjectStore,
        pipeline: IngestPipeline,
        ocr_runner: OcrRunner | None,
        language: str = "en",
        reconcile_interval_s: int = DEFAULT_RECONCILE_INTERVAL_S,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.project_id = project_id
        self.store = store
        self.pipeline = pipeline
        self.ocr_runner = ocr_runner
        self.language = language
        self.reconcile_interval_s = reconcile_interval_s
        self.on_event = on_event
        self._state: State = "idle"
        self._last_error: str | None = None
        self._last_status: SyncStatus | None = None
        self._commands: queue.Queue[tuple[str, Path | None]] = queue.Queue()
        self._stop_event = threading.Event()  # set first thing in stop(); every long step checks it
        self._rescan_lock = threading.Lock()
        self._rescan_queued = False  # a rescan was requested and no scan has started since
        self._run_lock = threading.Lock()  # one scan, prepare or OCR write at a time, whichever thread asks
        self._prepared = False
        self._lifecycle = threading.Lock()  # guards the fields below against a concurrent stop()
        self._started = False
        self._stopped = False
        self._worker: threading.Thread | None = None
        self._timer: threading.Timer | None = None
        self._watchers: list[FolderWatcher] = []

    # ---- public API ----

    def start(self) -> None:
        """Start the worker thread and return at once. Does nothing when already started or stopped.

        The worker runs ``prepare``, starts the folder watchers, scans, arms the timer and drains the OCR queue;
        then it serves rescans and timer scans until ``stop``.
        """
        with self._lifecycle:
            if self._started or self._stopped:
                return
            self._started = True
            self._worker = threading.Thread(target=self._work, name="ess-supervisor", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """Stop the watchers and the timer, let the worker finish its current slice or OCR job (up to 30 s), then
        close the parse pool and the store. Safe to call twice, and without ``start``.

        Called from the worker thread itself (an ``on_event`` callback), it returns at once and the worker closes
        the pool and the store as it exits.
        """
        self._stop_event.set()
        with self._lifecycle:
            if self._stopped:
                return
            self._stopped = True
            watchers, self._watchers = self._watchers, []
            timer, self._timer = self._timer, None
            worker = self._worker
        for watcher in watchers:
            watcher.stop()
        if timer is not None:
            timer.cancel()
            timer.join(_TIMER_JOIN_S)
        if worker is not None:
            self._commands.put(("stop", None))
            if threading.current_thread() is worker:
                return
            worker.join(_STOP_JOIN_S)
            if worker.is_alive():
                log.warning("the supervisor thread did not finish within %d s; closing anyway", _STOP_JOIN_S)
        self._close()

    def prepare(self) -> None:
        """Make the project ready to scan: re-queue jobs a crash left running, rebuild the index when
        ``reindex_required`` is set, and give files that failed for lack of time or memory one more try (they go
        back to ``pending``, so the next scan parses them again).

        ``start`` runs it on the worker thread; ``sync --once`` calls it before ``run_once``. Only the first
        successful call does anything, so the retry happens once per supervisor.
        """
        with self._run_lock:
            if self._prepared or self._stop_event.is_set():
                return
            with self.store.lock:
                requeued = requeue_running(self.store.conn)
            if requeued:
                log.info("%d jobs left running by the last session are queued again", requeued)
            if self._stop_event.is_set():
                return
            if self.store.get_setting("reindex_required", False):
                self._reindex()
            retried = self.store.retry_failed_files(RETRY_ON_START_PREFIXES)
            if retried:
                log.info("%d files that ran out of time or memory will be parsed again", retried)
            self._prepared = True

    def refresh_watchers(self) -> None:
        """Watch the project's current folders: local ones through change notifications, network ones by polling.

        The new watchers start before the old ones stop, so no change is missed in between. The folder routes call
        this after adding or removing a folder.
        """
        if self._stop_event.is_set():
            return
        roots = [Path(p) for p in self.store.list_folders()]
        on_network = {root: is_network_path(root) for root in roots}
        local = [root for root in roots if not on_network[root]]
        network = [root for root in roots if on_network[root]]
        new: list[FolderWatcher] = []
        if local:
            new.append(FolderWatcher(local, on_change=lambda dirs: self.request_rescan()))
        if network:
            new.append(
                FolderWatcher(
                    network,
                    on_change=lambda dirs: self.request_rescan(),
                    force_polling=True,
                    poll_delay_ms=NETWORK_POLL_DELAY_MS,
                )
            )
        for watcher in new:
            watcher.start()
        with self._lifecycle:
            if self._stopped:
                old = new  # stop() ran meanwhile and could not see these
            else:
                old, self._watchers = self._watchers, new
        for watcher in old:
            watcher.stop()

    def request_rescan(self, subtree: Path | None = None) -> None:
        """Ask for a reconcile soon; requests made before a scan starts are all served by that one scan.

        ``subtree`` is kept for narrowing the scan later; for now every rescan covers all the folders.
        """
        with self._rescan_lock:
            if self._rescan_queued:
                return
            self._rescan_queued = True
        self._commands.put(("rescan", subtree))

    def set_reconcile_interval(self, seconds: int) -> None:
        """Wait ``seconds`` between timer scans from the next time the timer is armed (after the next scan); the
        project settings route calls this when the setting changes."""
        with self._lifecycle:
            self.reconcile_interval_s = seconds

    def status(self) -> SyncStatus:
        """The current status; once the store is closed, the last one known, ``idle`` with the error ``closed``."""
        try:
            counts = self.store.counts()
            jobs = pending_counts(self.store.read_conn())
            status = SyncStatus(
                project_id=self.project_id,
                state=self._state,
                last_scan_at=self.store.get_setting("last_synced_at"),
                files_total=counts["files"],
                files_pending=self.store.pending_file_count(),
                ocr_pending=jobs["queued"] + jobs["running"],
                metadata_only=counts["metadata_only"],
                last_error=self._last_error,
            )
        except (sqlite3.ProgrammingError, RuntimeError):
            if not self.store.closed:
                raise
            last = self._last_status or SyncStatus(self.project_id, "idle", None, 0, 0, 0, 0, None)
            return dataclasses.replace(last, state="idle", last_error="closed")
        self._last_status = status
        return status

    def run_once(self) -> ScanResult:
        """Reconcile every folder and index what changed, synchronously; works without ``start``.

        Records ``last_scan_id`` and ``last_synced_at`` once the whole scan is applied. Raises what the scan raised,
        after recording its type as the last error.
        """
        with self._run_lock:
            return self._scan()

    # ---- worker thread ----

    def _work(self) -> None:
        try:
            for step, fn in (("start", self.prepare), ("watch", self.refresh_watchers), ("scan", self._scan_and_drain)):
                if self._stop_event.is_set():
                    return
                self._guard(step, fn)
            while True:
                command, _subtree = self._commands.get()
                if command == "stop" or self._stop_event.is_set():
                    return
                if command in ("scan", "rescan"):
                    if self._scan_wanted(command):
                        self._guard(command, self._scan_and_drain)
                    else:  # this waiting rescan cut the last scan's drain short; drain now instead
                        self._guard("ocr", self._drain_ocr)
                elif command == "ocr":
                    self._guard(command, self._drain_ocr)
        finally:
            if self._stop_event.is_set():  # stop() may have been called on this thread and left the closing to it
                self._close()

    def _scan_wanted(self, command: str) -> bool:
        """Whether a dequeued scan command should run. A scan serves every rescan requested before it starts, so
        it clears the rescan flag; a rescan command found with the flag already clear was served and is dropped."""
        with self._rescan_lock:
            wanted = command == "scan" or self._rescan_queued
            self._rescan_queued = False
        return wanted

    def _guard(self, step: str, fn: Callable[[], object]) -> None:
        """Run one worker step; an exception is logged by type, recorded and announced, and the worker carries on."""
        try:
            fn()
        except Exception as e:
            self._state = "idle"
            self._last_error = type(e).__name__
            log.warning("supervisor step %s failed: %s", step, type(e).__name__)
            log.debug("supervisor step %s failed", step, exc_info=True)
            self._emit({"type": "error", "step": step, "error": type(e).__name__})

    def _reindex(self) -> None:
        """Empty the index and send every live file back to ``pending``: the next scan rebuilds everything with the
        current embedder and chunker, from the parse cache. Clearing the flag right after the (atomic) clear is
        crash-safe, because the ``pending`` rows themselves carry the rest of the work."""
        self._state = "reindexing"
        try:
            log.info("the embedder or chunker changed: rebuilding the whole index")
            self.store.clear_index()
            self.store.set_setting("reindex_required", False)
        finally:
            self._state = "idle"

    def _scan_and_drain(self) -> None:
        """One reconcile, then the timer re-armed, then the OCR queue drained."""
        if self._stop_event.is_set():
            return
        with contextlib.suppress(Exception):  # run_once has logged, recorded and announced it
            self.run_once()
        self._arm_timer()
        self._drain_ocr()

    def _arm_timer(self) -> None:
        """Schedule the next timer scan ``reconcile_interval_s`` from now, replacing any pending one."""
        with self._lifecycle:
            if self._stopped:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.reconcile_interval_s, lambda: self._commands.put(("scan", None)))
            self._timer.name = "ess-reconcile-timer"
            self._timer.daemon = True
            self._timer.start()

    def _close(self) -> None:
        try:
            self.pipeline.pool.close()
        finally:
            self.store.close()

    # ---- the scan step ----

    def _scan(self) -> ScanResult:
        """Reconcile, apply, sweep orphaned messages, record the scan; the caller holds the run lock.

        A scan cut short by ``stop`` records nothing and sweeps nothing: the next one does it all.
        """
        scan_id = 0
        try:
            scan_id = int(self.store.get_setting("last_scan_id", 0)) + 1
            roots = [Path(p) for p in self.store.list_folders()]
            self._emit({"type": "scan_started", "scan_id": scan_id})
            self._state = "scanning"
            result = reconcile(self.store, roots, scan_id, int(time.time()))
            self._state = "parsing"
            applied = self._apply(result)
            if applied:
                orphans = self._sweep_orphans()
                self.store.set_setting("last_scan_id", scan_id)
                self.store.set_setting("last_synced_at", int(time.time()))
        except Exception as e:
            self._last_error = type(e).__name__
            log.warning("scan %d failed: %s", scan_id, type(e).__name__)
            log.debug("scan %d failed", scan_id, exc_info=True)
            self._emit({"type": "error", "scan_id": scan_id, "error": type(e).__name__})
            raise
        finally:
            self._state = "idle"
        if not applied:
            log.info("scan %d stopped before all %d files were parsed", scan_id, len(result.to_parse))
            return result
        self._last_error = _error_type(result.errors[0][1]) if result.errors else None
        counts = {
            "to_parse": len(result.to_parse),
            "renamed": len(result.renamed),
            "tombstoned": len(result.tombstoned),
            "purged": len(result.purged),
            "errors": len(result.errors),
            "aborted_roots": len(result.aborted_roots),
        }
        log.info(
            "scan %d: %d to parse, %d renamed, %d tombstoned, %d purged, %d errors, %d roots unreadable,"
            " %d orphaned messages removed",
            scan_id, *counts.values(), orphans,
        )
        self._emit({"type": "scan_finished", "scan_id": scan_id, **counts})
        return result

    def _apply(self, result: ScanResult) -> bool:
        """Hand the scan to the pipeline in slices of `_APPLY_SLICE` files, checking for a stop between slices;
        True when all of it was applied. The first slice carries the renames and purges. Files a stop leaves
        unreached keep their ``pending`` row, so the next reconcile queues them again."""
        todo = result.to_parse
        if self._stop_event.is_set():  # stopped during the reconcile: keep its renames and purges, parse nothing
            self.pipeline.apply_scan(dataclasses.replace(result, to_parse=[]))
            return False
        self.pipeline.apply_scan(dataclasses.replace(result, to_parse=todo[:_APPLY_SLICE]))
        for start in range(_APPLY_SLICE, len(todo), _APPLY_SLICE):
            if self._stop_event.is_set():
                return False
            batch = todo[start : start + _APPLY_SLICE]
            self.pipeline.apply_scan(dataclasses.replace(result, to_parse=batch, renamed=[], purged=[]))
        return True

    def _sweep_orphans(self) -> int:
        """Remove every message no file row links any more (say, a purge whose removal failed); returns how many."""
        keys = self.store.orphan_message_keys()
        for key in keys:
            self.pipeline.remove_path("", message_key=key)
        return len(keys)

    # ---- OCR dispatch ----

    def _drain_ocr(self) -> None:
        """Run queued OCR jobs one at a time until none is left, a command (a scan) is waiting or ``stop`` ran."""
        if self.ocr_runner is None:
            return
        done = failed = 0
        try:
            while not self._stop_event.is_set() and self._commands.empty():
                with self.store.lock:
                    job = claim_next(self.store.conn, ["ocr"])
                if job is None:
                    break
                self._state = "ocr"
                if self._run_ocr_job(job):
                    done += 1
                else:
                    failed += 1
        finally:
            self._state = "idle"
        if done or failed:
            log.info("OCR: %d jobs done, %d failed", done, failed)
            self._emit({"type": "ocr_done", "done": done, "failed": failed})

    def _run_ocr_job(self, job: Job) -> bool:
        """OCR one claimed job and bring its text in; True on success. A failure is recorded on the job: final
        when no engine can run, else retried (the queue gives up after three attempts)."""
        assert self.ocr_runner is not None
        try:
            text = self.ocr_runner.run(job.payload, self.language)
            with self._run_lock:
                self.pipeline.attach_ocr_text(job.payload["sha256"], int(job.payload["ordinal"]), text)
            with self.store.lock:
                complete(self.store.conn, job.id)
            return True
        except OcrJobFailed as e:
            error = str(e)  # the OCR child's message names no path
            log.warning("an OCR job failed: %s", _error_type(error)[:_OCR_ERROR_LOG_CHARS])
            self._fail_job(job, error, retry=not error.startswith("OcrUnavailable"))
        except Exception as e:
            log.warning("an OCR job failed: %s", type(e).__name__)
            log.debug("OCR job %d failed", job.id, exc_info=True)
            self._fail_job(job, type(e).__name__, retry=True)
        return False

    def _fail_job(self, job: Job, error: str, retry: bool) -> None:
        with self.store.lock:
            fail(self.store.conn, job.id, error, retry=retry)

    # ---- events ----

    def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as e:
            log.warning("an event callback failed: %s", type(e).__name__)
            log.debug("event callback failed for %r", event, exc_info=True)


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _free_ram_bytes() -> int:
    if sys.platform == "win32":
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return int(status.ullAvailPhys)
    return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


def workers_for_free_ram() -> int:
    """Parse workers to run: 2 when at least 8 GB of RAM is free, else 1 (also when it cannot be measured)."""
    try:
        free = _free_ram_bytes()
    except Exception:  # noqa: BLE001 - one worker is always safe
        return 1
    return 2 if free >= _FREE_RAM_FOR_TWO_WORKERS else 1


def _ocr_enqueuer(store: ProjectStore) -> Callable[[dict], int]:
    """``enqueue_ocr`` for the pipeline: queue the job on the store's writer connection, under its lock."""

    def enqueue_ocr(payload: dict) -> int:
        with store.lock:
            return enqueue(store.conn, "ocr", payload)

    return enqueue_ocr


def build_project_supervisor(
    project_id: str,
    store: ProjectStore,
    embedder: OnnxEmbedder | None,
    chunker: Chunker,
    cache_dir: Path,
    on_event: Callable[[dict], None] | None,
) -> ProjectSupervisor:
    """Wire a supervisor (with its own parse pool) for one open project from the project's settings.

    When the embedder or the chunker differs from the one the index was built with, ``reindex_required`` is set
    and the new ones are recorded: the next ``prepare`` rebuilds the index. OCR is wired only when the
    ``ocr_enabled`` setting is on and an engine can run for the project's ``language``.
    """
    language = store.get_setting("language", "en")
    embedder_id = embedder.id if embedder is not None else None
    if store.get_setting("embedder_id") != embedder_id or store.get_setting("chunker_version") != CHUNKER_VERSION:
        store.set_setting("reindex_required", True)
        store.set_setting("embedder_id", embedder_id)
        store.set_setting("chunker_version", CHUNKER_VERSION)
    ocr = bool(store.get_setting("ocr_enabled", True)) and select_engine(language) is not None
    pipeline = IngestPipeline(
        store,
        ParseCache(cache_dir),
        ParsePool(workers=workers_for_free_ram()),
        chunker,
        embedder,
        VectorIndex(store.conn) if embedder is not None else None,
        enqueue_ocr=_ocr_enqueuer(store) if ocr else None,
        on_event=on_event,
    )
    return ProjectSupervisor(
        project_id,
        store,
        pipeline,
        OcrRunner() if ocr else None,
        language=language,
        reconcile_interval_s=int(store.get_setting("reconcile_interval_s", DEFAULT_RECONCILE_INTERVAL_S)),
        on_event=on_event,
    )
