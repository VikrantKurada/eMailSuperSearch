"""The native folder picker: tkinter's directory dialog, shown by a child Python process.

The host serves requests on worker threads with no Tk main loop, so the dialog runs in a fresh interpreter
(``sys.executable -c``) that keeps it above every other window and prints the chosen folder. On Windows the child
gets no console window. Any failure (tkinter missing, the 10-minute timeout, a crash) is logged by exception type
only and answered like a cancel.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

PICK_TIMEOUT_S = 600

# Reads {"initial": <folder> | null} on stdin; prints the chosen folder in UTF-8, or nothing on cancel.
_SCRIPT = """\
import json
import sys
import tkinter
from tkinter import filedialog

args = json.loads(sys.stdin.read() or "{}")
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
chosen = filedialog.askdirectory(initialdir=args.get("initial") or None)
root.destroy()
if chosen:
    sys.stdout.reconfigure(encoding="utf-8")
    print(chosen)
"""


def pick_folder(initial: Path | None = None) -> Path | None:
    """Show the folder dialog, opened at ``initial`` when given, and wait up to 10 minutes for a choice.

    Returns the chosen folder, or None when the dialog was cancelled or could not be shown.
    """
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT],
            input=json.dumps({"initial": str(initial) if initial else None}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=PICK_TIMEOUT_S,
            creationflags=flags,
            check=True,
        )
    except Exception as e:  # noqa: BLE001 - whatever went wrong, the user just sees no folder chosen
        log.warning("the folder picker failed: %s", type(e).__name__)
        return None
    lines = result.stdout.splitlines()
    line = lines[-1].strip() if lines else ""
    return Path(line) if line else None
