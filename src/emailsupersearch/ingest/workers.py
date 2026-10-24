"""Parse mail files in spawned subprocesses, so a hostile or huge file can be killed.

``parse_document`` does the whole parse (file hash, message, attachment text). ``ParsePool.parse`` runs it in a
worker process and enforces two limits: a wall-clock timeout, and a cap on how far the worker's peak memory may
grow while it parses one file. Either way the pool kills every worker and starts a fresh executor, so a hung or
inflated worker never serves the next file; only the file that tripped the limit fails.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from emailsupersearch.ingest.attachments import extract_text
from emailsupersearch.ingest.parse import parse_file
from emailsupersearch.ingest.parse_cache import sha256_file
from emailsupersearch.types import ParsedDocument, ParsedMessage

_MB = 1024 * 1024
_START_UP_TIMEOUT_S = 60.0


class ParseTimeout(Exception):
    """A file whose worker ran past the timeout or died (``reason`` says which); the file is not parsed."""

    def __init__(self, path: Path, reason: str = "timeout") -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class ParseMemoryExceeded(Exception):
    """A file whose parse grew the worker's peak memory past the cap; the file is not parsed."""


def _drop_attachment_bytes(message: ParsedMessage) -> None:
    for att in message.attachments:
        att.data = None
        if att.nested is not None:
            _drop_attachment_bytes(att.nested)


def parse_document(path: Path) -> ParsedDocument:
    """The hash, message and attachment text of one mail file; raises ``ParseError`` for an unreadable file.

    An attached message is one entry in ``attachments`` (kind ``msg`` or ``eml``) whose text is the inner
    subject and body. The returned message carries no attachment bytes: the OCR step re-parses the file.
    """
    path = Path(path)
    sha256 = sha256_file(path)
    message = parse_file(path)
    attachments = [extract_text(att) for att in message.attachments]
    _drop_attachment_bytes(message)
    return ParsedDocument(sha256, message, attachments)


class _ProcessMemoryCounters(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS of psapi.h: DWORD fields are 32 bits, SIZE_T fields pointer-sized."""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _peak_working_set_bytes() -> int:
    """The highest resident memory this process has reached so far."""
    if sys.platform == "win32":
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_uint32]
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return counters.PeakWorkingSetSize
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # bytes on macOS, kilobytes elsewhere


def _check_memory_growth(baseline: int, max_rss_mb: int) -> None:
    grown = _peak_working_set_bytes() - baseline
    if grown > max_rss_mb * _MB:
        raise ParseMemoryExceeded(f"peak working set grew by {grown // _MB} MB, over the {max_rss_mb} MB cap")


def _worker_fn(path: Path, max_rss_mb: int) -> ParsedDocument:
    """Runs in the worker process. The peak is measured against its value at entry, because a worker that
    has parsed earlier files starts with whatever they left behind."""
    baseline = _peak_working_set_bytes()
    doc = parse_document(path)
    _check_memory_growth(baseline, max_rss_mb)
    return doc


def _sleep_forever_for_tests(path: Path, max_rss_mb: int) -> None:
    time.sleep(60)


def _allocate_for_tests(path: Path, max_rss_mb: int) -> None:
    baseline = _peak_working_set_bytes()
    block = bytearray(256 * _MB)
    for offset in range(0, len(block), 4096):  # touch every page so it counts towards the working set
        block[offset] = 1
    _check_memory_growth(baseline, max_rss_mb)


def _start_up() -> None:
    """Does nothing; a spawned worker has to import this module to run it, which is the start-up cost."""


class ParsePool:
    """Parses files in ``workers`` spawned processes; ``parse`` may be called from several threads."""

    def __init__(self, workers: int = 1, timeout_s: float = 120.0, max_rss_mb: int = 1024) -> None:
        self._workers = workers
        self._timeout_s = timeout_s
        self._max_rss_mb = max_rss_mb
        self._lock = threading.Lock()
        self._executor = self._new_executor()
        self._started = False  # whether the current executor's workers are running yet

    def _new_executor(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=self._workers, mp_context=multiprocessing.get_context("spawn"))

    def _replace_executor(self, failed: ProcessPoolExecutor) -> None:
        """Kill every worker of ``failed`` and carry on with a fresh executor.

        When another thread already replaced it (a kill breaks every in-flight call), the new one is kept.
        """
        with self._lock:
            if self._executor is not failed:
                return
            for process in list((failed._processes or {}).values()):
                process.kill()
            failed.shutdown(wait=False, cancel_futures=True)
            self._executor = self._new_executor()
            self._started = False

    def parse(self, path: Path) -> ParsedDocument:
        """The parsed document; raises ``ParseTimeout``, ``ParseMemoryExceeded`` or the worker's ``ParseError``."""
        path = Path(path)
        try:
            with self._lock:
                executor = self._executor
                if not self._started:
                    # Importing the parsers takes about half a second per process; that must not count
                    # against the timeout of the first file, so the workers are started before it.
                    wait([executor.submit(_start_up) for _ in range(self._workers)], timeout=_START_UP_TIMEOUT_S)
                    self._started = True
                future = executor.submit(_worker_fn, path, self._max_rss_mb)  # looked up now, so a test can patch it
            return future.result(timeout=self._timeout_s)
        except FutureTimeoutError:
            self._replace_executor(executor)
            raise ParseTimeout(path) from None
        except ParseMemoryExceeded:
            self._replace_executor(executor)
            raise
        except BrokenProcessPool as e:
            self._replace_executor(executor)
            raise ParseTimeout(path, f"worker died: {e}") from e

    def close(self) -> None:
        with self._lock:
            self._executor.shutdown(wait=True, cancel_futures=True)
