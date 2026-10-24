"""The host launcher: ``serve`` runs the always-on host for a data root, ``stop`` asks it to shut down.

``serve`` refuses a data root that ``validate_data_root`` rejects, and a folder that already holds files without the
ownership marker (exit 2); a root it takes on gets the marker. When a host already serves the root it only opens the
browser on it. Otherwise it takes the single-instance lock, records a doctor run, builds the services, starts a
supervisor for every project and serves the app with uvicorn, bound to 127.0.0.1 only and with uvicorn's own logging
off. The ``/auth`` URL carries the launch token: it is opened in the browser and, on a console, printed once, but
never written where a log is (so not at all when the std streams go to ``logs/host-stderr.log``). On exit, Ctrl+C
and Ctrl+Break included, the host stops its supervisors, closes the catalog, removes its run files, releases the
lock and, when the UI asked for a purge, removes the data root.

A purge removes only what this program keeps in a marked root, and folders only once they are empty: see
``remove_root``.

Native libraries (onnxruntime, tokenizers, numpy) are imported only by ``serve``, so ``stop`` and ``purge`` still
work on a machine where one of them is blocked; they read the catalog through a read-only ``sqlite3`` connection.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    lock_held,
    process_created,
    read_running,
    read_token,
    write_running,
    write_token,
)
from emailsupersearch.host.security import LAUNCH_HEADER, SessionStore, new_token
from emailsupersearch.host.sse import EventBus
from emailsupersearch.logging_setup import close_logging, configure_logging
from emailsupersearch.paths import (
    MARKER_FILE,
    data_root,
    ensure_layout,
    is_marked,
    mark_root,
    validate_data_root,
)

if TYPE_CHECKING:
    from emailsupersearch.catalog import Catalog
    from emailsupersearch.host.app import Services
    from emailsupersearch.index.embedder import OnnxEmbedder
    from emailsupersearch.ingest.chunker import Chunker
    from emailsupersearch.jobs.supervisor import ProjectSupervisor

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
GRACEFUL_SHUTDOWN_S = 5  # an open event stream is cut off after this long, so a stop never stalls on one
STOP_WAIT_S = 10.0  # how long stop waits for a starting host to serve, and then for a host to finish
STDERR_LOG = "host-stderr.log"

_STOP_POLL_S = 0.2
_SHUTDOWN_REQUEST_TIMEOUT_S = 5
_PURGE_RETRIES = 5  # after the first attempt: six tries in all, 0.2 s apart
_PURGE_RETRY_S = 0.2
_CATALOG_FILES = ("catalog.sqlite", "catalog.sqlite-wal", "catalog.sqlite-shm")
_RUN_FILES = (LOCK_FILE, RUNNING_FILE, TOKEN_FILE)
_FOLDERS = ("projects", "cache", "logs", "models")  # a purge removes these only once they are empty
_PROJECT_ID = re.compile(r"[A-Za-z0-9_-]+")  # anything else from the catalog is never joined onto a path
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
    rows = _read_catalog(default, "SELECT value FROM settings WHERE key = ?", ("data_root",))
    if not rows:
        return None
    try:
        value = json.loads(rows[0][0])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def _read_catalog(root: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
    """Rows from ``root``'s catalog through a read-only connection; none when the file or the table is missing.

    Read-only, and without the ``Catalog`` class: nothing is created, and no native library is loaded.
    """
    path = root / "catalog.sqlite"
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.info("the catalog could not be read: %s", type(e).__name__)
        return []


def _not_ours(root: Path) -> str | None:
    """Why ``serve`` must not take ``root`` on: it is a file, or a folder that holds files but no marker."""
    if not root.exists() or is_marked(root):
        return None
    if not root.is_dir():
        return "the data root is a file, not a folder"
    try:
        empty = next(root.iterdir(), None) is None
    except OSError:
        return "the data root folder cannot be read"
    if empty:
        return None
    return (
        "the data root folder already holds files but no eMailSuperSearch marker, so it is not a data root this"
        " program made; choose a new or empty folder"
    )


def remove_root(root: Path) -> str | None:
    """Remove a data root this program made, as far as it is the program's. Returns None once the root is gone,
    else why it is not.

    Refused, touching nothing, when the root has no ownership marker. Removed: the project folders the catalog
    lists, the catalog files, ``cache/parsed``, ``logs/host*.log*``, the run files and the marker (last); then
    ``projects``, ``cache``, ``logs``, ``models`` and the root itself, each only once empty, so anything else in
    them stays. A junction or symbolic link is removed itself, never followed. A failed removal is retried five
    times, 0.2 s apart, because Windows releases closed files late.
    """
    if not root.exists():
        return None
    if not is_marked(root):
        return "the folder carries no eMailSuperSearch marker, so nothing in it was removed"
    project_ids = [
        row[0]
        for row in _read_catalog(root, "SELECT id FROM projects")
        if isinstance(row[0], str) and _PROJECT_ID.fullmatch(row[0])
    ]
    for attempt in range(_PURGE_RETRIES + 1):
        try:
            _remove_owned(root, project_ids)
            break
        except OSError:
            if attempt == _PURGE_RETRIES:
                return "the data root could not be removed completely: a file in it is still in use"
            time.sleep(_PURGE_RETRY_S)
    _remove(root / MARKER_FILE)
    for name in _FOLDERS:
        with contextlib.suppress(OSError):  # not empty: it holds something this program did not put there
            (root / name).rmdir()
    if next(root.iterdir(), None) is not None:
        return "the data root holds files this program did not create; they were left in place"
    try:
        root.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        return "the data root folder could not be removed: it is in use"
    return None


def _remove_owned(root: Path, project_ids: list[str]) -> None:
    """Remove the program's own entries in ``root`` (see ``remove_root``); a folder that is a link goes as a link."""
    projects, cache, logs, models = (root / name for name in _FOLDERS)
    if _is_link(projects):
        _remove(projects)
    elif projects.is_dir():
        for project_id in project_ids:
            _remove(projects / project_id)
    if _is_link(cache):
        _remove(cache)
    elif cache.is_dir():
        _remove(cache / "parsed")
    if _is_link(logs):
        _remove(logs)
    elif logs.is_dir():
        for path in logs.glob("host*.log*"):
            if _is_link(path) or path.is_file():
                _remove(path)
    if _is_link(models):
        _remove(models)
    for name in (*_CATALOG_FILES, *_RUN_FILES):
        path = root / name
        if _is_link(path) or path.is_file():
            _remove(path)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _remove(path: Path) -> None:
    """Remove a file or a folder tree; a link is removed itself (``rmtree`` does not follow the links inside a tree
    either). A missing path is fine."""
    if _is_link(path):
        os.unlink(path)  # on Windows this removes a directory junction or symlink, never its target
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


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
    reason = validate_data_root(root) or _not_ours(root)
    if reason is not None:
        _say(reason, err=True)
        return 2
    redirected = headless or sys.stdout is None  # then stdout is a log file: the URL is never printed
    if redirected:
        redirect_std_streams_to_log(root)
    ensure_layout(root)
    mark_root(root)
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
        elif not redirected:
            _say(url)
        return 0
    if lock is None:
        _say("another instance is starting", err=True)
        return 1
    _run_host(root, catalog, lock, port, open_browser, redirected)
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
    root: Path, catalog: Catalog, lock: InstanceLock, port: int | None, open_browser: bool, redirected: bool
) -> None:
    switch = _ShutdownSwitch()
    services: Services | None = None
    with _break_raises_keyboard_interrupt():
        try:
            services = _build_services(root, catalog, switch)
            _start_supervisors(services)
            _serve_http(root, services, switch, port, open_browser, redirected)
        except KeyboardInterrupt:  # Ctrl+C or Ctrl+Break during start-up, or re-raised by uvicorn after its shutdown
            log.info("the host was interrupted")
        finally:
            _shut_down(root, catalog, lock, services, redirected)


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


@contextlib.contextmanager
def _break_raises_keyboard_interrupt() -> Iterator[None]:
    """On Windows, make SIGBREAK (Ctrl+Break, or closing the console) raise ``KeyboardInterrupt`` as Ctrl+C does.

    uvicorn re-raises a signal it caught into the handler it found once it has shut down; with the default SIGBREAK
    handler that would end the process before the cleanup ran. Handlers can only be set on the main thread.
    """
    if sys.platform != "win32" or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGBREAK, _raise_keyboard_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGBREAK, previous)


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
    """Start a supervisor for every project; one that cannot be built or started is recorded in
    ``blocked_projects``, with its store closed again."""
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
        try:
            sup.start()
        except Exception as e:
            log.warning("project %s could not be started: %s", project.id, type(e).__name__)
            log.debug("starting the supervisor failed", exc_info=True)
            services.blocked_projects[project.id] = type(e).__name__
            with contextlib.suppress(Exception):
                sup.stop()  # closes its store, so the blocked project can still be deleted
            continue
        services.supervisors[project.id] = sup


def _serve_http(
    root: Path,
    services: Services,
    switch: _ShutdownSwitch,
    port: int | None,
    open_browser: bool,
    redirected: bool,
) -> None:
    """Serve the app on 127.0.0.1 until the server is asked to exit (``/api/shutdown``, a purge, Ctrl+C or
    Ctrl+Break)."""
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
    write_running(root, RunningInfo(os.getpid(), chosen, process_created(os.getpid())))
    url = _auth_url(chosen, services.launch_token)
    if open_browser:
        webbrowser.open_new_tab(url)
    if not redirected:
        _say(url)
    log.info("the host is serving on port %d", chosen)
    # On the main thread uvicorn handles Ctrl+C and Ctrl+Break itself; on any other thread should_exit is the only
    # way out.
    server.run()


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
    """Close the package logger's files and detach the std streams from the log folder (the files live in the root
    and Windows will not delete an open file), then remove the root."""
    close_logging()
    if redirected:
        _detach_std_streams()
    problem = remove_root(root)
    if problem is not None:
        _say(f"warning: {problem}", err=True)


# ---- stop ----


def stop(root: Path) -> int:
    """Ask the host serving ``root`` to shut down and wait for it to finish: 0 when it has (or none was running),
    1 when it is still running or still starting. A host holding the lock without having written ``host.json`` is
    starting: ``stop`` waits up to 10 s for it to serve first."""
    code = _stop_host(root)
    if code is None:
        _say("not running")
        return 0
    return code


def stop_any(roots: Iterable[Path]) -> int:
    """``stop`` for every root in ``roots`` (each once): 0 only when no host is left running in any of them."""
    codes = [code for root in dict.fromkeys(roots) if (code := _stop_host(root)) is not None]
    if not codes:
        _say("not running")
        return 0
    return max(codes)


def _stop_host(root: Path) -> int | None:
    """None when no host runs or starts in ``root``; else 0 once it has finished and 1 when it has not."""
    running = read_running(root)
    if running is None:
        if not lock_held(root):
            return None
        running = _await_serving(root)
        if running is None:
            if not lock_held(root):  # it gave up starting
                return None
            _say("the host is still starting; try again shortly", err=True)
            return 1
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


def _await_serving(root: Path) -> RunningInfo | None:
    """Wait up to ``STOP_WAIT_S`` for a starting host to write ``host.json``; None when it did not, or when it let
    go of the lock instead."""
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(_STOP_POLL_S)
        running = read_running(root)
        if running is not None or not lock_held(root):
            return running
    return None
