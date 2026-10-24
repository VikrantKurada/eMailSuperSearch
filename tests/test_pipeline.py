import logging
import sqlite3
from email.message import EmailMessage

import numpy as np
import pytest
from helpers import LONG_TEXT_30000_WORDS, att_docx, write_minimal_pdf

from emailsupersearch import logging_setup
from emailsupersearch import store as store_module
from emailsupersearch.diagnostics.corpus import TOPICS, make_eml_bytes
from emailsupersearch.index.fts import keyword_search
from emailsupersearch.index.vectors import VectorIndex
from emailsupersearch.ingest import pipeline as pipeline_module
from emailsupersearch.ingest.parse import ParseError, parse_file
from emailsupersearch.ingest.parse_cache import ParseCache, sha256_file
from emailsupersearch.ingest.pipeline import (
    CHUNK_TEXT_MAX_CHARS,
    IngestOutcome,
    IngestPipeline,
)
from emailsupersearch.ingest.scan import reconcile
from emailsupersearch.ingest.workers import ParseMemoryExceeded, ParseTimeout
from emailsupersearch.logging_setup import configure_logging

_MIME = {
    ".docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".pdf": ("application", "pdf"),
}


def _scan(pipeline, folder, scan_id=1):
    """Reconcile ``folder`` so its files have manifest rows, as the supervisor does before it ingests anything."""
    return reconcile(pipeline.store, [folder], scan_id, scan_id)


def _first_with(corpus_dir, kind):
    """The first corpus file whose message carries an attachment of ``kind``."""
    for path in sorted(corpus_dir.glob("*.eml")):
        if any(a.kind == kind for a in parse_file(path).attachments):
            return path
    raise AssertionError(f"the corpus has no message with a {kind} attachment")


def _eml(folder, name, body, attachment=None, message_id="<built@example.com>"):
    """Write one message to ``folder / name``; ``attachment`` is ``(filename, bytes)`` of a .docx or .pdf."""
    msg = EmailMessage()
    msg["Subject"] = "Quarterly supplier report"
    msg["From"] = "Alice Morgan <alice.morgan@example.com>"
    msg["To"] = "Ben Carter <ben.carter@example.com>"
    msg["Date"] = "Mon, 05 Jan 2026 09:00:00 +0000"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    if attachment is not None:
        filename, data = attachment
        maintype, subtype = _MIME[filename[filename.rindex("."):]]
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(msg.as_bytes())
    return path


def _parts(store, key):
    return [r["part"] for r in store.conn.execute("SELECT part FROM chunks WHERE message_key = ? ORDER BY part, idx", (key,))]


def _vec_ids(store):
    return {r[0] for r in store.conn.execute("SELECT chunk_rowid FROM chunks_vec")}


def _raise(error):
    def parse(path):
        raise error

    return parse


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


def _snapshot(store, key):
    """Everything an indexed message is stored as: its new text, chunks, vectors and attachment rows."""
    chunks = [tuple(r) for r in store.conn.execute(
        "SELECT id, part, idx, text, embedded FROM chunks WHERE message_key=? ORDER BY id", (key,)
    )]
    atts = [tuple(r) for r in store.conn.execute("SELECT * FROM attachments WHERE message_key=? ORDER BY ordinal", (key,))]
    return store.message(key)["new_text"], chunks, sorted(_vec_ids(store)), atts


@pytest.fixture
def _reset_package_logger():
    yield
    logger = logging.getLogger("emailsupersearch")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)
    logging_setup._HANDLERS.clear()


def test_ingest_corpus_indexes_messages_and_chunks(pipeline, corpus_dir):
    outcomes = [pipeline.ingest_file(p, 1) for p in sorted(corpus_dir.glob("*.eml"))]
    assert all(o.status == "indexed" for o in outcomes)
    assert pipeline.store.counts()["chunks"] > len(outcomes)
    assert pipeline.store.counts()["messages"] == len(outcomes)


def test_duplicate_export_indexes_once_and_survives_one_delete(pipeline, corpus_dir):
    src = min(corpus_dir.glob("*.eml"))
    dup = corpus_dir / "dup.eml"
    dup.write_bytes(b"X-Export: 2\r\n" + src.read_bytes())
    _scan(pipeline, corpus_dir)
    k = pipeline.ingest_file(src, 1).message_key
    assert pipeline.ingest_file(dup, 1).status == "skipped_duplicate"
    assert len(pipeline.store.paths_for_message(k)) == 2
    assert pipeline.remove_path(str(dup)) == k
    assert pipeline.store.message(k)
    pipeline.remove_path(str(src))
    assert pipeline.store.message(k) is None
    assert pipeline.store.chunk_rowids(k) == [] and _vec_ids(pipeline.store) == set()
    assert pipeline.store.conn.execute("SELECT COUNT(*) FROM chains").fetchone()[0] == 0
    assert pipeline.store.get_file(str(src)) is None


def test_attachment_text_is_chunked_as_separate_part(pipeline, corpus_dir):
    p = _first_with(corpus_dir, "docx")
    k = pipeline.ingest_file(p, 1).message_key
    assert any(r["part"] == "att:0" for r in pipeline.store.conn.execute("SELECT part FROM chunks WHERE message_key=?", (k,)))
    assert set(_parts(pipeline.store, k)) == {"body", "att:0"}
    att_text = pipeline.store.conn.execute("SELECT text FROM chunks WHERE message_key=? AND part='att:0'", (k,)).fetchone()[0]
    assert att_text.startswith("From ") and "[attachment-" in att_text.splitlines()[0]


def test_attachment_over_cap_is_keyword_indexed_but_partially_embedded(pipeline, tmp_path, embedder):
    path = _eml(tmp_path / "big", "big.eml", "The full report is attached.", ("report.docx", att_docx(LONG_TEXT_30000_WORDS).data))
    store = pipeline.store
    k = pipeline.ingest_file(path, 1).message_key
    rows = store.conn.execute("SELECT embedded FROM chunks WHERE message_key=? AND part='att:0'", (k,)).fetchall()
    assert len(rows) > 64 and sum(r[0] for r in rows) == 64
    assert store.conn.execute("SELECT embedded_partial FROM attachments WHERE message_key=?", (k,)).fetchone()[0] == 1
    body = store.conn.execute("SELECT embedded FROM chunks WHERE message_key=? AND part='body'", (k,)).fetchall()
    assert body and all(r[0] == 1 for r in body)
    assert len(_vec_ids(store)) == 64 + len(body)


def test_image_attachment_enqueues_ocr_job(pipeline_with_queue, corpus_dir):
    p = _first_with(corpus_dir, "image")
    outcome = pipeline_with_queue.ingest_file(p, 1)
    assert outcome.ocr_jobs == 1 and pipeline_with_queue.jobs[0]["pages"] == [0]
    assert pipeline_with_queue.jobs == [{"path": str(p), "sha256": sha256_file(p), "ordinal": 0, "pages": [0]}]
    status = pipeline_with_queue.store.conn.execute(
        "SELECT ocr_status FROM attachments WHERE message_key=?", (outcome.message_key,)
    ).fetchone()[0]
    assert status == "pending"


def test_no_ocr_engine_marks_attachment_text_not_recovered(pipeline, corpus_dir):
    store = pipeline.store
    outcome = pipeline.ingest_file(_first_with(corpus_dir, "image"), 1)
    k = outcome.message_key
    assert outcome.ocr_jobs == 0
    assert store.conn.execute("SELECT ocr_status FROM attachments WHERE message_key=?", (k,)).fetchone()[0] == "unavailable"


def test_attach_ocr_text_makes_text_searchable(pipeline, corpus_dir):
    p = _first_with(corpus_dir, "image")
    _scan(pipeline, corpus_dir)
    k = pipeline.ingest_file(p, 1).message_key
    sha = pipeline.store.get_file(str(p)).sha256
    assert not keyword_search(pipeline.store.conn, "zebra", 10, None)
    pipeline.attach_ocr_text(sha, 0, "zebra crossing report")
    hits = keyword_search(pipeline.store.conn, "zebra", 10, None)
    assert hits
    att = pipeline.store.conn.execute("SELECT id, embedded FROM chunks WHERE message_key=? AND part='att:0'", (k,)).fetchall()
    assert [h[0] for h in hits] == [r["id"] for r in att]
    assert all(r["embedded"] == int(pipeline.embedder is not None) for r in att)
    status = pipeline.store.conn.execute(
        "SELECT extract_status, ocr_status FROM attachments WHERE message_key=? AND ordinal=0", (k,)
    ).fetchone()
    assert tuple(status) == ("ok", "ok")
    assert pipeline.cache.get(sha).attachments[0].text == "zebra crossing report"  # no text layer: OCR text alone
    text_chars = pipeline.store.conn.execute("SELECT text_chars FROM attachments WHERE message_key=?", (k,)).fetchone()[0]
    assert text_chars == len("zebra crossing report")


def test_attach_ocr_text_appends_to_the_text_layer(pipeline, tmp_path):
    folder = tmp_path / "mixed"
    pdf = write_minimal_pdf("Page one holds the contract summary.", pages=2)  # page 2 is a scan: no text layer
    path = _eml(folder, "m.eml", "The signed contract is attached.", ("contract.pdf", pdf))
    _scan(pipeline, folder)
    k = pipeline.ingest_file(path, 1).message_key
    sha = pipeline.store.get_file(str(path)).sha256
    layer = pipeline.cache.get(sha).attachments[0]
    assert layer.status == "needs_ocr" and "contract summary" in layer.text
    pipeline.attach_ocr_text(sha, 0, "zebra crossing report")
    combined = layer.text + "\n\n" + "zebra crossing report"
    assert pipeline.cache.get(sha).attachments[0].text == combined
    att = " ".join(r[0] for r in pipeline.store.conn.execute(
        "SELECT text FROM chunks WHERE message_key=? AND part='att:0' ORDER BY idx", (k,)
    ))
    assert "contract summary" in att and "zebra crossing report" in att
    att_ids = {r[0] for r in pipeline.store.conn.execute("SELECT id FROM chunks WHERE message_key=? AND part='att:0'", (k,))}
    for word in ("zebra", "holds"):  # one word from the OCR text, one from the text layer
        assert {h[0] for h in keyword_search(pipeline.store.conn, word, 10, None)} == att_ids
    text_chars = pipeline.store.conn.execute("SELECT text_chars FROM attachments WHERE message_key=?", (k,)).fetchone()[0]
    assert text_chars == len(combined)


def test_apply_scan_end_to_end(pipeline, corpus_dir):
    r = reconcile(pipeline.store, [corpus_dir], 1, 1)
    pipeline.apply_scan(r)
    n = pipeline.store.counts()["messages"]
    statuses = {row[0] for row in pipeline.store.conn.execute("SELECT parse_status FROM files")}
    assert statuses == {"indexed"}
    victim = max(corpus_dir.glob("*.eml"))
    victim.unlink()
    pipeline.apply_scan(reconcile(pipeline.store, [corpus_dir], 2, 2))
    pipeline.apply_scan(reconcile(pipeline.store, [corpus_dir], 3, 3))
    assert pipeline.store.counts()["messages"] == n - 1
    assert pipeline.store.conn.execute("SELECT SUM(message_count) FROM chains").fetchone()[0] == n - 1


def test_apply_scan_emits_renamed_and_file_indexed_events(tmp_store, tmp_path, corpus_dir, chunker, parse_pool):
    events = []
    pipeline = IngestPipeline(
        tmp_store, ParseCache(tmp_path / "cache"), parse_pool, chunker, None, None, on_event=events.append
    )
    outcomes = pipeline.apply_scan(reconcile(tmp_store, [corpus_dir], 1, 1))
    indexed = [e for e in events if e["type"] == "file_indexed"]
    assert [(e["path"], e["status"], e["message_key"]) for e in indexed] == [
        (o.path, o.status, o.message_key) for o in outcomes
    ]
    assert len(indexed) == len(list(corpus_dir.glob("*.eml")))
    events.clear()
    first = min(corpus_dir.glob("*.eml"))
    renamed = corpus_dir / "renamed.eml"
    first.rename(renamed)
    assert pipeline.apply_scan(reconcile(tmp_store, [corpus_dir], 2, 2)) == []
    assert events == [{"type": "renamed", "old": str(first), "new": str(renamed)}]


def test_cache_hit_skips_parse(pipeline, corpus_dir, monkeypatch):
    p = min(corpus_dir.glob("*.eml"))
    pipeline.ingest_file(p, 1)
    copy = corpus_dir / "copy.eml"
    copy.write_bytes(p.read_bytes())
    monkeypatch.setattr(pipeline.pool, "parse", lambda path: pytest.fail("parsed despite cache hit"))
    assert pipeline.ingest_file(copy, 1).status == "indexed"


@pytest.mark.usefixtures("_reset_package_logger")
def test_info_log_contains_no_subjects(pipeline, corpus_dir, tmp_path):
    configure_logging("emailsupersearch", tmp_path, "INFO")
    [pipeline.ingest_file(p, 1) for p in corpus_dir.glob("*.eml")]
    log = (tmp_path / "logs" / "emailsupersearch.log").read_text()
    assert all(subject not in log for subject, _, _ in TOPICS)
    assert "<subj:" in log  # the pipeline did log, with the subjects hashed
    assert str(corpus_dir) not in log


def test_chunk_text_is_capped_at_10000_chars(pipeline, tmp_path):
    folder = tmp_path / "long"
    path = _eml(folder, "token.eml", "Q7xk9" * 10_000)  # one 50,000-character token the chunker cannot split
    _scan(pipeline, folder)
    outcome = pipeline.ingest_file(path, 1)
    texts = [r[0] for r in pipeline.store.conn.execute("SELECT text FROM chunks WHERE message_key=?", (outcome.message_key,))]
    assert texts and all(len(t) <= CHUNK_TEXT_MAX_CHARS == 10_000 for t in texts)
    assert outcome.status == "indexed" and outcome.reason == "chunk text truncated"
    assert "chunk text truncated" in pipeline.store.get_file(str(path)).status_reason


@pytest.mark.parametrize(
    "error, reason",
    [(ParseTimeout("x.eml"), "timeout"), (ParseMemoryExceeded("grew"), "memory_cap"), (ParseError("bad header"), "bad header")],
)
def test_failed_parse_marks_file_failed(pipeline, corpus_dir, monkeypatch, error, reason):
    p = min(corpus_dir.glob("*.eml"))
    _scan(pipeline, corpus_dir)
    monkeypatch.setattr(pipeline.pool, "parse", _raise(error))
    outcome = pipeline.ingest_file(p, 1)
    assert outcome == IngestOutcome(str(p), None, "failed", reason)
    rec = pipeline.store.get_file(str(p))
    assert (rec.parse_status, rec.status_reason, rec.message_key) == ("failed", reason, None)
    assert pipeline.store.counts()["messages"] == 0


@pytest.mark.parametrize(
    "message", ["worker died: BrokenProcessPool('a child process terminated abruptly')", "pool closed"]
)
def test_parse_timeout_records_messages_other_than_timeout(pipeline, corpus_dir, monkeypatch, message):
    p = min(corpus_dir.glob("*.eml"))
    _scan(pipeline, corpus_dir)
    monkeypatch.setattr(pipeline.pool, "parse", _raise(ParseTimeout(p, message)))
    outcome = pipeline.ingest_file(p, 1)
    assert (outcome.status, outcome.reason) == ("failed", message)
    assert pipeline.store.get_file(str(p)).status_reason == message


def test_failed_reingest_keeps_indexed_message(pipeline, corpus_dir, monkeypatch):
    p = min(corpus_dir.glob("*.eml"))
    _scan(pipeline, corpus_dir)
    k = pipeline.ingest_file(p, 1).message_key
    before = _snapshot(pipeline.store, k)
    p.write_bytes(b"X-Changed: 1\n" + p.read_bytes())  # new bytes, so the cache misses and the pool parses
    monkeypatch.setattr(pipeline.pool, "parse", _raise(ParseTimeout(p)))
    assert pipeline.ingest_file(p, 2) == IngestOutcome(str(p), k, "failed", "timeout")
    rec = pipeline.store.get_file(str(p))
    assert (rec.message_key, rec.parse_status, rec.status_reason) == (k, "failed", "timeout")
    assert _snapshot(pipeline.store, k) == before
    assert pipeline.store.conn.execute("SELECT SUM(message_count) FROM chains").fetchone()[0] == 1


def test_failed_reingest_after_the_parse_keeps_indexed_message(pipeline, corpus_dir, monkeypatch):
    p = min(corpus_dir.glob("*.eml"))
    _scan(pipeline, corpus_dir)
    k = pipeline.ingest_file(p, 1).message_key
    before = _snapshot(pipeline.store, k)
    p.write_bytes(p.read_bytes().replace(b"Invoice INV", b"Receipt INV"))  # new text, new chunks if committed
    monkeypatch.setattr(store_module, "recompute_chain_row", _boom)  # the transaction's last write fails
    assert pipeline.ingest_file(p, 2) == IngestOutcome(str(p), k, "failed", "RuntimeError: boom")
    rec = pipeline.store.get_file(str(p))
    assert (rec.message_key, rec.parse_status) == (k, "failed")
    assert _snapshot(pipeline.store, k) == before  # every write of the attempt was rolled back


def test_failed_reingest_when_embedder_raises_keeps_vectors(pipeline, corpus_dir, monkeypatch, embedder):
    p = _first_with(corpus_dir, "docx")
    _scan(pipeline, corpus_dir)
    k = pipeline.ingest_file(p, 1).message_key
    before = _snapshot(pipeline.store, k)
    assert before[2] and before[3] and all(c[4] == 1 for c in before[1])  # vectors, attachment rows, embedded flags
    p.write_bytes(b"X-Changed: 1\n" + p.read_bytes())
    monkeypatch.setattr(pipeline.embedder, "embed", _boom)
    outcome = pipeline.ingest_file(p, 2)
    assert (outcome.status, outcome.message_key, outcome.reason) == ("failed", k, "RuntimeError: boom")
    assert pipeline.store.get_file(str(p)).parse_status == "failed"
    assert _snapshot(pipeline.store, k) == before


def test_reingest_with_a_different_message_drops_the_old_one(pipeline, tmp_path):
    store = pipeline.store
    folder = tmp_path / "swap"
    path = _eml(folder, "RE Budget.eml", "The first email.", message_id="<first@example.com>")
    _scan(pipeline, folder)
    assert pipeline.ingest_file(path, 1).message_key == "first@example.com"
    _eml(folder, "RE Budget.eml", "A later email saved under the same name.", message_id="<second@example.com>")
    assert pipeline.ingest_file(path, 2).message_key == "second@example.com"
    assert store.message("first@example.com") is None and store.chunk_rowids("first@example.com") == []
    assert store.get_file(str(path)).message_key == "second@example.com"
    assert store.conn.execute("SELECT SUM(message_count) FROM chains").fetchone()[0] == 1


def test_unexpected_error_marks_file_failed_and_leaves_no_orphan(pipeline, corpus_dir, monkeypatch, caplog):
    p = min(corpus_dir.glob("*.eml"))
    _scan(pipeline, corpus_dir)
    events = []
    pipeline.on_event = events.append
    monkeypatch.setattr(store_module, "recompute_chain_row", _boom)  # fails inside the write transaction
    with caplog.at_level(logging.WARNING, logger="emailsupersearch"):
        outcome = pipeline.ingest_file(p, 1)
    assert outcome == IngestOutcome(str(p), None, "failed", "RuntimeError: boom")  # first time: no message kept
    assert events == [{"type": "file_indexed", "path": str(p), "status": "failed", "message_key": None}]
    rec = pipeline.store.get_file(str(p))
    assert (rec.parse_status, rec.status_reason, rec.message_key) == ("failed", "RuntimeError: boom", None)
    assert pipeline.store.message("t000-m00@corpus.example") is None
    assert pipeline.store.counts()["chunks"] == 0
    assert any(r.levelno == logging.WARNING and "RuntimeError" in r.getMessage() for r in caplog.records)
    assert not any(str(p) in r.getMessage() or "Printer lease" in r.getMessage() for r in caplog.records)


def test_os_error_reason_names_no_path(pipeline, tmp_path):
    missing = tmp_path / "gone" / "vanished.eml"
    outcome = pipeline.ingest_file(missing, 1)
    assert (outcome.status, outcome.reason) == ("failed", "No such file or directory")


def test_parse_error_reason_is_redacted(pipeline, corpus_dir, monkeypatch):
    p = min(corpus_dir.glob("*.eml"))
    _scan(pipeline, corpus_dir)
    error = ParseError(f"FileNotFoundError: [Errno 2] No such file or directory: '{p}'")
    monkeypatch.setattr(pipeline.pool, "parse", _raise(error))
    outcome = pipeline.ingest_file(p, 1)
    assert outcome.reason.startswith("FileNotFoundError: [Errno 2] No such file or directory: '<path:")
    assert str(p) not in outcome.reason and str(p) not in pipeline.store.get_file(str(p)).status_reason


def test_cache_put_failure_still_indexes(pipeline, corpus_dir, monkeypatch, caplog):
    def disk_full(doc):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pipeline.cache, "put", disk_full)
    with caplog.at_level(logging.WARNING, logger="emailsupersearch"):
        outcome = pipeline.ingest_file(min(corpus_dir.glob("*.eml")), 1)
    assert outcome.status == "indexed"
    assert any(r.levelno == logging.WARNING and "OSError" in r.getMessage() for r in caplog.records)


def test_ocr_queue_failure_marks_attachment_unavailable(pipeline, corpus_dir, caplog):
    def locked(payload):
        raise sqlite3.OperationalError("database is locked")

    pipeline.enqueue_ocr = locked
    with caplog.at_level(logging.WARNING, logger="emailsupersearch"):
        outcome = pipeline.ingest_file(_first_with(corpus_dir, "image"), 1)
    assert (outcome.status, outcome.ocr_jobs) == ("indexed", 0)
    status = pipeline.store.conn.execute(
        "SELECT ocr_status FROM attachments WHERE message_key=?", (outcome.message_key,)
    ).fetchone()[0]
    assert status == "unavailable"
    assert any(r.levelno == logging.WARNING and "OperationalError" in r.getMessage() for r in caplog.records)


def test_ocr_job_names_the_cache_entry(pipeline_with_queue, corpus_dir, monkeypatch):
    p = _first_with(corpus_dir, "image")
    monkeypatch.setattr(pipeline_module, "sha256_file", lambda path: "0" * 64)  # the file changed after it was hashed
    pipeline_with_queue.ingest_file(p, 1)
    job = pipeline_with_queue.jobs[0]
    assert job["sha256"] == sha256_file(p) and pipeline_with_queue.cache.get(job["sha256"]) is not None


def test_apply_scan_removes_purged_before_parsing_and_survives_one_failure(pipeline, corpus_dir, monkeypatch, caplog):
    store = pipeline.store
    pipeline.apply_scan(reconcile(store, [corpus_dir], 1, 1))
    victims = [max(corpus_dir.glob("t000-*.eml")), max(corpus_dir.glob("t001-*.eml"))]  # two chains
    keys = [store.get_file(str(v)).message_key for v in victims]
    for victim in victims:
        victim.unlink()
    pipeline.apply_scan(reconcile(store, [corpus_dir], 2, 2))  # tombstones them
    (corpus_dir / "new.eml").write_bytes(make_eml_bytes())
    order = []
    real_update = pipeline_module.update_chain_row

    def flaky_update(s, chain_id):
        order.append("chain")
        if order.count("chain") == 1:
            raise RuntimeError("chain row")
        real_update(s, chain_id)

    monkeypatch.setattr(pipeline_module, "update_chain_row", flaky_update)
    pipeline.on_event = lambda event: order.append(event["type"])
    result = reconcile(store, [corpus_dir], 3, 3)
    assert sorted(r.message_key for r in result.purged) == sorted(keys)
    assert [p.name for p in result.to_parse] == ["new.eml"]
    with caplog.at_level(logging.WARNING, logger="emailsupersearch"):
        outcomes = pipeline.apply_scan(result)
    assert all(store.message(k) is None for k in keys)
    assert [o.status for o in outcomes] == ["indexed"]
    assert order == ["chain", "chain", "file_indexed"]  # both removals ran, and before the parse
    assert any(r.levelno == logging.WARNING and "RuntimeError" in r.getMessage() for r in caplog.records)


def test_attach_ocr_text_caps_chunk_text(pipeline, corpus_dir):
    p = _first_with(corpus_dir, "image")
    _scan(pipeline, corpus_dir)
    k = pipeline.ingest_file(p, 1).message_key
    pipeline.attach_ocr_text(pipeline.store.get_file(str(p)).sha256, 0, "Q7xk9" * 10_000)
    texts = [r[0] for r in pipeline.store.conn.execute("SELECT text FROM chunks WHERE message_key=? AND part='att:0'", (k,))]
    assert texts and all(len(t) <= CHUNK_TEXT_MAX_CHARS for t in texts)
    rec = pipeline.store.get_file(str(p))
    assert (rec.message_key, rec.parse_status, rec.status_reason) == (k, "indexed", "chunk text truncated")


def test_reingest_removes_stale_attachment_parts(pipeline, tmp_path):
    store = pipeline.store
    folder = tmp_path / "re"
    path = _eml(folder, "m.eml", "The renewal terms are below.", ("notes.docx", att_docx("Pricing notes for the renewal.").data))
    k = pipeline.ingest_file(path, 1).message_key
    old = [r[0] for r in store.conn.execute("SELECT id FROM chunks WHERE message_key=? AND part='att:0'", (k,))]
    assert old
    _eml(folder, "m.eml", "The renewal terms are below.")  # the same message, saved again without its attachment
    assert pipeline.ingest_file(path, 1).message_key == k
    assert _parts(store, k) == ["body"]
    assert not _vec_ids(store) & set(old)
    assert store.conn.execute("SELECT COUNT(*) FROM attachments WHERE message_key=?", (k,)).fetchone()[0] == 0


def test_duplicate_row_is_marked_indexed_with_key(pipeline, corpus_dir):
    src = min(corpus_dir.glob("*.eml"))
    dup = corpus_dir / "dup.eml"
    dup.write_bytes(b"X-Export: 2\r\n" + src.read_bytes())
    _scan(pipeline, corpus_dir)
    k = pipeline.ingest_file(src, 1).message_key
    assert pipeline.ingest_file(dup, 1) == IngestOutcome(str(dup), k, "skipped_duplicate", "duplicate")
    rec = pipeline.store.get_file(str(dup))
    assert (rec.message_key, rec.parse_status, rec.status_reason) == (k, "indexed", "duplicate")
    rec = pipeline.store.get_file(str(src))
    assert (rec.message_key, rec.parse_status, rec.status_reason) == (k, "indexed", "")


class _PoisonEmbedder:
    """Stands in for the embedder: a unit vector per text, but NaN for any text that mentions "poison"."""

    def embed(self, texts, batch_size=32):
        out = np.zeros((len(texts), 384), dtype=np.float32)
        out[:, 0] = 1.0
        for i, text in enumerate(texts):
            if "poison" in text:
                out[i] = np.nan
        return out


def test_non_finite_embeddings_are_not_stored(tmp_store, tmp_path, chunker, parse_pool, caplog):
    pipeline = IngestPipeline(
        tmp_store, ParseCache(tmp_path / "cache"), parse_pool, chunker, _PoisonEmbedder(), VectorIndex(tmp_store.conn)
    )
    path = _eml(tmp_path / "nan", "m.eml", "Ordinary words about the renewal.", ("x.docx", att_docx("A poison pill.").data))
    with caplog.at_level(logging.DEBUG, logger="emailsupersearch.ingest.pipeline"):
        k = pipeline.ingest_file(path, 1).message_key
    rows = {r["part"]: (r["id"], r["embedded"]) for r in tmp_store.conn.execute(
        "SELECT id, part, embedded FROM chunks WHERE message_key=?", (k,)
    )}
    (body_id, body_embedded), (att_id, att_embedded) = rows["body"], rows["att:0"]
    assert (body_embedded, att_embedded) == (1, 0)
    assert _vec_ids(tmp_store) == {body_id}
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and "non-finite" in r.getMessage()]
    assert len(debug) == 1 and str(att_id) in debug[0] and "poison" not in debug[0]


def test_chain_steps_run_without_the_store_lock_held(pipeline, corpus_dir, monkeypatch):
    held = []

    def watch(fn):
        def wrapper(*args, **kwargs):
            held.append((fn.__name__, pipeline.store.lock._is_owned()))
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(pipeline_module, "assign_chain", watch(pipeline_module.assign_chain))
    monkeypatch.setattr(pipeline.store, "write_indexed_message", watch(pipeline.store.write_indexed_message))
    outcome = pipeline.ingest_file(min(corpus_dir.glob("*.eml")), 1)
    assert outcome.status == "indexed"
    assert held == [("assign_chain", False), ("write_indexed_message", False)]  # the store takes its own lock
    assert pipeline.store.conn.execute("SELECT message_count FROM chains").fetchone()[0] == 1
