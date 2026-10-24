"""The host's run files in the data root: the single-instance lock, where the running host listens, its launch token.

- ``host.lock`` is created exclusively and holds the host's pid and process creation time (``"<pid>\\n<created>"``).
  A lock whose process is gone, or unreadable, was left by a crash: it is stale and replaced. The host removes it
  on exit.
- ``host.json`` holds ``{"pid", "port", "created"}`` while the host serves, so a second launch opens the browser on
  the running host and ``stop`` knows where to send the shutdown request.
- ``host.token`` holds the launch token. Its permissions are cut down to the owner (``icacls`` drops the inherited
  entries and grants the user full control) before the token is written; the token itself is never logged.

A recorded process counts only when a process with that pid is running *and* was created at the recorded time, so a
pid Windows has since given to another program (after a crash and a reboot, say) is not taken for the host. The
creation time is a Windows ``FILETIME``; it is 0 where unknown (other systems, or a file without one), and then
liveness alone is checked.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
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
    """The held single-instance lock: ``path`` is ``root/host.lock``, which records ``pid`` and ``created``."""

    path: Path
    pid: int
    created: int = 0

    def release(self) -> None:
        """Remove the lock file; a missing one is fine."""
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


@dataclass
class RunningInfo:
    """A running host: its process id, the loopback port it serves on and its process creation time (0: unknown)."""

    pid: int
    port: int
    created: int = 0


def process_created(pid: int) -> int:
    """The creation time of process ``pid`` as an integer ``FILETIME``; 0 when unknown (not Windows, or no such
    process)."""
    if sys.platform != "win32":
        return 0
    with _process_handle(pid) as handle:
        return 0 if handle is None else _creation_time(handle)


def acquire(root: Path) -> InstanceLock | None:
    """Take the single-instance lock for ``root``, or None when a live process holds it.

    A stale lock (its process is gone, or the file is unreadable) is removed and the exclusive create is tried once
    more.
    """
    path = root / LOCK_FILE
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _holder_alive(path):
                return None
            log.info("replacing a stale instance lock")
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        pid = os.getpid()
        created = process_created(pid)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(f"{pid}\n{created}")
        return InstanceLock(path, pid, created)
    return None


def lock_held(root: Path) -> bool:
    """Whether a live process holds ``root``'s lock: a host that is starting, or serving."""
    return _holder_alive(root / LOCK_FILE)


def read_running(root: Path) -> RunningInfo | None:
    """The host serving ``root`` per ``host.json``; None when the file is missing or malformed or its process is
    gone."""
    try:
        data = json.loads((root / RUNNING_FILE).read_text(encoding="utf-8"))
        info = RunningInfo(int(data["pid"]), int(data["port"]), int(data.get("created", 0)))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return info if _process_matches(info.pid, info.created) else None


def write_running(root: Path, info: RunningInfo) -> None:
    data = {"pid": info.pid, "port": info.port, "created": info.created}
    (root / RUNNING_FILE).write_text(json.dumps(data), encoding="utf-8")


def clear(root: Path) -> None:
    """Remove ``host.json``; a missing one is fine."""
    with contextlib.suppress(FileNotFoundError):
        (root / RUNNING_FILE).unlink()


def host_exited(root: Path, info: RunningInfo) -> bool:
    """Whether the host recorded in ``info`` has finished: its process is gone or, for a host serving from a thread
    of this very process (as in the tests), it has released its lock, the last step of its shutdown."""
    if info.pid == os.getpid():
        return not (root / LOCK_FILE).exists()
    return not _process_matches(info.pid, info.created)


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


def _holder_alive(path: Path) -> bool:
    """Whether the process a lock file records is still running; False for a missing or unreadable file."""
    try:
        fields = path.read_text(encoding="ascii").split()
        pid = int(fields[0])
        created = int(fields[1]) if len(fields) > 1 else 0
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return False
    return created >= 0 and _process_matches(pid, created)


def _process_matches(pid: int, created: int) -> bool:
    """Whether process ``pid`` is running and, when ``created`` is known (not 0), was created at that time."""
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
    import ctypes
    from ctypes import wintypes

    with _process_handle(pid) as handle:
        if handle is None:  # no such process (or not one of ours to query, so not our host either)
            return False
        code = wintypes.DWORD()
        if not _kernel32().GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return False
        return created == 0 or _creation_time(handle) == created


@contextlib.contextmanager
def _process_handle(pid: int) -> Iterator[int | None]:
    """A query handle on process ``pid`` (None when it cannot be opened), closed on exit. Windows only."""
    if not 0 < pid <= _MAX_PID:
        yield None
        return
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        yield handle or None
    finally:
        if handle:
            kernel32.CloseHandle(handle)


def _creation_time(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    ok = _kernel32().GetProcessTimes(
        handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
    )
    return (created.dwHighDateTime << 32) | created.dwLowDateTime if ok else 0


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
    filetime = ctypes.POINTER(wintypes.FILETIME)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, filetime, filetime, filetime, filetime)
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32
