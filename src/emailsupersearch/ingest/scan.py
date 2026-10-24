"""The reconcile scan (spec 9.1): keep the ``files`` manifest current and decide what the pipeline must parse.

``reconcile`` walks every watched folder with ``os.scandir`` and compares each mail file's ``(size, mtime_ns)`` with
its manifest row. It hashes only new and changed files, recognises a renamed file by its file identity (no
re-parse), stamps every file it saw, and tombstones the rows it did not see. A tombstone is purged only once the
next scan confirms the file is still gone. A root that cannot be listed is left alone: nothing under it is
tombstoned or purged. So is a subdirectory that cannot be listed, and so is a file that cannot be read: an error
below a root never costs rows. Symlinks and NTFS junctions are not followed (a junction can duplicate a folder or
loop back on itself). The scan owns the manifest's size, mtime, hash, file id and last-seen columns; it never writes
back the columns the ingest pipeline owns. Parsing is the pipeline's job.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from emailsupersearch.ingest.parse_cache import sha256_file
from emailsupersearch.store import FileRecord, ProjectStore, ScanUpdate

MAIL_SUFFIXES = {".msg", ".eml"}

_FLUSH_EVERY = 500  # manifest writes buffered between transactions


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
    """The volume and file index of ``path``, which normally survive a rename within a volume.

    It is a hint, not a proof. FAT and exFAT volumes report index 0, which ``_usable_identity`` filters out, but on
    Windows they can also report non-zero, position-derived indexes that change on rename and get reused. The
    ``:0`` filter is therefore a cheap guard, not a guarantee; a wrong match only costs a re-parse, because the
    scan compares file hashes before it treats a match as an unchanged rename.
    """
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


def _usable_identity(file_id: str) -> bool:
    """False when the volume reported file index 0, which every file would then share (a cheap filter only)."""
    return not file_id.endswith(":0")


def _file_id(path: str) -> str | None:
    """The identity to store for ``path``: ``None`` when the volume gives none that can tell files apart."""
    identity = file_identity(path)
    return identity if _usable_identity(identity) else None


def _is_gone(path: str) -> bool:
    """Whether ``path`` is known not to exist. A path that cannot be checked (permission, I/O error) is not gone."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _describe(error: OSError) -> str:
    return f"{type(error).__name__}: {error}"


def _list_dir(directory: str) -> list[os.DirEntry[str]]:
    """The entries of ``directory`` in name order; raises ``OSError`` when it cannot be listed."""
    with os.scandir(directory) as entries:
        return sorted(entries, key=lambda entry: entry.name)


class _Reconciler:
    """One scan's state: the result being built, the manifest writes waiting to be flushed and the paths seen."""

    def __init__(self, store: ProjectStore, scan_id: int) -> None:
        self.store = store
        self.scan_id = scan_id
        self.result = ScanResult(scan_id)
        self._inserts: list[FileRecord] = []  # rows for files the manifest has not seen before
        self._updates: list[ScanUpdate] = []  # scan-owned columns of rows that already exist
        self._seen: set[str] = set()
        self._unreadable: list[str] = []  # subtrees (or entries) that raised OSError; their rows are left as they are

    def run(self, roots: list[Path], now: int) -> ScanResult:
        live_roots: list[str] = []
        for root in (os.path.normpath(str(r)) for r in roots):
            try:
                entries = _list_dir(root)
            except NotADirectoryError:
                self._abort(root, "NotADirectoryError: not a directory")
                continue
            except OSError as e:
                self._abort(root, _describe(e))
                continue
            live_roots.append(root)
            for entry in self._walk(entries):
                self._visit(entry)
        self._flush()
        self._keep_unreadable_rows()
        self.store.stamp_seen(list(self._seen), self.scan_id)
        self._remove_missing(live_roots, now)
        return self.result

    def _abort(self, root: str, reason: str) -> None:
        self.result.aborted_roots.append(root)
        self.result.errors.append((root, reason))

    # ---- walking ----

    def _walk(self, entries: Iterable[os.DirEntry[str]]) -> Iterator[os.DirEntry[str]]:
        """The mail files among ``entries`` and below them. An entry or folder that raises ``OSError`` is recorded and skipped."""
        for entry in entries:
            try:
                if entry.is_junction():  # is_dir() is true for a junction, but following it duplicates or loops
                    continue
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
                self._decide(path, parse=row.parse_status == "pending")
        except OSError as e:
            self._record_error(path, e)
            if self.store.get_file(path) is None:
                return
        self._seen.add(path)  # a file that could not be read still exists, so its row stays as it was

    def _new_file(self, path: str, size: int, mtime_ns: int) -> None:
        sha = sha256_file(Path(path))
        file_id = _file_id(path)
        old = self.store.find_by_file_id(file_id) if file_id is not None else None
        if old is not None and self._is_rename_source(old, path):
            self._rename(old, path, size, mtime_ns, sha, file_id)
            return
        self._insert(FileRecord(path, size, mtime_ns, sha, file_id, None, "pending", "", self.scan_id, None))
        self.result.to_parse.append(Path(path))

    def _is_rename_source(self, old: FileRecord, path: str) -> bool:
        """Whether ``old``, the row with the same file identity as the new file at ``path``, is the same file renamed.

        That is so when only the case of the name changed (the old spelling still resolves on a case-insensitive
        volume), or when the row is missing this scan and its path is gone. A row whose path still exists is a second
        name for a live file (a hard link), not a rename source.
        """
        if os.path.normcase(old.path) == os.path.normcase(path):
            return True
        missing = old.tombstoned_at is not None or old.last_seen_scan_id < self.scan_id
        return missing and _is_gone(old.path)

    def _rename(self, old: FileRecord, path: str, size: int, mtime_ns: int, sha: str, file_id: str | None) -> None:
        self.store.move_file(old.path, path)
        self.result.renamed.append((old.path, path))
        self._record_change(old, path, size, mtime_ns, sha, file_id)

    def _changed_file(self, row: FileRecord, size: int, mtime_ns: int) -> None:
        """``row``'s size or mtime differs from the file on disk: re-parse only when the bytes differ."""
        sha = sha256_file(Path(row.path))
        self._record_change(row, row.path, size, mtime_ns, sha, _file_id(row.path))

    def _record_change(self, row: FileRecord, path: str, size: int, mtime_ns: int, sha: str, file_id: str | None) -> None:
        """Queue the scan-owned columns of ``row`` (now at ``path``) and decide whether the file needs parsing."""
        reparse = sha != row.sha256
        self._update(ScanUpdate(path, size, mtime_ns, sha, file_id, self.scan_id, reparse))
        self._decide(path, parse=reparse or row.parse_status == "pending")

    def _decide(self, path: str, parse: bool) -> None:
        """The file is known: it goes to the pipeline when ``parse``, otherwise it counts as unchanged."""
        if parse:
            self.result.to_parse.append(Path(path))
        else:
            self.result.unchanged += 1

    # ---- manifest writes ----

    def _insert(self, rec: FileRecord) -> None:
        self._inserts.append(rec)
        self._flush_if_full()

    def _update(self, update: ScanUpdate) -> None:
        self._updates.append(update)
        self._flush_if_full()

    def _flush_if_full(self) -> None:
        if len(self._inserts) + len(self._updates) >= _FLUSH_EVERY:
            self._flush()

    def _flush(self) -> None:
        if self._inserts:
            self.store.upsert_files(self._inserts)
            self._inserts = []
        if self._updates:
            self.store.update_file_scan_fields(self._updates)
            self._updates = []

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
