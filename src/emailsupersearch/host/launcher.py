"""The host launcher: ``serve`` runs the always-on host for a data root, ``stop`` asks it to shut down.

``serve`` refuses a data root that ``validate_data_root`` rejects (exit 2). When a host already serves the root it
only opens the browser on it. Otherwise it takes the single-instance lock, records a doctor run, builds the services,
starts a supervisor for every project and serves the app with uvicorn, bound to 127.0.0.1 only and with uvicorn's
own logging off. The ``/auth`` URL carries the launch token: it is opened in the browser and, with a console, printed
once, but never logged. On exit the host stops its supervisors, closes the catalog, removes its run files, releases
the lock and, when the UI asked for a purge, removes the data root.

Native libraries (onnxruntime, tokenizers, numpy) are imported only by ``serve``, so ``stop`` and ``purge`` still
work on a machine where one of them is blocked.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import sqlite3
import sys
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

import emailsupersearch
from emailsupersearch.diagnostics.doctor import report_json, run_doctor
from emailsupersearch.host.lock import (
    LOCK_FILE,
    RUNNING_FILE,
    TOKEN_FILE,
    InstanceLock,
    RunningInfo,
    acquire,
    clear,
    host_exited,
    read_running,
    read_token,
    write_running,
    write_token,
)
from emailsupersearch.host.security import LAUNCH_HEADER, SessionStore, new_token
from emailsupersearch.host.sse import EventBus
from emailsupersearch.logging_setup import LOGGER_NAME, configure_logging
from emailsupersearch.paths import data_root, ensure_layout, validate_data_root

if TYPE_CHECKING:
    from emailsupersearch.catalog import Catalog
    from emailsupersearch.host.app import Services
    from emailsupersearch.index.embedder import OnnxEmbedder
    from emailsupersearch.ingest.chunker import Chunker
    from emailsupersearch.jobs.supervisor import ProjectSupervisor

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
GRACEFUL_SHUTDOWN_S = 5  # an open event stream is cut off after this long, so a stop never stalls on one
STOP_WAIT_S = 10.0
STDERR_LOG = "host-stderr.log"

_STOP_POLL_S = 0.2
_SHUTDOWN_REQUEST_TIMEOUT_S = 5
_PURGE_RETRIES = 5  # after the first attempt: six tries in all, 0.2 s apart
_PURGE_RETRY_S = 0.2
# Everything this program keeps in a data root. A purge removes these and then the root, if nothing else is left.
_ROOT_ENTRIES = (
    "projects",
    "cache",
    "models",
    "logs",
    "catalog.sqlite",
    "catalog.sqlite-wal",
    "catalog.sqlite-shm",
    "catalog.sqlite-journal",
    LOCK_FILE,
    RUNNING_FILE,
    TOKEN_FILE,
)
# Loopback requests must never go through a proxy configured for the machine.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---- the data root ----


def resolve_root() -> Path:
    """The data root to run from: ``EMAILSUPERSEARCH_DATA_ROOT``; else the ``data_root`` setting in the default
    root's catalog, when it is an absolute path ``validate_data_root`` accepts (ignored with a warning otherwise);
    else the default root. ``serve`` validates the result again."""
    default = data_root()
    if os.environ.get("EMAILSUPERSEARCH_DATA_ROOT"):
        return default
    stored = _stored_data_root(default)
    if stored is None:
        return default
    candidate = Path(stored)
    reason = validate_data_root(candidate) if candidate.is_absolute() else "it is not an absolute path"
    if reason is not None:
        log.warning("the data_root setting is ignored: %s", reason)
        return default
    return candidate


def _stored_data_root(default: Path) -> str | None:
    """The ``data_root`` setting of the default root's catalog, or None when there is no catalog or no setting."""
    if not (default / "catalog.sqlite").is_file():  # never create the default root just to read a setting
        return None
    from emailsupersearch.catalog import Catalog

    try:
        catalog = Catalog(default)
    except sqlite3.Error as e:
        log.warning("the default catalog could not be read: %s", type(e).__name__)
        return None
    try:
        value = catalog.get_setting("data_root")
    finally:
        catalog.close()
    return value if isinstance(value, str) and value.strip() else None


def remove_root(root: Path) -> str | None:
    """Remove the data root: everything this program keeps there, then the folder itself if nothing else is left.

    Files the owner put in the folder are left alone. A failed removal is retried five times, 0.2 s apart, because
    Windows releases closed files late. Returns None once the root is gone, else why it is not.
    """
    for attempt in range(_PURGE_RETRIES + 1):
        try:
            _remove_entries(root)
            break
        except OSError:
            if attempt == _PURGE_RETRIES:
                return "the data root could not be removed completely: a file in it is still in use"
            time.sleep(_PURGE_RETRY_S)
    if _holds_other_files(root):
        return "the data root holds files this program did not create; they were left in place"
    try:
        root.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        return "the data root folder could not be removed: it is in use"
    return None


def _remove_entries(root: Path) -> None:
    for name in _ROOT_ENTRIES:
        path = root / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def _holds_other_files(root: Path) -> bool:
    try:
        return any(entry.name not in _ROOT_ENTRIES for entry in root.iterdir())
    except OSError:
        return False


# ---- console and std streams ----


def _say(text: str, err: bool = False) -> None:
    """Print a line to stdout (or stderr) when the process has one: under ``pythonw`` it has neither."""
    stream = sys.stderr if err else sys.stdout
    if stream is not None:
        print(text, file=stream, flush=True)


def redirect_std_streams_to_log(root: Path) -> None:
    """Send fds 1 and 2, and ``sys.stdout`` and ``sys.stderr``, to ``logs/host-stderr.log``, appending.

    For a host without a console (``pythonw``) or started ``--headless``: what uvicorn or a native library writes to
    the std streams lands in the log folder instead of nowhere.
    """
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / STDERR_LOG, "ab") as target:
        os.dup2(target.fileno(), 1)
        os.dup2(target.fileno(), 2)
    # Text wrappers over the duplicated fds themselves, so Python and native writes go to the same file.
    sys.stdout = open(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)  # noqa: SIM115
    sys.stderr = open(2, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)  # noqa: SIM115


def _detach_std_streams() -> None:
    """Point fds 1 and 2 at the null device, so the purge can delete ``logs/host-stderr.log`` (Windows refuses to
    delete a file that is open)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.flush()
    with open(os.devnull, "wb") as null:
        os.dup2(null.fileno(), 1)
        os.dup2(null.fileno(), 2)


# ---- port ----


def choose_port(preferred: int) -> int:
    """``preferred`` when it can be bound on 127.0.0.1, else a free port the OS assigns."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _auth_url(port: int, token: str | None) -> str:
    return f"http://127.0.0.1:{port}/auth?token={token or ''}"


# ---- serve ----


def serve(root: Path, port: int | None, headless: bool, open_browser: bool = True) -> int:
    """Run the host for ``root`` until it is stopped, or open the browser on the host already running; see the
    module docstring. Exit codes: 0 done, 1 another instance is starting, 2 the data root is refused."""
    reason = validate_data_root(root)
    if reason is not None:
        _say(reason, err=True)
        return 2
    redirected = headless or sys.stdout is None
    if redirected:
        redirect_std_streams_to_log(root)
    ensure_layout(root)
    from emailsupersearch.catalog import Catalog

    catalog = Catalog(root)
    lock = running = None
    try:
        configure_logging("host", root, catalog.get_setting("log_level", "INFO"))
        running = read_running(root)
        if running is None:
            lock = acquire(root)
    finally:
        if lock is None:
            catalog.close()
    if running is not None:
        url = _auth_url(running.port, read_token(root))
        if open_browser:
            webbrowser.open_new_tab(url)
        elif not headless:
            _say(url)
        return 0
    if lock is None:
        _say("another instance is starting", err=True)
        return 1
    _run_host(root, catalog, lock, port, headless, open_browser, redirected)
    return 0


class _ShutdownSwitch:
    """``Services.request_shutdown``: asks the uvicorn server to exit. The server is set once it exists, before any
    request can arrive."""

    def __init__(self) -> None:
        self.server: uvicorn.Server | None = None

    def __call__(self) -> None:
        if self.server is not None:
            self.server.should_exit = True


def _run_host(
    root: Path,
    catalog: Catalog,
    lock: InstanceLock,
    port: int | None,
    headless: bool,
    open_browser: bool,
    redirected: bool,
) -> None:
    switch = _ShutdownSwitch()
    services: Services | None = None
    try:
        services = _build_services(root, catalog, switch)
        _start_supervisors(services)
        _serve_http(root, services, switch, port, headless, open_browser)
    finally:
        _shut_down(root, catalog, lock, services, redirected)


def make_start_supervisor(
    catalog: Catalog,
    embedder: OnnxEmbedder | None,
    chunker: Chunker,
    cache_dir: Path,
    bus: EventBus,
) -> Callable[[str], ProjectSupervisor]:
    """``Services.start_supervisor``: open the project's store and wire its supervisor, not yet started. When the
    wiring fails the store is closed before the error propagates, so the blocked project can still be deleted."""
    from emailsupersearch.jobs.supervisor import build_project_supervisor

    def start_supervisor(project_id: str) -> ProjectSupervisor:
        store = catalog.open_store(project_id)
        try:
            return build_project_supervisor(
                project_id, store, embedder, chunker, cache_dir, lambda ev: bus.publish(f"sync:{project_id}", ev)
            )
        except BaseException:
            store.close()
            raise

    return start_supervisor


def _build_services(root: Path, catalog: Catalog, switch: _ShutdownSwitch) -> Services:
    from emailsupersearch.host.app import Services
    from emailsupersearch.index.embedder import load_default_embedder, tokenizer_path
    from emailsupersearch.ingest.chunker import Chunker

    embedder = load_default_embedder()
    if embedder is not None:
        from emailsupersearch_models_en import embedding_dir

        chunker = Chunker(tokenizer_path(embedding_dir()))
    else:
        chunker = Chunker(None)
    catalog.record_doctor_run(report_json(run_doctor(root)))
    bus = EventBus()
    token = new_token()
    write_token(root, token)
    bundle = Path(emailsupersearch.__file__).parent / "ui" / "dist"
    return Services(
        catalog=catalog,
        embedder=embedder,
        chunker=chunker,
        supervisors={},
        bus=bus,
        launch_token=token,
        sessions=SessionStore(),
        start_supervisor=make_start_supervisor(catalog, embedder, chunker, root / "cache" / "parsed", bus),
        static_dir=bundle if bundle.is_dir() else None,
        request_shutdown=switch,
        blocked_projects={},
    )


def _start_supervisors(services: Services) -> None:
    """Start a supervisor for every project; one that cannot be built is recorded in ``blocked_projects``."""
    from emailsupersearch.db.schema import SchemaTooNew

    for project in services.catalog.list_projects():
        try:
            sup = services.start_supervisor(project.id)
        except SchemaTooNew:
            log.warning("project %s was written by a newer version; it stays closed", project.id)
            services.blocked_projects[project.id] = "schema too new"
            continue
        except Exception as e:
            log.warning("project %s could not be opened: %s", project.id, type(e).__name__)
            log.debug("opening the project failed", exc_info=True)
            services.blocked_projects[project.id] = type(e).__name__
            continue
        services.supervisors[project.id] = sup
        sup.start()


def _serve_http(
    root: Path,
    services: Services,
    switch: _ShutdownSwitch,
    port: int | None,
    headless: bool,
    open_browser: bool,
) -> None:
    """Serve the app on 127.0.0.1 until the server is asked to exit (``/api/shutdown``, a purge, or Ctrl+C)."""
    from emailsupersearch.host.app import create_app

    chosen = choose_port(port or int(services.catalog.get_setting("port", DEFAULT_PORT)))
    config = uvicorn.Config(
        create_app(services),
        host="127.0.0.1",
        port=chosen,
        log_config=None,  # uvicorn's loggers stay unconfigured and
        access_log=False,  # no request line is ever logged: the /auth URL carries the launch token
        lifespan="on",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    server = uvicorn.Server(config)
    switch.server = server
    write_running(root, RunningInfo(os.getpid(), chosen))
    url = _auth_url(chosen, services.launch_token)
    if open_browser:
        webbrowser.open_new_tab(url)
    if not headless:
        _say(url)
    log.info("the host is serving on port %d", chosen)
    try:
        # On the main thread uvicorn handles Ctrl+C itself; on any other thread should_exit is the only way out.
        server.run()
    except KeyboardInterrupt:  # uvicorn re-raises the Ctrl+C it caught once it has shut down
        pass


def _shut_down(
    root: Path, catalog: Catalog, lock: InstanceLock, services: Services | None, redirected: bool
) -> None:
    """Stop the supervisors (each closes its store), close the catalog, remove the run files, release the lock and,
    when a purge was requested, remove the data root."""
    supervisors = services.supervisors if services is not None else {}
    try:
        for project_id, sup in list(supervisors.items()):
            try:
                sup.stop()
            except Exception as e:  # one supervisor must not keep the others, or the lock, held
                log.warning("stopping project %s failed: %s", project_id, type(e).__name__)
                log.debug("stopping the supervisor failed", exc_info=True)
        catalog.close()
    finally:
        clear(root)
        lock.release()
    log.info("the host has stopped")
    if services is not None and services.purge_requested:
        _purge_on_exit(root, redirected)


def _purge_on_exit(root: Path, redirected: bool) -> None:
    """Close every handler of the package logger and detach the std streams from the log folder (the files live in
    the root and Windows will not delete an open file), then remove the root."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    if redirected:
        _detach_std_streams()
    problem = remove_root(root)
    if problem is not None:
        _say(f"warning: {problem}", err=True)


# ---- stop ----


def stop(root: Path) -> int:
    """Ask the host serving ``root`` to shut down and wait up to 10 s for it to finish: 0 when it has (or none was
    running), 1 when it is still running."""
    running = read_running(root)
    if running is None:
        _say("not running")
        return 0
    request = urllib.request.Request(
        f"http://127.0.0.1:{running.port}/api/shutdown",
        method="POST",
        headers={LAUNCH_HEADER: read_token(root) or ""},
    )
    try:
        with _LOOPBACK.open(request, timeout=_SHUTDOWN_REQUEST_TIMEOUT_S):
            pass
    except OSError as e:  # it may be exiting already; whether it finishes is what counts
        log.info("the shutdown request failed: %s", type(e).__name__)
    deadline = time.monotonic() + STOP_WAIT_S
    while not host_exited(root, running):
        if time.monotonic() >= deadline:
            _say("still running", err=True)
            return 1
        time.sleep(_STOP_POLL_S)
    return 0
