"""Data-root location, validation and on-disk layout (spec 6.2)."""

import ntpath
import os
import re
from pathlib import Path, PureWindowsPath

from emailsupersearch import __version__

MARKER_FILE = ".emailsupersearch"  # in a data root: this program made the folder, so a purge may empty it
_KNOWN_FOLDERS = ("Documents", "Desktop")
_KNOWN_FOLDERS_LOWER = {folder.lower() for folder in _KNOWN_FOLDERS}
_DRIVE_RE = re.compile(r"[A-Za-z]:")


def data_root() -> Path:
    """Return the data root: EMAILSUPERSEARCH_DATA_ROOT, else the eMailSuperSearch folder in LOCALAPPDATA."""
    override = os.environ.get("EMAILSUPERSEARCH_DATA_ROOT")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "eMailSuperSearch"


def validate_data_root(p: Path) -> str | None:
    """Return the reason a data root is refused, or None when it is acceptable."""
    normalised = PureWindowsPath(ntpath.normpath(str(p)))
    parts = normalised.parts
    if any(part.lower().startswith("onedrive") for part in parts):
        return "the data root must not be inside OneDrive: a synced folder corrupts a live SQLite database"
    if (
        _DRIVE_RE.fullmatch(normalised.drive)
        and len(parts) > 3
        and parts[1].lower() == "users"
        and parts[3].lower() in _KNOWN_FOLDERS_LOWER
    ):
        return "the data root must not be inside a user's Documents or Desktop folder, which Windows may sync or protect"
    profile = os.environ.get("USERPROFILE")
    if profile:
        lowered = [part.lower() for part in parts]
        for folder in _KNOWN_FOLDERS:
            known = [part.lower() for part in PureWindowsPath(ntpath.normpath(profile), folder).parts]
            if lowered[: len(known)] == known:
                return f"the data root must not be inside your {folder} folder, which Windows may sync or protect"
    return None


def project_dir(root: Path, project_id: str) -> Path:
    return root / "projects" / project_id


def ensure_layout(root: Path) -> None:
    """Create the standard sub-folders under the data root."""
    for sub in ("projects", "cache/parsed", "models", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def mark_root(root: Path) -> None:
    """Write the ownership marker (it holds the version that wrote it) unless the root already has one."""
    marker = root / MARKER_FILE
    if not marker.is_file():
        marker.write_text(__version__, encoding="utf-8")


def is_marked(root: Path) -> bool:
    """Whether ``root`` carries the ownership marker, so it is a data root this program made."""
    return (root / MARKER_FILE).is_file()
