"""The reconcile scan (spec 9.1): keep the ``files`` manifest current and decide what the pipeline must parse.

``reconcile`` walks every watched folder with ``os.scandir`` and compares each mail file's ``(size, mtime_ns)`` with
its manifest row. It hashes only new and changed files, recognises a renamed file by its file identity (no
re-parse), stamps every file it saw, and tombstones the rows it did not see. A tombstone is purged only once the
next scan confirms the file is still gone. A root that cannot be listed is left alone: nothing under it is
tombstoned or purged. So is a subdirectory that cannot be listed, and so is a file that cannot be read: an error
below a root never costs rows. The scan owns the manifest; parsing is the ingest pipeline's job.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from emailsupersearch.ingest.parse_cache import sha256_file
from emailsupersearch.store import FileRecord, ProjectStore

MAIL_SUFFIXES = {".msg", ".eml"}

_FLUSH_EVERY = 500  # manifest rows buffered between writes


@dataclass
class ScanResult:
    """What one scan found. ``to_parse`` is in walk order; ``renamed`` holds ``(old_path, new_path)`` pairs."""

    scan_id: int
    to_parse: list[Path] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    unchanged: int = 0
    tombstoned: list[str] = field(default_factory=list)
    purged: list[FileRecord] = field(default_factory=list)
    aborted_roots: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def file_identity(path: Path | str) -> str:
    """The volume and file index of ``path``, which survive a rename within a volume."""
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


def _usable_identity(file_id: str) -> bool:
    """False when the volume reported file index 0 (FAT and exFAT do), which every file would then share."""
    return not file_id.endswith(":0")


def _describe(error: OSError) -> str:
    return f"{type(error).__name__}: {error}"


def _list_dir(directory: str) -> list[os.DirEntry[str]]:
    """The entries of ``directory`` in name order; raises ``OSError`` when it cannot be listed."""
    with os.scandir(directory) as entries:
        return sorted(entries, key=lambda entry: entry.name)


class _Reconciler:
    """One scan's state: the result being built, the manifest rows waiting to be written and the paths seen."""

    def __init__(self, store: ProjectStore, scan_id: int) -> None:
        self.store = store
        self.scan_id = scan_id
        self.result = ScanResult(scan_id)
        self._queued: list[FileRecord] = []
        self._seen: set[str] = set()
        self._unreadable: list[str] = []  # subtrees (or entries) that raised OSError; their rows are left as they are

    def run(self, roots: list[Path], now: int) -> ScanResult:
        live_roots: list[str] = []
        for root in (os.path.normpath(str(r)) for r in roots):
            try:
                entries = _list_dir(root)
            except OSError:
                self.result.aborted_roots.append(root)
                continue
            live_roots.append(root)
            for entry in self._walk(entries):
                self._visit(entry)
        self._flush()
        self._keep_unreadable_rows()
        self.store.stamp_seen(list(self._seen), self.scan_id)
        self._remove_missing(live_roots, now)
        return self.result

    # ---- walking ----

    def _walk(self, entries: Iterable[os.DirEntry[str]]) -> Iterator[os.DirEntry[str]]:
        """The mail files among ``entries`` and below them. An entry or folder that raises ``OSError`` is recorded and skipped."""
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_mail = (
                    not is_dir and entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in MAIL_SUFFIXES
                )
                children = _list_dir(entry.path) if is_dir else []
            except OSError as e:
                self._record_error(entry.path, e)
                self._unreadable.append(entry.path)
                continue
            if is_dir:
                yield from self._walk(children)
            elif is_mail:
                yield entry

    def _record_error(self, path: str, error: OSError) -> None:
        self.result.errors.append((path, _describe(error)))

    def _keep_unreadable_rows(self) -> None:
        """Mark every live row below an unreadable subtree as seen, so it is neither tombstoned nor purged this scan."""
        for path in self._unreadable:
            self._seen.update(rec.path for rec in self.store.unseen_under([path], self.scan_id))

    # ---- per file ----

    def _visit(self, entry: os.DirEntry[str]) -> None:
        path = entry.path
        if path in self._seen:  # a root nested inside another reaches the same file twice
            return
        try:
            st = entry.stat(follow_symlinks=False)
            row = self.store.get_file(path)
            if row is None:
                self._new_file(path, st.st_size, st.st_mtime_ns)
            elif (row.size, row.mtime_ns) != (st.st_size, st.st_mtime_ns):
                self._changed_file(row, st.st_size, st.st_mtime_ns)
            else:
                self._settle(row)
        except OSError as e:
            self._record_error(path, e)
            if self.store.get_file(path) is None:
                return
        self._seen.add(path)  # a file that could not be read still exists, so its row stays as it was

    def _new_file(self, path: str, size: int, mtime_ns: int) -> None:
        sha = sha256_file(Path(path))
        identity = file_identity(path)
        file_id = identity if _usable_identity(identity) else None
        old = self.store.find_by_file_id(file_id) if file_id is not None else None
        if old is not None and self._moved_away(old):
            self._rename(old, path, size, mtime_ns, sha, file_id)
            return
        self._queue(FileRecord(path, size, mtime_ns, sha, file_id, None, "pending", "", self.scan_id, None))
        self.result.to_parse.append(Path(path))

    def _moved_away(self, old: FileRecord) -> bool:
        """Whether ``old`` is a row whose file is gone from its path: a rename source, not a second name for a live file."""
        missing = old.tombstoned_at is not None or old.last_seen_scan_id < self.scan_id
        return missing and not os.path.exists(old.path)

    def _rename(self, old: FileRecord, path: str, size: int, mtime_ns: int, sha: str, file_id: str | None) -> None:
        self.store.move_file(old.path, path)
        self.result.renamed.append((old.path, path))
        moved = replace(
            old, path=path, size=size, mtime_ns=mtime_ns, sha256=sha, file_id=file_id,
            last_seen_scan_id=self.scan_id, tombstoned_at=None,
        )
        if sha != old.sha256:
            self._reparse(moved)
        else:
            self._queue(moved)
            self._settle(moved)

    def _changed_file(self, row: FileRecord, size: int, mtime_ns: int) -> None:
        """``row``'s size or mtime differs from the file on disk: re-parse only when the bytes differ."""
        sha = sha256_file(Path(row.path))
        updated = replace(row, size=size, mtime_ns=mtime_ns, sha256=sha)
        if sha != row.sha256:
            self._reparse(updated)
        else:
            self._queue(updated)
            self._settle(updated)

    def _reparse(self, rec: FileRecord) -> None:
        self._queue(replace(rec, parse_status="pending"))
        self.result.to_parse.append(Path(rec.path))

    def _settle(self, rec: FileRecord) -> None:
        """``rec``'s file is unchanged: it still needs parsing only when the pipeline has not finished with it."""
        if rec.parse_status == "pending":
            self.result.to_parse.append(Path(rec.path))
        else:
            self.result.unchanged += 1

    # ---- manifest writes ----

    def _queue(self, rec: FileRecord) -> None:
        self._queued.append(rec)
        if len(self._queued) >= _FLUSH_EVERY:
            self._flush()

    def _flush(self) -> None:
        if self._queued:
            self.store.upsert_files(self._queued)
            self._queued = []

    def _remove_missing(self, live_roots: list[str], now: int) -> None:
        """Purge tombstones the earlier scans left, then tombstone this scan's missing files.

        Purging first keeps a file from being tombstoned and purged in the same scan when earlier scans skipped
        the root (an aborted root stamps nothing, so the file's last-seen id can already be two scans old).
        Tombstones below an unreadable subtree are not purged either: this scan could not confirm they are gone.
        """
        if not live_roots:
            return
        self.result.purged = self.store.purge_tombstoned_under(live_roots, self.scan_id, skip=self._unreadable)
        missing = sorted(rec.path for rec in self.store.unseen_under(live_roots, self.scan_id))
        self.store.tombstone(missing, now)
        self.result.tombstoned = missing


def reconcile(store: ProjectStore, roots: list[Path], scan_id: int, now: int) -> ScanResult:
    """Scan ``roots`` against the manifest as scan ``scan_id`` and report what the pipeline must do (spec 9.1)."""
    return _Reconciler(store, scan_id).run(roots, now)
