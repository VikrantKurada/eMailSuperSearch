"""Parse mail files in spawned subprocesses, so a hostile or huge file can be killed.

``parse_document`` does the whole parse (file hash, message, attachment text). ``ParsePool.parse`` runs it in a
worker process and enforces two limits: a wall-clock timeout, and a cap on how far the worker's memory may grow.
On Windows the cap is enforced while the worker runs, by a job object that makes an allocation past it fail;
everywhere it is also checked once the parse is done. Either way the pool kills every worker and starts a fresh
executor, so a hung or inflated worker never serves the next file; only the file that tripped the limit fails.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import multiprocessing
import sys
import threading
import time
from concurrent.futures import CancelledError, ProcessPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from emailsupersearch.ingest.attachments import extract_text
from emailsupersearch.ingest.parse import ParseError, parse_file
from emailsupersearch.ingest.parse_cache import sha256_file
from emailsupersearch.types import ParsedDocument, ParsedMessage

log = logging.getLogger(__name__)

_MB = 1024 * 1024
_START_UP_TIMEOUT_S = 60.0


class ParseTimeout(Exception):
    """A file whose worker ran past the timeout or died (``reason`` says which); the file is not parsed."""

    def __init__(self, path: Path, reason: str = "timeout") -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class ParseMemoryExceeded(Exception):
    """A file whose parse grew the worker's memory past the cap; the file is not parsed."""


def _drop_attachment_bytes(message: ParsedMessage) -> None:
    for att in message.attachments:
        att.data = None
        if att.nested is not None:
            _drop_attachment_bytes(att.nested)


def parse_document(path: Path) -> ParsedDocument:
    """The hash, message and attachment text of one mail file.

    Raises ``ParseError`` for a file that cannot be read (any ``OSError`` while hashing it, such as a missing
    path) as well as for one that cannot be parsed. An attached message is one entry in ``attachments`` (kind
    ``msg`` or ``eml``) whose text is the inner subject and body. The returned message carries no attachment
    bytes: the OCR step re-parses the file.
    """
    path = Path(path)
    try:
        sha256 = sha256_file(path)
    except OSError as e:
        raise ParseError(f"{type(e).__name__}: {e}") from e
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


class _BasicLimitInformation(ctypes.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION of winnt.h."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    """IO_COUNTERS of winnt.h."""

    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION of winnt.h."""

    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_PROCESS_TERMINATE = 0x0001
_PROCESS_VM_READ = 0x0010
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_INFORMATION = 0x0400
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


@functools.cache
def _win32():
    """``(kernel32, psapi)`` with the prototypes of the calls this module makes."""
    kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    handle = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = handle
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = handle
    kernel32.CreateJobObjectW.argtypes = [handle, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = handle
    kernel32.SetInformationJobObject.argtypes = [handle, ctypes.c_int, handle, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
    kernel32.CloseHandle.argtypes = [handle]
    psapi.GetProcessMemoryInfo.argtypes = [handle, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_uint32]
    return kernel32, psapi


def _memory_counters(process: int) -> _ProcessMemoryCounters:
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not _win32()[1].GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return counters


def _peak_working_set_bytes() -> int:
    """The highest resident memory this process has reached so far."""
    if sys.platform == "win32":
        return _memory_counters(_win32()[0].GetCurrentProcess()).PeakWorkingSetSize
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # bytes on macOS, kilobytes elsewhere


def _limit_process_memory(pid: int, growth_mb: int) -> int:
    """Windows only: put process ``pid`` in a new job object that makes any allocation fail once the process's
    committed memory would exceed what it holds now plus ``growth_mb``. Returns the job handle; raises
    ``OSError`` when the process cannot be assigned (for example it is already in a job that forbids nesting).

    The limit is relative to the commit at this moment because a parse worker has already committed several
    hundred MB for its libraries; an absolute figure of ``growth_mb`` would starve it from the first byte.
    """
    kernel32 = _win32()[0]
    process = kernel32.OpenProcess(
        _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ | _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
    )
    if not process:
        raise ctypes.WinError()
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError()
        try:
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = _memory_counters(process).PagefileUsage + growth_mb * _MB
            if not kernel32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError()
            if not kernel32.AssignProcessToJobObject(job, process):
                raise ctypes.WinError()
        except BaseException:
            kernel32.CloseHandle(job)
            raise
        return job
    finally:
        kernel32.CloseHandle(process)


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


def _allocate_forever_for_tests(path: Path, max_rss_mb: int) -> None:
    """Allocates 32 MB at a time with no check of its own, so only an enforced limit can stop it.

    It gives up after 2 GB, so a machine where nothing enforces the cap is not exhausted: the pool's
    timeout then ends it.
    """
    blocks = []
    for _ in range(64):
        block = bytearray(32 * _MB)
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        blocks.append(block)
    time.sleep(60)


def _start_up() -> None:
    """Does nothing; a spawned worker has to import this module to run it, which is the start-up cost."""


class ParsePool:
    """Parses files in ``workers`` spawned processes; ``parse`` may be called from several threads."""

    def __init__(self, workers: int = 1, timeout_s: float = 120.0, max_rss_mb: int = 1024) -> None:
        self._workers = workers
        self._timeout_s = timeout_s
        self._max_rss_mb = max_rss_mb
        self._lock = threading.Lock()  # never held while waiting for a worker
        self._closed = False
        self._executor: ProcessPoolExecutor | None = self._new_executor()
        self._ready: threading.Event | None = None  # set once the current executor's workers are running and limited
        self._jobs: list[int] = []  # job-object handles of the current executor's workers (Windows)
        self._warned = False

    def _new_executor(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=self._workers, mp_context=multiprocessing.get_context("spawn"))

    @staticmethod
    def _discard(executor: ProcessPoolExecutor, jobs: list[int]) -> None:
        """Kill every worker of ``executor``, abandon it and release its job objects."""
        for process in list((executor._processes or {}).values()):
            process.kill()
        executor.shutdown(wait=False, cancel_futures=True)
        if jobs:
            kernel32 = _win32()[0]
            for job in jobs:
                kernel32.CloseHandle(job)

    def _replace_executor(self, failed: ProcessPoolExecutor) -> None:
        """Discard ``failed`` and carry on with a fresh executor, unless the pool is closed.

        When another thread already replaced it (a kill breaks every in-flight call), the new one is kept.
        """
        with self._lock:
            if self._executor is not failed:
                return
            self._discard(failed, self._jobs)
            self._jobs = []
            if self._ready is not None:
                self._ready.set()  # callers waiting for these workers find the executor gone
                self._ready = None
            self._executor = self._new_executor()

    def _limit_memory(self, executor: ProcessPoolExecutor) -> None:
        """Windows: enforce the memory cap on the workers of ``executor`` once they are running."""
        if sys.platform != "win32":
            return
        with self._lock:
            if self._executor is not executor:
                return
            for pid in list((executor._processes or {}).keys()):
                try:
                    self._jobs.append(_limit_process_memory(pid, self._max_rss_mb))
                except OSError as e:
                    if not self._warned:
                        self._warned = True
                        log.warning("parse workers have no enforced memory limit, only the check after each file: %s", e)

    def parse(self, path: Path) -> ParsedDocument:
        """The parsed document; raises ``ParseTimeout``, ``ParseMemoryExceeded`` or the worker's ``ParseError``."""
        path = Path(path)
        try:
            with self._lock:
                executor = self._executor
                if executor is None:
                    raise ParseTimeout(path, "pool closed")
                ready = self._ready
                starter = ready is None
                if starter:
                    ready = self._ready = threading.Event()
                    starting = [executor.submit(_start_up) for _ in range(self._workers)]
            # Importing the parsers takes about half a second per process; that must not count against the
            # timeout of the first file, and the memory limit must be in force before any file is read.
            # So the first caller starts the workers and limits them while the others wait.
            if starter:
                try:
                    wait(starting, timeout=_START_UP_TIMEOUT_S)
                    self._limit_memory(executor)
                finally:
                    ready.set()
            else:
                ready.wait(_START_UP_TIMEOUT_S + 10)
            with self._lock:
                if self._executor is not executor:  # a failure elsewhere replaced it, or the pool was closed
                    raise ParseTimeout(path, self._reason("worker restarted"))
                future = executor.submit(_worker_fn, path, self._max_rss_mb)  # looked up now, so a test can patch it
            return future.result(timeout=self._timeout_s)
        except FutureTimeoutError:
            self._replace_executor(executor)
            raise ParseTimeout(path, self._reason("timeout")) from None
        except ParseMemoryExceeded:
            self._replace_executor(executor)
            raise
        except MemoryError as e:  # the job object refused an allocation inside the worker
            self._replace_executor(executor)
            raise ParseMemoryExceeded(f"{path}: the worker ran out of its {self._max_rss_mb} MB of growth") from e
        except (BrokenProcessPool, CancelledError) as e:
            self._replace_executor(executor)
            raise ParseTimeout(path, self._reason(f"worker died: {e!r}")) from e

    def _reason(self, default: str) -> str:
        return "pool closed" if self._closed else default

    def close(self) -> None:
        """Kill the workers and stop. Returns at once, even if a worker is in the middle of a file; the
        ``parse`` call waiting for it ends with ``ParseTimeout("pool closed")``. Closing twice does nothing."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executor, self._executor = self._executor, None
            jobs, self._jobs = self._jobs, []
            if self._ready is not None:
                self._ready.set()
        self._discard(executor, jobs)
