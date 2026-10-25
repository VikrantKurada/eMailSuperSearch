"""The launcher: the single-instance lock, the port choice, and serve and stop against a real uvicorn server.

Tests that serve run ``serve`` on a daemon thread of this process and always stop it. Its stream redirection is
recorded instead of done there (it would ``dup2`` over pytest's own fds 1 and 2); the real one runs in a child
process, as do the headless purge, the console-less start and Ctrl+Break.
"""

import contextlib
import http.cookiejar
import json
import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import wait_until

from emailsupersearch import cli, logging_setup
from emailsupersearch.catalog import Catalog
from emailsupersearch.host import launcher
from emailsupersearch.host import lock as lock_module
from emailsupersearch.host.launcher import (
    DEFAULT_PORT,
    choose_port,
    remove_root,
    resolve_root,
    serve,
    stop,
)
from emailsupersearch.host.lock import (
    RunningInfo,
    acquire,
    clear,
    process_created,
    read_running,
    read_token,
    write_running,
    write_token,
)
from emailsupersearch.host.sse import EventBus
from emailsupersearch.paths import MARKER_FILE, ensure_layout, is_marked, mark_root

_START_S = 60  # serve's start-up runs the doctor and loads the embedder
_EXIT_S = 10  # serve must have returned this long after stop was called
# Loopback requests must never go through a proxy configured for the machine.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture(autouse=True)
def _detach_host_log():
    """Close the log handler serve attaches, so later tests do not write to this test's ``host.log``."""
    yield
    logging_setup.close_logging()
    logging.getLogger(logging_setup.LOGGER_NAME).setLevel(logging.NOTSET)


@pytest.fixture
def redirects(monkeypatch):
    """The roots serve asked to redirect the std streams for; the redirection itself is not done in this process."""
    calls: list[Path] = []
    monkeypatch.setattr(launcher, "redirect_std_streams_to_log", calls.append)
    return calls


@pytest.fixture
def must_refuse(monkeypatch):
    """Fail, rather than serve forever on the test's own thread, should serve accept the root."""

    def run_host(*args, **kwargs):
        raise AssertionError("serve accepted the data root")

    monkeypatch.setattr(launcher, "_run_host", run_host)


@dataclass
class Host:
    info: RunningInfo
    thread: threading.Thread
    result: dict


def _health(port: int) -> bool:
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.load(r) == {"ok": True}
    except OSError:
        return False


@contextlib.contextmanager
def running_host(root: Path, headless: bool = True):
    """``serve(root)`` on a daemon thread, once it answers ``/health``; stopped on the way out if still running."""
    result: dict = {}

    def run() -> None:
        result["code"] = serve(root, None, headless=headless, open_browser=False)

    thread = threading.Thread(target=run, name="test-host", daemon=True)
    thread.start()
    try:
        info = wait_until(lambda: read_running(root) or not thread.is_alive(), _START_S)
        assert thread.is_alive(), "serve returned before it was serving"
        wait_until(lambda: _health(info.port), 10)
        yield Host(info, thread, result)
    finally:
        if thread.is_alive():
            stop(root)
            thread.join(_EXIT_S)


@contextlib.contextmanager
def child_host(args: list[str], root: Path, env: dict | None = None, **popen):
    """A host in a child process (``python <args>``) serving ``root``, once it answers ``/health``; killed on the
    way out if still running. Yields ``(child, info)``.

    The pid in ``info`` is not ``child.pid``: the venv's python.exe runs the real interpreter as its own child.
    """
    child = subprocess.Popen(
        [sys.executable, *args], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen
    )
    host_pid = None
    try:
        info = wait_until(lambda: read_running(root) or child.poll() is not None, _START_S)
        assert child.poll() is None, "the host exited before it was serving"
        host_pid = info.pid
        wait_until(lambda: _health(info.port), 10)
        yield child, info
    finally:
        if child.poll() is None:
            if host_pid is not None:
                with contextlib.suppress(OSError):
                    os.kill(host_pid, signal.SIGTERM)  # TerminateProcess on Windows
            child.kill()
            child.wait(timeout=30)


def _session_opener(base: str, token: str) -> tuple[urllib.request.OpenerDirector, str]:
    """An opener holding the session cookies from ``/auth``, and the CSRF value to send with each POST."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
    opener.open(f"{base}/auth?token={token}", timeout=10).close()  # follows the 303 to "/"
    return opener, next(c.value for c in jar if c.name == "ess_csrf")


def _post_json(opener, url: str, csrf: str, body: dict | None = None):
    data = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json", "X-ESS-Token": csrf}
    )
    return opener.open(request, timeout=30)


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=60)
    return child.pid


def _owned_root(root: Path, projects: int = 1) -> list[str]:
    """A data root as ``serve`` initialises it, holding ``projects`` projects; their ids."""
    ensure_layout(root)
    mark_root(root)
    catalog = Catalog(root)
    try:
        return [catalog.create_project(f"P{n}", []).id for n in range(projects)]
    finally:
        catalog.close()


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ---- port, lock and run files ----


def test_choose_port_falls_back_when_busy():
    busy = socket.socket()
    try:
        try:
            busy.bind(("127.0.0.1", DEFAULT_PORT))
        except OSError:
            pytest.skip(f"port {DEFAULT_PORT} is already in use on this machine")
        busy.listen()
        port = choose_port(DEFAULT_PORT)
        assert port != DEFAULT_PORT and port > 0
        with socket.socket() as probe:  # the fallback is really free
            probe.bind(("127.0.0.1", port))
    finally:
        busy.close()
    assert choose_port(DEFAULT_PORT) == DEFAULT_PORT


def test_lock_is_exclusive_and_stale_detected(tmp_path):
    lock = acquire(tmp_path)
    assert lock is not None and lock.pid == os.getpid()
    assert (tmp_path / "host.lock").read_text() == f"{os.getpid()}\n{process_created(os.getpid())}"
    assert acquire(tmp_path) is None  # held by a live process: this one
    lock.release()
    assert not (tmp_path / "host.lock").exists()
    lock.release()  # a second release is harmless

    for stale in (str(_dead_pid()), "not a pid", ""):
        (tmp_path / "host.lock").write_text(stale)
        replaced = acquire(tmp_path)
        assert replaced is not None and replaced.pid == os.getpid(), stale
        replaced.release()

    write_running(tmp_path, RunningInfo(os.getpid(), 4321))  # no creation time recorded: liveness alone counts
    assert read_running(tmp_path) == RunningInfo(os.getpid(), 4321)
    write_running(tmp_path, RunningInfo(_dead_pid(), 4321))
    assert read_running(tmp_path) is None
    (tmp_path / "host.json").write_text("{not json")
    assert read_running(tmp_path) is None


@pytest.mark.skipif(sys.platform != "win32", reason="the creation time is recorded on Windows only")
def test_reused_pid_is_stale(tmp_path):
    created = process_created(os.getpid())
    assert created > 0
    # This very process, but created at another time: the pid of a dead host, since given to another program.
    (tmp_path / "host.lock").write_text(f"{os.getpid()}\n{created + 1}")
    lock = acquire(tmp_path)
    assert lock is not None and (tmp_path / "host.lock").read_text() == f"{os.getpid()}\n{created}"
    lock.release()
    write_running(tmp_path, RunningInfo(os.getpid(), 4321, created + 1))
    assert read_running(tmp_path) is None
    write_running(tmp_path, RunningInfo(os.getpid(), 4321, created))
    assert read_running(tmp_path) == RunningInfo(os.getpid(), 4321, created)


def test_token_file_is_user_only(tmp_path):
    assert read_token(tmp_path) is None
    write_token(tmp_path, "secret-value")
    assert read_token(tmp_path) == "secret-value"
    write_token(tmp_path, "second-value")  # a restart overwrites it
    assert read_token(tmp_path) == "second-value"
    if sys.platform == "win32":
        acl = subprocess.run(
            ["icacls", str(tmp_path / "host.token")], check=False, capture_output=True, text=True, timeout=30
        ).stdout
        assert os.environ["USERNAME"].lower() in acl.lower() and "(I)" not in acl  # nothing inherited


# ---- serve and stop ----


def test_serve_refuses_onedrive_root(capsys, must_refuse):
    assert serve(Path(r"C:\Users\x\OneDrive\ess"), None, True, False) == 2
    assert "OneDrive" in capsys.readouterr().err
    assert not Path(r"C:\Users\x\OneDrive\ess").exists()


def test_serve_refuses_nonempty_unmarked_root(tmp_path, redirects, capsys, must_refuse):
    root = tmp_path / "work"
    root.mkdir()
    (root / "report.docx").write_text("the owner's own file")
    assert serve(root, None, True, False) == 2
    err = capsys.readouterr().err
    assert "marker" in err and str(tmp_path) not in err
    assert _tree(root) == ["report.docx"] and redirects == []


def test_serve_then_stop_roundtrip(tmp_path, redirects):
    root = tmp_path / "root"
    with running_host(root) as host:
        assert redirects == [root]  # headless: the std streams go to the log
        assert (root / "host.lock").read_text().split()[0] == str(os.getpid())
        assert is_marked(root)
        started = time.monotonic()
        assert stop(root) == 0
        host.thread.join(_EXIT_S)
        assert not host.thread.is_alive() and time.monotonic() - started < _EXIT_S
        assert host.result["code"] == 0
    assert not (root / "host.json").exists() and not (root / "host.lock").exists()
    assert stop(root) == 0  # nothing left to stop


def test_second_serve_opens_existing_instance(tmp_path, redirects, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open_new_tab", opened.append)
    root = tmp_path / "root"
    with running_host(root) as host:
        assert serve(root, None, headless=True, open_browser=True) == 0
        assert opened == [f"http://127.0.0.1:{host.info.port}/auth?token={read_token(root)}"]
        assert host.thread.is_alive() and read_running(root) == host.info  # the first host carries on
        assert stop(root) == 0


def test_auth_url_not_in_logs(tmp_path, redirects, capsys):
    root = tmp_path / "root"
    with running_host(root, headless=False) as host:
        token = read_token(root)
        assert stop(root) == 0
        host.thread.join(_EXIT_S)
    printed = capsys.readouterr().out
    assert printed.count("/auth?token=") == 1  # the console is the one place the URL goes
    logs = [p for p in (root / "logs").rglob("*") if p.is_file()]
    assert logs
    for path in logs:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "/auth?token=" not in text and token not in text, path.name


def test_url_not_logged_without_a_console(tmp_path):
    """Under pythonw there is no stdout: the std streams go to the log, so the URL must not be printed, by the host
    or by a second launch that finds it running."""
    root = tmp_path / "root"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from emailsupersearch.host.launcher import serve\n"
        "sys.stdout = None\n"
        f"raise SystemExit(serve(Path({str(root)!r}), None, headless=False, open_browser=False))\n"
    )
    with child_host(["-c", script], root) as (child, _):
        second = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, timeout=120)
        assert second.returncode == 0  # it found the running host
        token = read_token(root)
        assert stop(root) == 0
        assert child.wait(timeout=30) == 0
    assert (root / "logs" / "host-stderr.log").is_file()
    for path in (p for p in (root / "logs").rglob("*") if p.is_file()):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "/auth?token=" not in text and token not in text, path.name


def test_redirect_std_streams_writes_to_log(tmp_path):
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from emailsupersearch.host.launcher import redirect_std_streams_to_log\n"
        "sys.stdout = None\n"
        f"redirect_std_streams_to_log(Path({str(tmp_path)!r}))\n"
        "print('marker-stdout')\n"
        "print('marker-stderr', file=sys.stderr)\n"
        "os.write(2, b'marker-fd2\\n')\n"
    )
    done = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout == b"" and done.stderr == b""  # nothing reached the original streams
    text = (tmp_path / "logs" / "host-stderr.log").read_text(encoding="utf-8")
    assert "marker-stdout" in text and "marker-stderr" in text and "marker-fd2" in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console control events")
def test_ctrl_break_runs_cleanup(tmp_path):
    root = tmp_path / "root"
    env = {**os.environ, "EMAILSUPERSEARCH_DATA_ROOT": str(root)}
    args = ["-m", "emailsupersearch", "serve", "--no-browser"]
    with child_host(args, root, env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) as (child, _):
        try:
            os.kill(child.pid, signal.CTRL_BREAK_EVENT)  # to the child's process group, the real interpreter included
        except OSError:
            pytest.skip("this test process has no console to send Ctrl+Break through")
        assert child.wait(timeout=30) == 0
    assert not (root / "host.json").exists() and not (root / "host.lock").exists()


def test_stop_with_open_sse_stream_exits_within_10s(tmp_path, redirects):
    root = tmp_path / "root"
    mail = tmp_path / "mail"
    mail.mkdir()
    with running_host(root) as host:
        base = f"http://127.0.0.1:{host.info.port}"
        opener, csrf = _session_opener(base, read_token(root))
        with _post_json(opener, f"{base}/api/projects", csrf, {"name": "Mail", "folders": [str(mail)]}) as r:
            assert r.status == 201
            project_id = json.load(r)["id"]
        first_line: queue.Queue = queue.Queue()
        ended: list[str] = []

        def read_stream() -> None:
            try:
                with opener.open(f"{base}/api/projects/{project_id}/sync/events", timeout=60) as r:
                    first_line.put(r.readline())
                    while r.read(1):  # hold the stream open until the server ends it
                        pass
                ended.append("closed")
            except Exception as e:  # noqa: BLE001 - the server may cut the stream off mid-chunk at shutdown
                ended.append(type(e).__name__)

        reader = threading.Thread(target=read_stream, name="test-sse-reader", daemon=True)
        reader.start()
        assert first_line.get(timeout=30).startswith(b"event: status")
        started = time.monotonic()
        assert stop(root) == 0
        host.thread.join(_EXIT_S)
        assert not host.thread.is_alive() and time.monotonic() - started < _EXIT_S
        reader.join(_EXIT_S)
        assert not reader.is_alive() and ended
    assert not (root / "host.json").exists() and not (root / "host.lock").exists()


_WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete an open file")


@_WINDOWS_ONLY
def test_release_waits_for_a_reader_to_close_the_lock(tmp_path):
    lock = acquire(tmp_path)
    with open(lock.path, "rb") as reader:  # a concurrent `stop` reading host.lock, as in CI's failing run
        closer = threading.Timer(0.2, reader.close)
        closer.start()
        try:
            lock.release()  # used to raise PermissionError [WinError 32] and leave the lock behind
        finally:
            closer.join()
    assert not lock.path.exists()


@_WINDOWS_ONLY
def test_clear_waits_for_a_reader_to_close_host_json(tmp_path):
    write_running(tmp_path, RunningInfo(os.getpid(), 8765, 0))
    with open(tmp_path / "host.json", "rb") as reader:
        closer = threading.Timer(0.2, reader.close)
        closer.start()
        try:
            clear(tmp_path)  # a raise here used to skip lock.release() in serve's shutdown
        finally:
            closer.join()
    assert not (tmp_path / "host.json").exists()


@_WINDOWS_ONLY
def test_release_never_raises_when_the_lock_stays_open(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(lock_module, "_UNLINK_TRIES", 2)
    lock = acquire(tmp_path)
    with open(lock.path, "rb"), caplog.at_level(logging.WARNING, logger="emailsupersearch.host.lock"):
        lock.release()
    assert "could not be removed" in caplog.text and lock.path.exists()
    lock.release()  # once nothing holds it open the file goes
    assert not lock.path.exists()


def test_stop_waits_for_a_starting_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "START_WAIT_S", 0.6)
    lock = acquire(tmp_path)  # a host that holds the lock but has not written host.json yet
    try:
        started = time.monotonic()
        assert stop(tmp_path) == 1
        assert time.monotonic() - started >= 0.5 and "starting" in capsys.readouterr().err
        gives_up = threading.Timer(0.2, lock.release)  # it stops starting: nothing is left to stop
        gives_up.start()
        assert stop(tmp_path) == 0
        gives_up.join()
    finally:
        lock.release()


def test_stop_waits_for_the_process_to_exit_and_prints_dots(tmp_path, monkeypatch, capsys):
    assert launcher.STOP_WAIT_S == 90 and launcher._STOP_DOT_S == 5
    monkeypatch.setattr(launcher, "STOP_WAIT_S", 0.7)
    monkeypatch.setattr(launcher, "_STOP_DOT_S", 0.15)
    lock = acquire(tmp_path)  # a host of this very process: it has exited once it lets go of its lock
    try:
        write_running(tmp_path, RunningInfo(os.getpid(), choose_port(0), process_created(os.getpid())))
        started = time.monotonic()
        assert stop(tmp_path) == 1  # it never exits: nothing answers the shutdown request, the lock stays
        out, err = capsys.readouterr()
        assert time.monotonic() - started >= 0.7 and out.count(".") >= 3 and "still running" in err
        assert "stopped" not in out
        exits = threading.Timer(0.4, lock.release)
        exits.start()
        assert stop(tmp_path) == 0
        exits.join()
        out, _ = capsys.readouterr()
        assert out.rstrip().endswith("stopped") and "." in out
    finally:
        lock.release()


def test_read_only_uri_handles_drive_and_unc_paths():
    assert launcher.read_only_uri(Path(r"C:\Users\me\data\catalog.sqlite")) == (
        "file:///C:/Users/me/data/catalog.sqlite?mode=ro"
    )
    assert launcher.read_only_uri(Path(r"\\server\share\ess data\catalog.sqlite")) == (
        "file:////server/share/ess%20data/catalog.sqlite?mode=ro"
    )


def test_catalog_on_a_unc_path_is_read(tmp_path):
    Catalog(tmp_path).close()
    drive, rest = os.path.splitdrive(str(tmp_path))
    unc = Path(rf"\\localhost\{drive[0]}$" + rest)  # the same folder through the administrative share
    try:
        reachable = unc.is_dir()
    except OSError:
        reachable = False
    if not reachable:
        pytest.skip(r"the administrative share \\localhost\C$ is not reachable here")
    assert launcher._read_catalog(unc, "SELECT COUNT(*) FROM projects") == [(0,)]


def test_purge_stops_a_host_on_either_root(tmp_path, monkeypatch):
    monkeypatch.delenv("EMAILSUPERSEARCH_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    default = tmp_path / "local" / "eMailSuperSearch"
    _owned_root(default, projects=0)
    catalog = Catalog(default)
    try:
        catalog.set_setting("data_root", str(tmp_path / "next"))
    finally:
        catalog.close()
    asked: list[list[Path]] = []
    monkeypatch.setattr(launcher, "stop_any", lambda roots: asked.append(list(roots)) or 1)
    assert cli.main(["purge", "--yes"]) == 1  # the stub says a host is still running: nothing is removed
    assert asked == [[tmp_path / "next", default]] and (default / "catalog.sqlite").is_file()


def test_cli_stop_also_finds_a_host_on_the_default_root(tmp_path, monkeypatch, redirects):
    monkeypatch.delenv("EMAILSUPERSEARCH_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    default = tmp_path / "local" / "eMailSuperSearch"
    _owned_root(default, projects=0)
    catalog = Catalog(default)
    try:
        catalog.set_setting("data_root", str(tmp_path / "next"))  # takes effect at the next start
    finally:
        catalog.close()
    with running_host(default) as host:
        assert resolve_root() == tmp_path / "next"
        assert cli.main(["stop"]) == 0
        host.thread.join(_EXIT_S)
        assert not host.thread.is_alive()
    assert not (default / "host.json").exists()


def test_headless_purge_removes_the_data_root(tmp_path):
    """The real command line, headless in a child process: a purge from the UI removes the whole data root,
    the log files its own logger and std streams were writing included."""
    root = tmp_path / "root"
    env = {**os.environ, "EMAILSUPERSEARCH_DATA_ROOT": str(root)}
    with child_host(["-m", "emailsupersearch", "serve", "--headless", "--no-browser"], root, env) as (child, info):
        base = f"http://127.0.0.1:{info.port}"
        assert (root / "logs" / "host-stderr.log").is_file() and (root / "logs" / "host.log").is_file()
        opener, csrf = _session_opener(base, read_token(root))
        with _post_json(opener, f"{base}/api/projects", csrf, {"name": "Mail", "folders": []}) as r:
            assert r.status == 201
        with _post_json(opener, f"{base}/api/purge", csrf) as r:
            assert r.status == 202
        assert child.wait(timeout=30) == 0
    assert not root.exists()


# ---- root resolution, supervisors, purge ----


def test_resolve_root_prefers_env_then_valid_setting(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("EMAILSUPERSEARCH_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    default = tmp_path / "local" / "eMailSuperSearch"
    assert resolve_root() == default
    assert not default.exists()  # reading the setting creates nothing

    def store_setting(value: str) -> None:
        catalog = Catalog(default)
        try:
            catalog.set_setting("data_root", value)
        finally:
            catalog.close()

    store_setting(str(tmp_path / "chosen"))
    assert resolve_root() == tmp_path / "chosen"
    for refused in ("relative\\root", r"C:\Users\x\OneDrive\ess"):
        store_setting(refused)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="emailsupersearch.host.launcher"):
            assert resolve_root() == default
        assert [r.levelno for r in caplog.records] == [logging.WARNING], refused
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(tmp_path / "env"))
    assert resolve_root() == tmp_path / "env"


def test_start_supervisor_closes_store_on_failure(tmp_path, monkeypatch, chunker_ws):
    catalog = Catalog(tmp_path)
    try:
        project = catalog.create_project("Blocked", [])
        given = []

        def failing_build(project_id, store, *args, **kwargs):
            given.append(store)
            raise RuntimeError("cannot wire")

        monkeypatch.setattr("emailsupersearch.jobs.supervisor.build_project_supervisor", failing_build)
        start = launcher.make_start_supervisor(catalog, None, chunker_ws, tmp_path / "cache" / "parsed", EventBus())
        with pytest.raises(RuntimeError):
            start(project.id)
        assert len(given) == 1 and given[0].closed
        assert catalog.delete_project(project.id)  # nothing holds the project's files open
    finally:
        catalog.close()


def test_supervisor_that_fails_to_start_blocks_its_project(tmp_path):
    catalog = Catalog(tmp_path)
    try:
        project = catalog.create_project("Unstartable", [])
        stopped = []

        class Unstartable:
            def start(self):
                raise RuntimeError("no thread")

            def stop(self):
                stopped.append(True)

        services = SimpleNamespace(
            catalog=catalog, start_supervisor=lambda pid: Unstartable(), supervisors={}, blocked_projects={}
        )
        launcher._start_supervisors(services)
        assert services.supervisors == {} and services.blocked_projects == {project.id: "RuntimeError"}
        assert stopped == [True]  # its store is closed again
    finally:
        catalog.close()


def test_purge_requires_yes(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(root))
    _owned_root(root, projects=2)
    assert cli.main(["purge"]) == 2
    assert "refusing to purge without --yes" in capsys.readouterr().err
    assert (root / "catalog.sqlite").is_file()
    assert cli.main(["purge", "--yes"]) == 0
    assert not root.exists()


def test_purge_refuses_root_without_marker(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(root))
    _owned_root(root)
    (root / MARKER_FILE).unlink()
    before = _tree(root)
    assert cli.main(["purge", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "marker" in err and str(tmp_path) not in err
    assert _tree(root) == before


def test_purge_keeps_files_it_did_not_create(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(root))
    _owned_root(root)
    (root / "notes.txt").write_text("the owner's own file")
    (root / "models" / "mine.onnx").write_text("a model the owner placed")
    (root / "logs" / "other.log").write_text("another program's log")
    assert cli.main(["purge", "--yes"]) == 1
    assert "did not create" in capsys.readouterr().err
    assert _tree(root) == ["logs", str(Path("logs/other.log")), "models", str(Path("models/mine.onnx")), "notes.txt"]


def test_purge_keeps_user_folders_with_allowlisted_names(tmp_path):
    root = tmp_path / "root"
    _owned_root(root)  # its listed project folder goes; the unlisted one is the owner's
    (root / "projects" / "myrepo").mkdir()
    (root / "projects" / "myrepo" / "code.py").write_text("print('mine')")
    (root / "cache" / "thumbnails").mkdir()
    assert remove_root(root) is not None
    assert _tree(root) == [
        "cache",
        str(Path("cache/thumbnails")),
        "projects",
        str(Path("projects/myrepo")),
        str(Path("projects/myrepo/code.py")),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions")
def test_purge_never_follows_junctions(tmp_path):
    root = tmp_path / "root"
    (project_id,) = _owned_root(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the program's")

    def junction(link: Path) -> None:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)

    junction(root / "projects" / project_id / "linked")  # inside a project the catalog lists
    (root / "cache" / "parsed").rmdir()
    junction(root / "cache" / "parsed")  # an allow-listed entry that is itself a link
    junction(root / "logs" / "host.log.1")
    assert remove_root(root) is None
    assert not root.exists() and (outside / "keep.txt").read_text() == "not the program's"


def test_stop_and_purge_import_no_native_libraries(tmp_path):
    script = (
        "import sys\n"
        "from emailsupersearch.cli import main\n"
        "main(['stop'])\n"
        "main(['purge', '--yes'])\n"
        "print(sorted(m for m in ('numpy', 'onnxruntime', 'tokenizers', 'sqlite_vec') if m in sys.modules))\n"
    )
    env = {**os.environ, "EMAILSUPERSEARCH_DATA_ROOT": str(tmp_path / "root")}
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, check=False, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == "[]"
