from email.message import EmailMessage

import pytest

from emailsupersearch.diagnostics.corpus import generate
from emailsupersearch.ingest import workers
from emailsupersearch.ingest.parse import ParseError
from emailsupersearch.ingest.parse_cache import sha256_file
from emailsupersearch.ingest.workers import (
    ParseMemoryExceeded,
    ParsePool,
    ParseTimeout,
    parse_document,
)


def test_parse_pool_parses_eml(tmp_path):
    p = generate(tmp_path, 1, seed=1)[0]; pool = ParsePool(); doc = pool.parse(p); assert doc.message.subject and doc.sha256 == sha256_file(p); pool.close()
def test_parse_pool_times_out_and_recovers(tmp_path, monkeypatch):
    pool = ParsePool(timeout_s=0.5); monkeypatch.setattr(workers, "_worker_fn", workers._sleep_forever_for_tests)
    with pytest.raises(ParseTimeout): pool.parse(tmp_path / "any.eml")
    monkeypatch.undo(); assert pool.parse(generate(tmp_path, 1, seed=1)[0]).message.subject; pool.close()
def test_parse_pool_memory_cap_fails_file_not_pool(tmp_path, monkeypatch):
    pool = ParsePool(max_rss_mb=64); monkeypatch.setattr(workers, "_worker_fn", workers._allocate_for_tests)  # allocates 256 MB over the baseline, then applies the same growth check
    with pytest.raises(ParseMemoryExceeded): pool.parse(tmp_path / "any.eml")
    monkeypatch.undo(); assert pool.parse(generate(tmp_path, 1, seed=1)[0]).message.subject; pool.close()


def _nested_eml(path):
    inner = EmailMessage()
    inner["Subject"] = "inner subject"
    inner.set_content("inner body")
    inner.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="deep.pdf")
    outer = EmailMessage()
    outer["Subject"] = "outer"
    outer["From"] = "a@example.com"
    outer["To"] = "b@example.com"
    outer.set_content("hi")
    outer.add_attachment(inner)
    path.write_bytes(outer.as_bytes())
    return path


def test_parse_document_has_one_text_entry_per_attachment_and_no_bytes(tmp_path):
    p = generate(tmp_path, 1, seed=1)[-1]
    doc = parse_document(p)
    assert doc.sha256 == sha256_file(p)
    assert [(a.ordinal, a.kind, a.status) for a in doc.attachments] == [(0, "pdf", "ok")]
    assert all(a.data is None for a in doc.message.attachments)


def test_parse_document_flattens_a_nested_message_into_its_text(tmp_path):
    doc = parse_document(_nested_eml(tmp_path / "n.eml"))
    assert [(a.kind, a.status) for a in doc.attachments] == [("eml", "ok")]
    assert "inner subject" in doc.attachments[0].text and "inner body" in doc.attachments[0].text
    nested = doc.message.attachments[0].nested
    assert doc.message.attachments[0].data is None and nested.attachments[0].data is None


def test_parse_pool_passes_parse_errors_through(tmp_path):
    (tmp_path / "x.txt").write_text("not mail", encoding="utf-8")
    pool = ParsePool()
    try:
        with pytest.raises(ParseError, match="unsupported file type"):
            pool.parse(tmp_path / "x.txt")
        assert pool.parse(generate(tmp_path, 1, seed=1)[0]).message.subject  # the same worker carries on
    finally:
        pool.close()


def test_parse_pool_recovers_from_a_dead_worker(tmp_path):
    p = generate(tmp_path, 1, seed=1)[0]
    pool = ParsePool()
    try:
        assert pool.parse(p).message.subject
        for process in list(pool._executor._processes.values()):
            process.kill()
        with pytest.raises(ParseTimeout) as caught:
            pool.parse(p)
        assert caught.value.path == p and "worker died" in caught.value.reason
        assert pool.parse(p).message.subject
    finally:
        pool.close()
