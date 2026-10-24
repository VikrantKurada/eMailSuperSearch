"""Folder watching: tell the supervisor soon after something changes below a watched folder (spec 9.1).

The reconcile timer alone would notice a new export only every fifteen minutes. A ``FolderWatcher`` runs
``watchfiles`` on one daemon thread and reports each debounced batch of changes as the set of directories they
happened in; the supervisor answers with a rescan. Local folders use the operating system's change notifications.
Network shares often deliver none, so the supervisor watches them with polling (``force_polling``).
The watcher never touches the store: it only calls ``on_change``.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import watchfiles

log = logging.getLogger(__name__)

_DRIVE_REMOTE = 4  # GetDriveTypeW: a network drive
_STOP_TIMEOUT_S = 5.0
_RUST_TIMEOUT_MS = 1000


def is_network_path(p: Path) -> bool:
    """Whether ``p`` is on a network share: a UNC path, or a drive letter Windows maps to a remote drive."""
    if str(p).startswith("\\\\") or p.drive.startswith("\\\\"):
        return True
    if sys.platform != "win32" or not p.drive:
        return False
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(f"{p.drive}\\") == _DRIVE_REMOTE
    except Exception as e:  # noqa: BLE001 - an unanswerable drive is treated as local
        log.debug("GetDriveTypeW failed: %s", type(e).__name__)
        return False


class FolderWatcher:
    """Watches ``roots`` on a daemon thread and calls ``on_change`` with the set of directories that changed."""

    def __init__(
        self,
        roots: list[Path],
        on_change: Callable[[set[Path]], None],
        debounce_s: float = 2.0,
        force_polling: bool = False,
    ) -> None:
        self.roots = [Path(r) for r in roots]
        self.on_change = on_change
        self.debounce_s = debounce_s
        self.force_polling = force_polling
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start watching the roots that exist now; a missing root is skipped (logged by count only)."""
        if self._thread is not None:
            return
        existing = [root for root in self.roots if root.exists()]
        missing = len(self.roots) - len(existing)
        if missing:
            log.warning("%d watched folders do not exist and are not watched", missing)
        if not existing:
            return
        self._thread = threading.Thread(target=self._run, args=(existing,), name="ess-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and wait up to five seconds for the thread to end. Safe to call twice, or before ``start``."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=_STOP_TIMEOUT_S)
            if thread.is_alive():
                log.warning("the folder watcher did not stop within %d s", _STOP_TIMEOUT_S)

    def _run(self, roots: list[Path]) -> None:
        try:
            for changes in watchfiles.watch(
                *roots,
                debounce=int(self.debounce_s * 1000),
                force_polling=self.force_polling,
                stop_event=self._stop,
                rust_timeout=_RUST_TIMEOUT_MS,
            ):
                self.on_change({Path(path).parent for _, path in changes})
        except Exception as e:  # the watcher only speeds things up: the timer still reconciles
            log.warning("the folder watcher stopped: %s", type(e).__name__)
            log.debug("the folder watcher stopped", exc_info=True)
