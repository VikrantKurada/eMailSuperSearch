"""The host's run files in the data root: the single-instance lock, where the running host listens, its launch token.

- ``host.lock`` is created exclusively and holds the host's pid. A lock whose pid is not alive, or unreadable, was
  left by a crash: it is stale and replaced. The host removes it on exit.
- ``host.json`` holds ``{"pid", "port"}`` while the host serves, so a second launch opens the browser on the running
  host and ``stop`` knows where to send the shutdown request.
- ``host.token`` holds the launch token. Its permissions are cut down to the owner (``icacls`` drops the inherited
  entries and grants the user full control) before the token is written; the token itself is never logged.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_FILE = "host.lock"
RUNNING_FILE = "host.json"
TOKEN_FILE = "host.token"

_ICACLS_TIMEOUT_S = 30
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_MAX_PID = 0xFFFFFFFF


@dataclass
class InstanceLock:
    """The held single-instance lock: ``path`` is ``root/host.lock``, which holds ``pid``."""

    path: Path
    pid: int

    def release(self) -> None:
        """Remove the lock file; a missing one is fine."""
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


@dataclass
class RunningInfo:
    """A running host: its process id and the loopback port it serves on."""

    pid: int
    port: int


def acquire(root: Path) -> InstanceLock | None:
    """Take the single-instance lock for ``root``, or None when a live process holds it.

    A stale lock (its pid is dead or unreadable) is removed and the exclusive create is tried once more.
    """
    path = root / LOCK_FILE
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pid = _read_pid(path)
            if pid is not None and _pid_alive(pid):
                return None
            log.info("replacing a stale instance lock")
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
        return InstanceLock(path, os.getpid())
    return None


def read_running(root: Path) -> RunningInfo | None:
    """The host serving ``root`` per ``host.json``; None when the file is missing or malformed or its pid is dead."""
    try:
        data = json.loads((root / RUNNING_FILE).read_text(encoding="utf-8"))
        info = RunningInfo(int(data["pid"]), int(data["port"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return info if _pid_alive(info.pid) else None


def write_running(root: Path, info: RunningInfo) -> None:
    (root / RUNNING_FILE).write_text(json.dumps({"pid": info.pid, "port": info.port}), encoding="utf-8")


def clear(root: Path) -> None:
    """Remove ``host.json``; a missing one is fine."""
    with contextlib.suppress(FileNotFoundError):
        (root / RUNNING_FILE).unlink()


def host_exited(root: Path, info: RunningInfo) -> bool:
    """Whether the host recorded in ``info`` has finished: its process is gone or, for a host serving from a thread
    of this very process (as in the tests), it has released its lock, the last step of its shutdown."""
    if info.pid == os.getpid():
        return not (root / LOCK_FILE).exists()
    return not _pid_alive(info.pid)


def write_token(root: Path, token: str) -> None:
    """Write the launch token to ``host.token``, restricted to the owner first (a warning is logged if that fails)."""
    path = root / TOKEN_FILE
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))  # empty while its ACL is cut down
    if sys.platform == "win32":
        _restrict_to_user(path)
    with open(path, "w", encoding="ascii") as f:
        f.write(token)


def read_token(root: Path) -> str | None:
    """The launch token from ``host.token``, or None when there is none."""
    try:
        token = (root / TOKEN_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token or None


def _restrict_to_user(path: Path) -> None:
    user = os.environ.get("USERNAME", "")
    command = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"]
    try:
        done = subprocess.run(
            command, check=False, capture_output=True, timeout=_ICACLS_TIMEOUT_S, creationflags=_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not restrict the token file to its owner: %s", type(e).__name__)
        return
    if done.returncode != 0:
        log.warning("could not restrict the token file to its owner: icacls exited with %d", done.returncode)


def _read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return pid if 0 < pid <= _MAX_PID else None


def _pid_alive(pid: int) -> bool:
    """Whether a process with this id is running."""
    if not 0 < pid <= _MAX_PID:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)  # signal 0 only checks; never call this on Windows, where it terminates the process
        except ProcessLookupError:
            return False
        except PermissionError:  # it exists, as another user's process
            return True
        return True
    return _windows_pid_alive(pid)


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:  # no such process (or not one of ours to query, so not our host either)
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


@functools.cache
def _kernel32():
    """kernel32 with the signatures used here, on its own ``WinDLL`` so other modules' prototypes are untouched."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32
