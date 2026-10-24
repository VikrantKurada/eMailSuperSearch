import os
import shutil
from dataclasses import replace

import pytest

from emailsupersearch.ingest import scan
from emailsupersearch.ingest.parse_cache import sha256_file
from emailsupersearch.ingest.scan import (
    MAIL_SUFFIXES,
    ScanResult,
    file_identity,
    reconcile,
)


def _emls(corpus_dir):
    return sorted(corpus_dir.glob("*.eml"))


def _set_status(store, status):
    """Stand in for the ingest pipeline: give every manifest row ``status``."""
    with store.lock, store.conn:
        store.conn.execute("UPDATE files SET parse_status = ?", (status,))


def _skip_without_file_index(path):
    if os.stat(path).st_ino == 0:
        pytest.skip("this volume reports no file index, so renames cannot be detected")


def test_first_scan_lists_all_mail_files(tmp_store, corpus_dir):
    r = reconcile(tmp_store, [corpus_dir], 1, now=1)
    assert isinstance(r, ScanResult) and r.scan_id == 1
    assert len(r.to_parse) == len(_emls(corpus_dir))
    assert r.unchanged == 0 and r.tombstoned == [] and r.purged == [] and r.aborted_roots == [] and r.errors == []
    assert tmp_store.counts()["files"] == len(r.to_parse)
    first = _emls(corpus_dir)[0]
    row = tmp_store.get_file(str(first))
    assert row.parse_status == "pending" and row.message_key is None and row.last_seen_scan_id == 1
    assert row.sha256 == sha256_file(first) and row.file_id == file_identity(first)


def test_unchanged_second_scan_parses_nothing(tmp_store, corpus_dir):
    reconcile(tmp_store, [corpus_dir], 1, 1)
    _set_status(tmp_store, "indexed")
    r2 = reconcile(tmp_store, [corpus_dir], 2, 2)
    assert r2.to_parse == [] and r2.unchanged == len(_emls(corpus_dir))
    assert r2.tombstoned == [] and r2.renamed == []
    assert {r[0] for r in tmp_store.conn.execute("SELECT last_seen_scan_id FROM files")} == {2}


def test_modified_file_is_reparsed_only_if_bytes_changed(tmp_store, corpus_dir):
    target = _emls(corpus_dir)[0]
    reconcile(tmp_store, [corpus_dir], 1, 1)
    _set_status(tmp_store, "indexed")

    later = target.stat().st_mtime_ns + 5_000_000_000
    os.utime(target, ns=(later, later))
    touched = reconcile(tmp_store, [corpus_dir], 2, 2)
    assert target not in touched.to_parse and touched.to_parse == []
    assert touched.unchanged == len(_emls(corpus_dir))
    row = tmp_store.get_file(str(target))
    assert row.mtime_ns == target.stat().st_mtime_ns and row.parse_status == "indexed"

    with target.open("ab") as f:
        f.write(b"\n")
    edited = reconcile(tmp_store, [corpus_dir], 3, 3)
    assert edited.to_parse == [target]
    row = tmp_store.get_file(str(target))
    assert row.parse_status == "pending" and row.size == target.stat().st_size and row.sha256 == sha256_file(target)


def test_rename_is_detected_without_reparse(tmp_store, corpus_dir):
    a = _emls(corpus_dir)[0]
    b = corpus_dir / "renamed.eml"
    _skip_without_file_index(a)
    reconcile(tmp_store, [corpus_dir], 1, 1)
    _set_status(tmp_store, "indexed")
    tmp_store.upsert_file(replace(tmp_store.get_file(str(a)), message_key="k1"))

    os.rename(a, b)
    r = reconcile(tmp_store, [corpus_dir], 2, 2)
    assert r.renamed == [(str(a), str(b))]
    assert b not in r.to_parse and r.to_parse == []
    assert r.tombstoned == [] and r.purged == [] and r.unchanged == len(_emls(corpus_dir))
    assert tmp_store.get_file(str(a)) is None
    moved = tmp_store.get_file(str(b))
    assert (moved.message_key, moved.parse_status, moved.tombstoned_at, moved.last_seen_scan_id) == (
        "k1", "indexed", None, 2,
    )
    assert moved.file_id == file_identity(b) and moved.sha256 == sha256_file(b)
    assert tmp_store.counts()["tombstoned"] == 0


def test_renamed_and_edited_file_is_reparsed(tmp_store, corpus_dir):
    a = _emls(corpus_dir)[0]
    b = corpus_dir / "renamed.eml"
    _skip_without_file_index(a)
    reconcile(tmp_store, [corpus_dir], 1, 1)
    _set_status(tmp_store, "indexed")

    os.rename(a, b)
    with b.open("ab") as f:
        f.write(b"\n")
    r = reconcile(tmp_store, [corpus_dir], 2, 2)
    assert r.renamed == [(str(a), str(b))] and r.to_parse == [b]
    assert tmp_store.get_file(str(a)) is None
    moved = tmp_store.get_file(str(b))
    assert moved.parse_status == "pending" and moved.sha256 == sha256_file(b) and moved.size == b.stat().st_size


def test_file_moved_out_and_back_under_a_new_name_is_a_rename(tmp_store, corpus_dir, tmp_path):
    a = _emls(corpus_dir)[0]
    b = corpus_dir / "returned.eml"
    parked = tmp_path / "parked.eml"
    _skip_without_file_index(a)
    reconcile(tmp_store, [corpus_dir], 1, 1)
    _set_status(tmp_store, "indexed")

    os.rename(a, parked)
    assert reconcile(tmp_store, [corpus_dir], 2, 2).tombstoned == [str(a)]
    os.rename(parked, b)
    r = reconcile(tmp_store, [corpus_dir], 3, 3)
    assert r.renamed == [(str(a), str(b))] and r.to_parse == [] and r.tombstoned == [] and r.purged == []
    assert tmp_store.counts()["tombstoned"] == 0 and tmp_store.get_file(str(a)) is None
    assert tmp_store.get_file(str(b)).parse_status == "indexed"


def test_hard_link_is_a_new_file_not_a_rename(tmp_store, corpus_dir):
    a = _emls(corpus_dir)[0]
    b = corpus_dir / "linked.eml"
    _skip_without_file_index(a)
    reconcile(tmp_store, [corpus_dir], 1, 1)
    _set_status(tmp_store, "indexed")

    try:
        os.link(a, b)  # the same file under a second name while the first name still exists
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not available here")
    r = reconcile(tmp_store, [corpus_dir], 2, 2)
    assert r.renamed == [] and r.to_parse == [b] and r.tombstoned == []
    assert tmp_store.get_file(str(a)) is not None and tmp_store.get_file(str(b)) is not None


def test_deleted_file_tombstoned_then_purged_on_next_scan(tmp_store, corpus_dir):
    victim = _emls(corpus_dir)[0]
    reconcile(tmp_store, [corpus_dir], 1, 1)
    victim.unlink()
    r2 = reconcile(tmp_store, [corpus_dir], 2, 2)
    assert r2.tombstoned == [str(victim)] and r2.purged == []
    assert tmp_store.get_file(str(victim)).tombstoned_at == 2
    r3 = reconcile(tmp_store, [corpus_dir], 3, 3)
    assert r3.tombstoned == [] and [p.path for p in r3.purged] == [str(victim)] and r3.purged[0].tombstoned_at == 2
    assert tmp_store.get_file(str(victim)) is None
    assert tmp_store.counts()["tombstoned"] == 0 and tmp_store.counts()["files"] == len(_emls(corpus_dir))


def test_reappearing_file_is_revived_not_reparsed(tmp_store, corpus_dir, tmp_path):
    target = _emls(corpus_dir)[0]
    parked = tmp_path / "parked.eml"
    reconcile(tmp_store, [corpus_dir], 1, 1)
    _set_status(tmp_store, "indexed")

    os.rename(target, parked)
    assert reconcile(tmp_store, [corpus_dir], 2, 2).tombstoned == [str(target)]
    os.rename(parked, target)
    r3 = reconcile(tmp_store, [corpus_dir], 3, 3)
    assert r3.to_parse == [] and r3.tombstoned == [] and r3.purged == []
    row = tmp_store.get_file(str(target))
    assert (row.tombstoned_at, row.last_seen_scan_id, row.parse_status) == (None, 3, "indexed")


def test_a_deletion_found_after_an_aborted_scan_is_not_purged_in_the_same_scan(tmp_store, corpus_dir):
    victim = _emls(corpus_dir)[0]
    reconcile(tmp_store, [corpus_dir], 1, 1)
    reconcile(tmp_store, [corpus_dir / "gone"], 2, 2)  # the root was unreachable, so scan 2 stamped nothing
    victim.unlink()
    r3 = reconcile(tmp_store, [corpus_dir], 3, 3)
    assert r3.tombstoned == [str(victim)] and r3.purged == []
    r4 = reconcile(tmp_store, [corpus_dir], 4, 4)
    assert [p.path for p in r4.purged] == [str(victim)]


def test_missing_root_aborts_without_tombstones(tmp_store, corpus_dir):
    reconcile(tmp_store, [corpus_dir], 1, 1)
    r = reconcile(tmp_store, [corpus_dir / "gone"], 2, 2)
    assert r.aborted_roots and r.tombstoned == [] and tmp_store.counts()["tombstoned"] == 0
    assert r.aborted_roots == [str(corpus_dir / "gone")] and r.to_parse == [] and r.purged == []


def test_root_that_is_a_file_aborts(tmp_store, corpus_dir):
    reconcile(tmp_store, [corpus_dir], 1, 1)
    not_a_folder = _emls(corpus_dir)[0]
    r = reconcile(tmp_store, [not_a_folder, corpus_dir], 2, 2)
    assert r.aborted_roots == [str(not_a_folder)] and r.tombstoned == []
    assert tmp_store.counts()["tombstoned"] == 0 and r.unchanged + len(r.to_parse) == len(_emls(corpus_dir))


def test_one_aborted_root_leaves_the_other_roots_deletions_in_force(tmp_store, corpus_dir, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    shutil.copy(_emls(corpus_dir)[0], second / "kept.eml")
    shutil.copy(_emls(corpus_dir)[1], second / "gone.eml")
    reconcile(tmp_store, [corpus_dir, second], 1, 1)
    (second / "gone.eml").unlink()
    r = reconcile(tmp_store, [corpus_dir / "missing", second], 2, 2)
    assert r.aborted_roots == [str(corpus_dir / "missing")] and r.tombstoned == [str(second / "gone.eml")]
    assert tmp_store.get_file(str(_emls(corpus_dir)[0])).tombstoned_at is None


def test_non_mail_files_ignored(tmp_store, corpus_dir):
    (corpus_dir / "notes.txt").write_text("x")
    to_parse = reconcile(tmp_store, [corpus_dir], 1, 1).to_parse
    assert all(p.suffix in MAIL_SUFFIXES for p in to_parse)
    assert corpus_dir / "notes.txt" not in to_parse and tmp_store.get_file(str(corpus_dir / "notes.txt")) is None


def test_subdirectories_are_walked_and_the_suffix_match_ignores_case(tmp_store, corpus_dir):
    nested = corpus_dir / "2023" / "inbox"
    nested.mkdir(parents=True)
    shutil.copy(_emls(corpus_dir)[0], nested / "UPPER.EML")
    shutil.copy(_emls(corpus_dir)[1], nested / "old.MSG")
    (nested / "readme.eml.txt").write_text("not mail")
    to_parse = reconcile(tmp_store, [corpus_dir], 1, 1).to_parse
    assert nested / "UPPER.EML" in to_parse and nested / "old.MSG" in to_parse
    assert nested / "readme.eml.txt" not in to_parse and len(to_parse) == len(_emls(corpus_dir)) + 2


def test_nested_roots_visit_a_file_once(tmp_store, corpus_dir):
    nested = corpus_dir / "sub"
    nested.mkdir()
    shutil.copy(_emls(corpus_dir)[0], nested / "x.eml")
    r = reconcile(tmp_store, [corpus_dir, nested], 1, 1)
    assert r.to_parse.count(nested / "x.eml") == 1 and len(r.to_parse) == len(_emls(corpus_dir)) + 1


def test_pending_rows_are_reparsed_even_if_unchanged(tmp_store, corpus_dir):
    reconcile(tmp_store, [corpus_dir], 1, 1)
    tmp_store.conn.execute("UPDATE files SET parse_status='pending'")
    tmp_store.conn.commit()
    r = reconcile(tmp_store, [corpus_dir], 2, 2)
    assert len(r.to_parse) == len(_emls(corpus_dir)) and r.unchanged == 0


def test_pending_row_is_parsed_even_when_only_its_mtime_changed(tmp_store, corpus_dir):
    target = _emls(corpus_dir)[0]
    reconcile(tmp_store, [corpus_dir], 1, 1)
    _set_status(tmp_store, "indexed")
    tmp_store.upsert_file(replace(tmp_store.get_file(str(target)), parse_status="pending"))
    later = target.stat().st_mtime_ns + 5_000_000_000
    os.utime(target, ns=(later, later))
    assert reconcile(tmp_store, [corpus_dir], 2, 2).to_parse == [target]


def test_aborted_root_keeps_old_tombstones(tmp_store, corpus_dir):
    reconcile(tmp_store, [corpus_dir], 1, 1)
    victim = _emls(corpus_dir)[0]
    victim.unlink()
    reconcile(tmp_store, [corpus_dir], 2, 2)
    r = reconcile(tmp_store, [corpus_dir / "gone"], 3, 3)
    assert r.purged == [] and tmp_store.counts()["tombstoned"] == 1


def test_errors_in_subdirectory_do_not_abort_root(tmp_store, corpus_dir, monkeypatch):
    locked = corpus_dir / "locked"
    open_dir = corpus_dir / "open"
    locked.mkdir()
    open_dir.mkdir()
    shutil.copy(_emls(corpus_dir)[0], locked / "hidden.eml")
    shutil.copy(_emls(corpus_dir)[0], open_dir / "visible.eml")
    real_scandir = os.scandir

    def scandir(path="."):
        if os.fspath(path) == str(locked):
            raise PermissionError(13, "Access is denied", str(locked))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    r = reconcile(tmp_store, [corpus_dir], 1, 1)
    assert r.aborted_roots == []
    assert [path for path, _ in r.errors] == [str(locked)] and r.errors[0][1].startswith("PermissionError: ")
    assert open_dir / "visible.eml" in r.to_parse and locked / "hidden.eml" not in r.to_parse
    assert len(r.to_parse) == len(_emls(corpus_dir)) + 1


def test_file_that_cannot_be_read_is_an_error_not_an_abort(tmp_store, corpus_dir, monkeypatch):
    broken = _emls(corpus_dir)[0]
    real_sha = scan.sha256_file

    def sha256_file_or_fail(path):
        if str(path) == str(broken):
            raise PermissionError(13, "The process cannot access the file", str(broken))
        return real_sha(path)

    monkeypatch.setattr(scan, "sha256_file", sha256_file_or_fail)
    r = reconcile(tmp_store, [corpus_dir], 1, 1)
    assert [path for path, _ in r.errors] == [str(broken)] and r.aborted_roots == []
    assert broken not in r.to_parse and len(r.to_parse) == len(_emls(corpus_dir)) - 1
    assert tmp_store.get_file(str(broken)) is None


def test_a_large_scan_is_written_in_batches(tmp_store, corpus_dir):
    many = corpus_dir / "many"
    many.mkdir()
    for i in range(1200):  # more than the 500 records buffered between writes
        (many / f"m{i:04d}.eml").write_bytes(b"Subject: s\n\n" + str(i).encode())
    total = len(_emls(corpus_dir)) + 1200
    assert len(reconcile(tmp_store, [corpus_dir], 1, 1).to_parse) == total
    assert tmp_store.counts()["files"] == total
    _set_status(tmp_store, "indexed")
    r2 = reconcile(tmp_store, [corpus_dir], 2, 2)
    assert r2.to_parse == [] and r2.unchanged == total


def test_file_identity_is_device_and_index(corpus_dir):
    target = _emls(corpus_dir)[0]
    st = os.stat(target)
    assert file_identity(target) == f"{st.st_dev}:{st.st_ino}"
    assert file_identity(str(target)) == file_identity(target)
