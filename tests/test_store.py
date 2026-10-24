import os
import sqlite3
import threading

import numpy as np
import pytest

from emailsupersearch.db.schema import SCHEMA_VERSION, SchemaTooNew, schema_version
from emailsupersearch.ingest.chunker import ChunkText
from emailsupersearch.store import FileRecord, ProjectStore
from emailsupersearch.types import AttachmentText, ParsedAttachment, Recipient


def _rec(path: str, **overrides) -> FileRecord:
    fields = {
        "path": path,
        "size": 10,
        "mtime_ns": 5,
        "sha256": None,
        "file_id": None,
        "message_key": None,
        "parse_status": "indexed",
        "status_reason": "",
        "last_seen_scan_id": 0,
        "tombstoned_at": None,
    }
    fields.update(overrides)
    return FileRecord(**fields)


def _chunks(*texts: str) -> list[ChunkText]:
    return [ChunkText(i, t, len(t.split())) for i, t in enumerate(texts)]


def _att(
    ordinal: int, text: str = "attachment text", status: str = "ok", needs_ocr_pages=(), sha256: str | None = None
) -> AttachmentText:
    return AttachmentText(
        ordinal, f"a{ordinal}.pdf", "pdf", 1000 + ordinal, status, text, 3, list(needs_ocr_pages), sha256=sha256
    )


def _parsed_att() -> ParsedAttachment:
    return ParsedAttachment(0, "a0.pdf", "application/pdf", 1000, "pdf")


def _vec_blob(value: float) -> bytes:
    return np.full(384, value, dtype=np.float32).tobytes()


def _vec_ids(store: ProjectStore) -> set[int]:
    return {r[0] for r in store.conn.execute("SELECT chunk_rowid FROM chunks_vec")}


def _count(store: ProjectStore, sql: str, *params) -> int:
    return store.conn.execute(sql, params).fetchone()[0]


def test_open_creates_and_migrates(tmp_path):
    project_dir = tmp_path / "deep" / "proj"
    store = ProjectStore.open(project_dir)
    try:
        assert (project_dir / "project.sqlite").is_file()
        assert schema_version(store.conn) == SCHEMA_VERSION
        assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        store.close()
    reopened = ProjectStore.open(project_dir)  # opening an existing project migrates nothing and loses nothing
    try:
        assert schema_version(reopened.conn) == SCHEMA_VERSION
    finally:
        reopened.close()


def test_open_propagates_schema_too_new_and_releases_file(tmp_path):
    store = ProjectStore.open(tmp_path / "proj")
    with store.conn:
        store.conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))
    store.close()
    with pytest.raises(SchemaTooNew):
        ProjectStore.open(tmp_path / "proj")
    (tmp_path / "proj" / "project.sqlite").unlink()  # the failed open closed its connection


def test_settings_roundtrip_json(tmp_store):
    assert tmp_store.get_setting("missing") is None
    assert tmp_store.get_setting("missing", 42) == 42
    value = {"a": [1, 2.5, None, True], "b": {"nested": "ünï"}}
    tmp_store.set_setting("cfg", value)
    assert tmp_store.get_setting("cfg", "default") == value
    tmp_store.set_setting("cfg", 7)
    assert tmp_store.get_setting("cfg") == 7
    assert tmp_store.conn.execute("SELECT value FROM settings WHERE key = 'cfg'").fetchone()[0] == "7"


def test_folders_crud(tmp_path):
    store = ProjectStore.open(tmp_path / "proj")
    try:
        assert store.list_folders() == []
        store.add_folder("D:\\mail\\b", "local")
        store.add_folder("\\\\srv\\share\\a", "network")
        assert store.list_folders() == sorted(["D:\\mail\\b", "\\\\srv\\share\\a"])
        row = store.conn.execute("SELECT * FROM folders WHERE path = ?", ("D:\\mail\\b",)).fetchone()
        assert (row["kind"], row["last_scan_id"], row["reachable"]) == ("local", 0, 1)
        with store.conn:
            store.conn.execute("UPDATE folders SET last_scan_id = 9, reachable = 0")
        store.add_folder("D:\\mail\\b", "network")  # INSERT OR REPLACE: kind changes, scan state resets
        row = store.conn.execute("SELECT * FROM folders WHERE path = ?", ("D:\\mail\\b",)).fetchone()
        assert (row["kind"], row["last_scan_id"], row["reachable"]) == ("network", 0, 1)
        assert len(store.list_folders()) == 2
        store.remove_folder("D:\\mail\\b")
        assert store.list_folders() == ["\\\\srv\\share\\a"]
        store.remove_folder("not there")  # no error
        store.add_folder("D:/x/../y", "local")  # roots are stored normalised, as file paths are joined from them
        assert store.list_folders() == sorted(["\\\\srv\\share\\a", os.path.normpath("D:/x/../y")])
        store.remove_folder("D:/x/../y")  # the same spelling finds the stored row
        assert store.list_folders() == ["\\\\srv\\share\\a"]
    finally:
        store.close()


def test_file_manifest_scan_lifecycle(tmp_store, corpus_dir):
    root = str(corpus_dir)
    inside = os.path.join(root, "a.eml")
    nested = os.path.join(root, "sub", "b.eml")
    outside = os.path.join(str(corpus_dir.parent), "elsewhere", "c.eml")
    sibling = root + "-sibling" + os.sep + "d.eml"  # starts with the root's spelling but is not a folder under it
    for path in (inside, nested, outside, sibling):
        tmp_store.upsert_file(_rec(path, message_key="m1" if path == inside else None))
    assert tmp_store.get_file(inside).message_key == "m1"
    assert tmp_store.get_file(os.path.join(root, "nope.eml")) is None

    tmp_store.stamp_seen([inside, nested, outside, sibling], 1)
    assert tmp_store.unseen_under([root], 1) == []

    tmp_store.stamp_seen([nested, outside, sibling], 2)  # `inside` is not seen in scan 2
    unseen = tmp_store.unseen_under([root], 2)
    assert [r.path for r in unseen] == [inside]
    assert [r.path for r in tmp_store.unseen_under([root + os.sep, os.path.join(root, "sub", "..")], 2)] == [inside]
    assert tmp_store.unseen_under([], 2) == []

    tmp_store.tombstone([inside], 100)
    tmp_store.tombstone([inside], 200)  # the first tombstone time stays
    assert tmp_store.get_file(inside).tombstoned_at == 100
    assert tmp_store.unseen_under([root], 2) == []  # tombstoned rows are no longer "unseen"
    assert tmp_store.paths_for_message("m1") == []
    assert tmp_store.paths_for_message("m1", live_only=False) == [inside]
    assert tmp_store.purge_tombstoned_under([root], 2) == []  # last seen in scan 1; scan 2 is too early to purge

    purged = tmp_store.purge_tombstoned_under([root], 3)
    assert [r.path for r in purged] == [inside] and purged[0].tombstoned_at == 100 and purged[0].message_key == "m1"
    assert tmp_store.get_file(inside) is None
    assert tmp_store.get_file(nested) is not None and tmp_store.get_file(sibling) is not None
    assert tmp_store.purge_tombstoned_under([root], 3) == []


def test_stamp_seen_revives_a_tombstoned_path(tmp_store, corpus_dir):
    path = os.path.join(str(corpus_dir), "a.eml")
    tmp_store.upsert_file(_rec(path, message_key="m1", file_id="vol:1"))
    tmp_store.tombstone([path], 50)
    assert tmp_store.find_by_file_id("vol:1").tombstoned_at == 50
    tmp_store.stamp_seen([path], 4)
    rec = tmp_store.get_file(path)
    assert (rec.tombstoned_at, rec.last_seen_scan_id) == (None, 4)
    assert tmp_store.paths_for_message("m1") == [path]
    assert tmp_store.find_by_file_id("vol:none") is None


def test_upsert_file_overwrites_every_column(tmp_store, corpus_dir):
    path = os.path.join(str(corpus_dir), "a.eml")
    tmp_store.upsert_file(_rec(path, sha256="aa", file_id="f1", message_key="m1", status_reason="x", tombstoned_at=7))
    new = _rec(
        path, size=99, mtime_ns=123, sha256=None, file_id="f2", message_key="m2",
        parse_status="failed", status_reason="boom", last_seen_scan_id=3, tombstoned_at=None,
    )
    tmp_store.upsert_file(new)
    assert tmp_store.get_file(path) == new
    assert _count(tmp_store, "SELECT COUNT(*) FROM files") == 1


def test_manifest_batches_large_path_lists(tmp_store, corpus_dir):
    paths = [os.path.join(str(corpus_dir), f"m{i}.eml") for i in range(1300)]  # more than 500 per statement
    for p in paths:
        tmp_store.upsert_file(_rec(p))
    tmp_store.stamp_seen(paths, 5)
    assert tmp_store.unseen_under([str(corpus_dir)], 5) == []
    assert len(tmp_store.unseen_under([str(corpus_dir)], 6)) == 1300
    tmp_store.tombstone(paths, 9)
    assert tmp_store.counts()["tombstoned"] == 1300
    rowids = tmp_store.replace_chunks("m", "body", _chunks(*[f"w{i}" for i in range(1300)]))
    tmp_store.mark_embedded(rowids)
    assert _count(tmp_store, "SELECT COUNT(*) FROM chunks WHERE embedded = 1") == 1300


def test_close_releases_file(tmp_path):
    project_dir = tmp_path / "proj"
    store = ProjectStore.open(project_dir)
    store.set_setting("k", 1)
    store.read_conn().execute("SELECT 1").fetchall()
    worker = threading.Thread(target=lambda: store.read_conn().execute("SELECT 1").fetchall())
    worker.start()
    worker.join()
    store.close()
    (project_dir / "project.sqlite").unlink()
    assert not (project_dir / "project.sqlite").exists()
    store.close()  # closing twice is harmless
    with pytest.raises(RuntimeError, match="closed"):
        store.read_conn()


def test_read_conn_is_read_only_and_sees_commits(tmp_store):
    tmp_store.set_setting("k", {"v": 1})
    seen: dict[str, object] = {}

    def reader():
        conn = tmp_store.read_conn()
        seen["conn"] = conn
        seen["again"] = tmp_store.read_conn()
        seen["value"] = conn.execute("SELECT value FROM settings WHERE key = 'k'").fetchone()[0]

    thread = threading.Thread(target=reader)
    thread.start()
    thread.join()
    assert seen["value"] == '{"v": 1}'
    assert seen["conn"] is seen["again"]  # one connection per thread
    assert seen["conn"] is not tmp_store.read_conn() and tmp_store.read_conn() is not tmp_store.conn
    with pytest.raises(sqlite3.OperationalError):
        tmp_store.read_conn().execute("INSERT INTO settings(key, value) VALUES ('x', '1')")
    tmp_store.set_setting("k", 2)  # a write committed after the read connection exists is visible to it
    assert tmp_store.read_conn().execute("SELECT value FROM settings WHERE key = 'k'").fetchone()[0] == "2"


def test_read_conn_reaps_connections_of_exited_threads(tmp_store):
    opened: list[sqlite3.Connection] = []

    def reader():
        opened.append(tmp_store.read_conn())

    for _ in range(6):  # a worker pool retires idle threads, so bursts of searches arrive on fresh threads
        thread = threading.Thread(target=reader)
        thread.start()
        thread.join()
    assert len(opened) == 6 and len(tmp_store._readers) <= 2
    for dead in opened[:5]:  # reaped by the next thread's first read_conn()
        with pytest.raises(sqlite3.ProgrammingError):
            dead.execute("SELECT 1")
    tmp_store.set_setting("k", 1)
    assert tmp_store.read_conn().execute("SELECT value FROM settings WHERE key = 'k'").fetchone()[0] == "1"
    assert len(tmp_store._readers) <= 2


def test_read_conn_does_not_commit_the_writers_transaction(tmp_store):
    with pytest.raises(RuntimeError, match="boom"), tmp_store._lock, tmp_store.conn:
        tmp_store.conn.execute("INSERT INTO settings(key, value) VALUES ('half', '1')")
        with tmp_store.read_conn() as reader:  # a search's `with conn:` must not commit the writer's open transaction
            assert reader.execute("SELECT 1 FROM settings WHERE key = 'half'").fetchone() is None
        assert tmp_store.conn.in_transaction
        raise RuntimeError("boom")
    assert tmp_store.get_setting("half") is None


def test_upsert_message_replaces_recipients(tmp_store, parsed_factory):
    first = parsed_factory(
        message_id="<m1@example.com>",
        subject="RE:  Re: Quarterly   Budget",
        recipients=[Recipient("to", "Bob", "bob@example.org"), Recipient("cc", "Cy", "cy@example.org")],
        attachments=[_parsed_att()],
    )
    tmp_store.upsert_message(first, "m1", "new text one", "chain-1", 2)
    row = tmp_store.message("m1")
    assert row["message_id"] == "<m1@example.com>" and row["subject"] == "RE:  Re: Quarterly   Budget"
    assert row["normalized_subject"] == "quarterly budget"
    assert (row["sender_name"], row["sender_addr"]) == ("Jane Doe", "jane@example.com")
    assert (row["sent_ts"], row["received_ts"], row["date_source"]) == (1773705600, 1773705660, "header_date")
    assert (row["body_text"], row["body_html"], row["new_text"]) == ("hello", None, "new text one")
    assert (row["chain_id"], row["chain_depth"], row["has_attachments"], row["flags"]) == ("chain-1", 2, 1, "{}")
    recipients = tmp_store.conn.execute("SELECT * FROM recipients WHERE message_key = 'm1' ORDER BY addr").fetchall()
    assert [(r["kind"], r["name"], r["addr"]) for r in recipients] == [
        ("to", "Bob", "bob@example.org"),
        ("cc", "Cy", "cy@example.org"),
    ]

    second = parsed_factory(
        message_id="<m1@example.com>",
        subject="Budget v2",
        body_text="changed",
        body_html="<p>changed</p>",
        recipients=[Recipient("bcc", "Di", "di@example.org")],
    )
    tmp_store.upsert_message(parsed_factory(message_id="<m2@example.com>"), "m2", "other", None, 0)
    tmp_store.upsert_message(second, "m1", "new text two", None, 0)
    row = tmp_store.message("m1")
    assert (row["subject"], row["normalized_subject"], row["body_text"], row["body_html"]) == (
        "Budget v2", "budget v2", "changed", "<p>changed</p>",
    )
    assert (row["new_text"], row["chain_id"], row["chain_depth"], row["has_attachments"]) == (
        "new text two", None, 0, 0,
    )
    recipients = tmp_store.conn.execute("SELECT * FROM recipients WHERE message_key = 'm1'").fetchall()
    assert [(r["kind"], r["addr"]) for r in recipients] == [("bcc", "di@example.org")]
    assert _count(tmp_store, "SELECT COUNT(*) FROM recipients WHERE message_key = 'm2'") == 1
    assert _count(tmp_store, "SELECT COUNT(*) FROM messages") == 2
    assert tmp_store.message("absent") is None


def test_attachments_upsert_and_status(tmp_store):
    atts = [_att(0, sha256="abc123"), _att(1, text="", status="needs_ocr", needs_ocr_pages=[1, 2])]
    tmp_store.upsert_attachments("m1", atts, "pending")
    tmp_store.upsert_attachments("other", [_att(0)], "pending")
    sql = "SELECT * FROM attachments WHERE message_key = 'm1' ORDER BY ordinal"
    rows = tmp_store.conn.execute(sql).fetchall()
    assert [(r["ordinal"], r["filename"], r["mime"], r["size"], r["kind"], r["sha256"]) for r in rows] == [
        (0, "a0.pdf", None, 1000, "pdf", "abc123"),
        (1, "a1.pdf", None, 1001, "pdf", None),
    ]
    assert [(r["extract_status"], r["ocr_status"], r["embedded_partial"]) for r in rows] == [
        ("ok", "none", 0),
        ("needs_ocr", "pending", 0),
    ]
    assert [(r["page_count"], r["text_chars"]) for r in rows] == [(3, len("attachment text")), (3, 0)]

    tmp_store.set_attachment_status("m1", 1, ocr_status="done", embedded_partial=True)
    tmp_store.set_attachment_status("m1", 0, extract_status="partial")
    rows = tmp_store.conn.execute(sql).fetchall()
    assert [(r["extract_status"], r["ocr_status"], r["embedded_partial"]) for r in rows] == [
        ("partial", "none", 0),
        ("needs_ocr", "done", 1),
    ]
    tmp_store.set_attachment_status("m1", 1, embedded_partial=False)
    assert _count(tmp_store, "SELECT embedded_partial FROM attachments WHERE message_key = 'm1' AND ordinal = 1") == 0

    tmp_store.upsert_attachments("m1", [_att(5)], "pending")  # replaces the key's rows, leaves other keys alone
    rows = tmp_store.conn.execute("SELECT message_key, ordinal FROM attachments ORDER BY message_key").fetchall()
    assert [tuple(r) for r in rows] == [("m1", 5), ("other", 0)]


def test_replace_chunks_returns_rowids_and_deletes_old(tmp_store, parsed_factory):
    tmp_store.upsert_message(parsed_factory(message_id="<m1@example.com>"), "m1", "t", None, 0)
    body = tmp_store.replace_chunks("m1", "body", _chunks("alpha one", "beta two"))
    att0 = tmp_store.replace_chunks("m1", "att0", _chunks("gamma three"))
    other = tmp_store.replace_chunks("m2", "body", _chunks("alpha elsewhere"))
    assert len(body) == 2 and len(set(body + att0 + other)) == 4
    assert tmp_store.chunk_rowids("m1") == att0 + body  # ordered by part ("att0" < "body"), then idx
    sql = "SELECT id, idx, text, token_count, embedded FROM chunks WHERE message_key = 'm1' AND part = 'body'"
    rows = tmp_store.conn.execute(sql).fetchall()
    assert [tuple(r) for r in rows] == [(body[0], 0, "alpha one", 2, 0), (body[1], 1, "beta two", 2, 0)]

    tmp_store.mark_embedded([body[0], att0[0]])
    tmp_store.mark_embedded([])
    embedded = {r["id"]: r["embedded"] for r in tmp_store.conn.execute("SELECT id, embedded FROM chunks")}
    assert embedded == {body[0]: 1, body[1]: 0, att0[0]: 1, other[0]: 0}
    with tmp_store.conn:
        for rowid, value in ((body[0], 0.1), (att0[0], 0.2)):
            tmp_store.conn.execute("INSERT INTO chunks_vec(chunk_rowid, embedding) VALUES (?, ?)", (rowid, _vec_blob(value)))

    new = tmp_store.replace_chunks("m1", "body", _chunks("delta four"))
    assert len(new) == 1 and not set(new) & set(body)
    assert tmp_store.chunk_rowids("m1") == att0 + new
    assert _count(tmp_store, "SELECT embedded FROM chunks WHERE id = ?", new[0]) == 0
    assert _vec_ids(tmp_store) == {att0[0]}  # the replaced chunk's vector went with it; other parts keep theirs
    assert not tmp_store.conn.execute("SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH 'beta'").fetchall()
    assert [r[0] for r in tmp_store.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'delta'")] == new
    trigram = tmp_store.conn.execute("SELECT rowid FROM chunks_trigram WHERE chunks_trigram MATCH 'elt'")
    assert [r[0] for r in trigram] == new
    assert tmp_store.chunk_rowids("m2") == other  # another message's chunks are untouched
    assert tmp_store.replace_chunks("m1", "body", []) == []
    assert tmp_store.chunk_rowids("m1") == att0


def test_delete_message_removes_chunks_and_vec_rows(tmp_store, parsed_factory, corpus_dir):
    path = os.path.join(str(corpus_dir), "a.eml")
    tmp_store.upsert_file(_rec(path, message_key="m1"))
    tmp_store.upsert_message(parsed_factory(message_id="<m1@example.com>", attachments=[_parsed_att()]), "m1", "t", None, 0)
    tmp_store.upsert_message(parsed_factory(message_id="<m2@example.com>"), "m2", "t", None, 0)
    tmp_store.upsert_attachments("m1", [_att(0)], "pending")
    tmp_store.upsert_attachments("m2", [_att(0)], "pending")
    body = tmp_store.replace_chunks("m1", "body", _chunks("invoice INV-2024-00017 approved", "second chunk"))
    att = tmp_store.replace_chunks("m1", "att0", _chunks("attachment words"))
    keep = tmp_store.replace_chunks("m2", "body", _chunks("invoice kept elsewhere"))
    with tmp_store.conn:
        for rowid in (body[0], att[0], keep[0]):  # body[1] has no vector: not every chunk is embedded
            tmp_store.conn.execute("INSERT INTO chunks_vec(chunk_rowid, embedding) VALUES (?, ?)", (rowid, _vec_blob(0.5)))
    assert _vec_ids(tmp_store) == {body[0], att[0], keep[0]}

    tmp_store.delete_message("m1")

    assert tmp_store.message("m1") is None
    assert tmp_store.chunk_rowids("m1") == []
    assert _vec_ids(tmp_store) == {keep[0]}
    assert _count(tmp_store, "SELECT COUNT(*) FROM recipients WHERE message_key = 'm1'") == 0
    assert _count(tmp_store, "SELECT COUNT(*) FROM attachments WHERE message_key = 'm1'") == 0
    assert _count(tmp_store, "SELECT COUNT(*) FROM chunks WHERE message_key = 'm1'") == 0
    assert [r[0] for r in tmp_store.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'invoice'")] == keep
    assert not tmp_store.conn.execute("SELECT 1 FROM chunks_trigram WHERE chunks_trigram MATCH '\"INV-2024\"'").fetchall()
    assert tmp_store.get_file(path) is not None  # the manifest belongs to the pipeline
    assert tmp_store.message("m2") is not None and tmp_store.chunk_rowids("m2") == keep
    assert _count(tmp_store, "SELECT COUNT(*) FROM recipients WHERE message_key = 'm2'") == 1
    assert _count(tmp_store, "SELECT COUNT(*) FROM attachments WHERE message_key = 'm2'") == 1
    tmp_store.delete_message("never existed")  # no error


def test_counts_metadata_only(tmp_store, parsed_factory, corpus_dir):
    root = str(corpus_dir)
    assert tmp_store.counts() == {"messages": 0, "chunks": 0, "files": 0, "tombstoned": 0, "metadata_only": 0}
    for key in ("ok", "empty", "partial", "failed"):
        tmp_store.upsert_message(parsed_factory(message_id=f"<{key}@example.com>"), key, "t", None, 0)
    tmp_store.replace_chunks("ok", "body", _chunks("one", "two"))
    tmp_store.replace_chunks("partial", "body", _chunks("three"))
    tmp_store.replace_chunks("failed", "body", _chunks("four"))
    # "empty" has no chunks; "partial" and "failed" have chunks but a file row that says the parse was not clean
    tmp_store.upsert_file(_rec(os.path.join(root, "ok.eml"), message_key="ok"))
    tmp_store.upsert_file(_rec(os.path.join(root, "empty.eml"), message_key="empty"))
    tmp_store.upsert_file(_rec(os.path.join(root, "partial1.eml"), message_key="partial", parse_status="indexed_partial"))
    tmp_store.upsert_file(_rec(os.path.join(root, "partial2.eml"), message_key="partial", parse_status="indexed_partial"))
    tmp_store.upsert_file(_rec(os.path.join(root, "failed.eml"), message_key="failed", parse_status="failed"))
    tmp_store.upsert_file(_rec(os.path.join(root, "orphan.eml"), message_key=None, parse_status="failed"))
    tmp_store.tombstone([os.path.join(root, "ok.eml"), os.path.join(root, "orphan.eml")], 10)
    assert tmp_store.counts() == {"messages": 4, "chunks": 4, "files": 4, "tombstoned": 2, "metadata_only": 3}
