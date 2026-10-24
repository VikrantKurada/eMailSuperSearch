import logging
from email.message import EmailMessage

import numpy as np
import pytest
from helpers import LONG_TEXT_30000_WORDS, att_docx

from emailsupersearch import logging_setup
from emailsupersearch.diagnostics.corpus import TOPICS
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

_DOCX = ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")


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
    """Write one message to ``folder / name``; ``attachment`` is ``(filename, docx bytes)``."""
    msg = EmailMessage()
    msg["Subject"] = "Quarterly supplier report"
    msg["From"] = "Alice Morgan <alice.morgan@example.com>"
    msg["To"] = "Ben Carter <ben.carter@example.com>"
    msg["Date"] = "Mon, 05 Jan 2026 09:00:00 +0000"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    if attachment is not None:
        filename, data = attachment
        msg.add_attachment(data, maintype=_DOCX[0], subtype=_DOCX[1], filename=filename)
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
    assert pipeline.cache.get(sha).attachments[0].text == "zebra crossing report"


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


def test_failed_reparse_drops_the_message_the_file_held(pipeline, corpus_dir, monkeypatch):
    p = min(corpus_dir.glob("*.eml"))
    _scan(pipeline, corpus_dir)
    k = pipeline.ingest_file(p, 1).message_key
    p.write_bytes(b"X-Changed: 1\n" + p.read_bytes())  # new bytes, so the cache misses and the pool parses
    monkeypatch.setattr(pipeline.pool, "parse", _raise(ParseError("broken")))
    assert pipeline.ingest_file(p, 2).status == "failed"
    assert pipeline.store.message(k) is None and pipeline.store.chunk_rowids(k) == []
    assert pipeline.store.conn.execute("SELECT COUNT(*) FROM chains").fetchone()[0] == 0


def test_unexpected_error_marks_file_failed_and_leaves_no_orphan(pipeline, corpus_dir, monkeypatch, caplog):
    p = min(corpus_dir.glob("*.eml"))
    _scan(pipeline, corpus_dir)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline.store, "replace_chunks", boom)  # fails after the message row is written
    with caplog.at_level(logging.WARNING, logger="emailsupersearch"):
        outcome = pipeline.ingest_file(p, 1)
    assert (outcome.status, outcome.reason) == ("failed", "RuntimeError: boom")
    assert outcome.message_key == "t000-m00@corpus.example"
    rec = pipeline.store.get_file(str(p))
    assert (rec.parse_status, rec.status_reason, rec.message_key) == ("failed", "RuntimeError: boom", None)
    assert pipeline.store.message(outcome.message_key) is None  # the half-written message went with the link
    assert any(r.levelno == logging.WARNING and "RuntimeError" in r.getMessage() for r in caplog.records)
    assert not any(str(p) in r.getMessage() or "Printer lease" in r.getMessage() for r in caplog.records)


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
    monkeypatch.setattr(pipeline_module, "update_chain_row", watch(pipeline_module.update_chain_row))
    monkeypatch.setattr(pipeline.store, "upsert_message", watch(pipeline.store.upsert_message))
    assert pipeline.ingest_file(min(corpus_dir.glob("*.eml")), 1).status == "indexed"
    assert held == [("assign_chain", False), ("upsert_message", False), ("update_chain_row", False)]
