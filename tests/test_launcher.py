"""The launcher: the single-instance lock, the port choice, and serve and stop against a real uvicorn server.

Tests that serve run ``serve`` on a daemon thread of this process and always stop it. Its stream redirection is
recorded instead of done there (it would ``dup2`` over pytest's own fds 1 and 2); the real one runs in a child
process, as does the headless purge.
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

import pytest
from helpers import wait_until

from emailsupersearch import cli, logging_setup
from emailsupersearch.catalog import Catalog
from emailsupersearch.host import launcher
from emailsupersearch.host.launcher import (
    DEFAULT_PORT,
    choose_port,
    resolve_root,
    serve,
    stop,
)
from emailsupersearch.host.lock import (
    RunningInfo,
    acquire,
    read_running,
    read_token,
    write_running,
    write_token,
)
from emailsupersearch.host.sse import EventBus

_START_S = 60  # serve's start-up runs the doctor and loads the embedder
_EXIT_S = 10  # serve must have returned this long after stop was called
# Loopback requests must never go through a proxy configured for the machine.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture(autouse=True)
def _detach_host_log():
    """Close the log handler serve attaches, so later tests do not write to this test's ``host.log``."""
    yield
    logger = logging.getLogger(logging_setup.LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)
    logging_setup._HANDLERS.clear()


@pytest.fixture
def redirects(monkeypatch):
    """The roots serve asked to redirect the std streams for; the redirection itself is not done in this process."""
    calls: list[Path] = []
    monkeypatch.setattr(launcher, "redirect_std_streams_to_log", calls.append)
    return calls


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
    assert (tmp_path / "host.lock").read_text() == str(os.getpid())
    assert acquire(tmp_path) is None  # held by a live process: this one
    lock.release()
    assert not (tmp_path / "host.lock").exists()
    lock.release()  # a second release is harmless

    for stale in (str(_dead_pid()), "not a pid", ""):
        (tmp_path / "host.lock").write_text(stale)
        replaced = acquire(tmp_path)
        assert replaced is not None and (tmp_path / "host.lock").read_text() == str(os.getpid()), stale
        replaced.release()

    write_running(tmp_path, RunningInfo(os.getpid(), 4321))
    assert read_running(tmp_path) == RunningInfo(os.getpid(), 4321)
    write_running(tmp_path, RunningInfo(_dead_pid(), 4321))
    assert read_running(tmp_path) is None
    (tmp_path / "host.json").write_text("{not json")
    assert read_running(tmp_path) is None


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


def test_serve_refuses_onedrive_root(capsys):
    assert serve(Path(r"C:\Users\x\OneDrive\ess"), None, True, False) == 2
    assert "OneDrive" in capsys.readouterr().err
    assert not Path(r"C:\Users\x\OneDrive\ess").exists()


def test_serve_then_stop_roundtrip(tmp_path, redirects):
    root = tmp_path / "root"
    with running_host(root) as host:
        assert redirects == [root]  # headless: the std streams go to the log
        assert (root / "host.lock").read_text() == str(os.getpid())
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


def test_headless_purge_removes_the_data_root(tmp_path):
    """The real command line, headless in a child process: a purge from the UI removes the whole data root,
    the log files its own logger and std streams were writing included."""
    root = tmp_path / "root"
    env = {**os.environ, "EMAILSUPERSEARCH_DATA_ROOT": str(root)}
    child = subprocess.Popen(
        [sys.executable, "-m", "emailsupersearch", "serve", "--headless", "--no-browser"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    host_pid = None
    try:
        info = wait_until(lambda: read_running(root) or child.poll() is not None, _START_S)
        assert child.poll() is None, "the host exited before it was serving"
        host_pid = info.pid  # not child.pid: the venv's python.exe runs the real interpreter as its child
        base = f"http://127.0.0.1:{info.port}"
        wait_until(lambda: _health(info.port), 10)
        assert (root / "logs" / "host-stderr.log").is_file() and (root / "logs" / "host.log").is_file()
        opener, csrf = _session_opener(base, read_token(root))
        with _post_json(opener, f"{base}/api/purge", csrf) as r:
            assert r.status == 202
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            if host_pid is not None:
                with contextlib.suppress(OSError):
                    os.kill(host_pid, signal.SIGTERM)  # TerminateProcess on Windows
            child.kill()
            child.wait(timeout=30)
    assert not root.exists()


# ---- root resolution, supervisors, purge command ----


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


def test_purge_requires_yes(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(root))
    Catalog(root).close()
    assert cli.main(["purge"]) == 2
    assert "refusing to purge without --yes" in capsys.readouterr().err
    assert (root / "catalog.sqlite").is_file()
    assert cli.main(["purge", "--yes"]) == 0
    assert not root.exists()


def test_purge_keeps_files_it_did_not_create(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(root))
    Catalog(root).close()
    (root / "notes.txt").write_text("the owner's own file")
    assert cli.main(["purge", "--yes"]) == 1
    assert "did not create" in capsys.readouterr().err
    assert sorted(p.name for p in root.iterdir()) == ["notes.txt"]
