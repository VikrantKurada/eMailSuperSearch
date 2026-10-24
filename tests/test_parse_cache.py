import json
import threading

import pytest

from emailsupersearch.ingest import parse_cache
from emailsupersearch.ingest.parse_cache import ParseCache, sha256_file
from emailsupersearch.types import ParsedAttachment, ParsedDocument


def test_cache_roundtrip(tmp_path, eml_doc):
    c = ParseCache(tmp_path); assert c.get(eml_doc.sha256) is None; c.put(eml_doc)
    back = c.get(eml_doc.sha256); assert back.message.subject == eml_doc.message.subject and back.attachments[0].text == eml_doc.attachments[0].text
def test_cache_set_ocr_text(tmp_path, eml_doc_with_image):
    c = ParseCache(tmp_path); c.put(eml_doc_with_image); c.set_ocr_text(eml_doc_with_image.sha256, 0, "recovered words")
    a = c.get(eml_doc_with_image.sha256).attachments[0]; assert (a.status, a.text, a.needs_ocr_pages) == ("ok", "recovered words", [])


def test_cache_roundtrip_is_lossless(tmp_path, eml_doc):
    c = ParseCache(tmp_path / "cache")
    c.put(eml_doc)
    assert c.get(eml_doc.sha256) == eml_doc
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [f"{eml_doc.sha256}.json"]  # no temporary file left


def test_cache_stores_hex_conversation_index_and_no_attachment_bytes(tmp_path, parsed_factory):
    inner = parsed_factory(
        conversation_index=b"\x01\xff",
        attachments=[ParsedAttachment(0, "deep.pdf", "application/pdf", 3, "pdf", data=b"abc")],
    )
    outer = parsed_factory(
        conversation_index=b"\x0a\x0b",
        attachments=[ParsedAttachment(0, "fwd.eml", "message/rfc822", 3, "eml", data=b"raw", nested=inner)],
    )
    doc = ParsedDocument("ab" * 32, outer, [])
    c = ParseCache(tmp_path)
    c.put(doc)

    stored = json.loads((tmp_path / f"{doc.sha256}.json").read_text(encoding="utf-8"))
    att = stored["message"]["attachments"][0]
    assert stored["message"]["conversation_index"] == "0a0b" and att["nested"]["conversation_index"] == "01ff"
    assert att["data"] is None and att["nested"]["attachments"][0]["data"] is None

    back = c.get(doc.sha256).message
    assert back.conversation_index == b"\x0a\x0b" and back.recipients == outer.recipients
    assert back.attachments[0].nested.conversation_index == b"\x01\xff"
    assert back.attachments[0].nested.attachments[0].filename == "deep.pdf"
    assert doc.message.attachments[0].data == b"raw"  # put does not touch the document it was given


@pytest.mark.parametrize("content", ["not json {", "", "{}", '{"sha256": "x"}', "[]"])
def test_cache_ignores_an_unreadable_entry(tmp_path, content):
    (tmp_path / "ab.json").write_text(content, encoding="utf-8")
    assert ParseCache(tmp_path).get("ab") is None


def test_cache_set_ocr_text_needs_an_existing_attachment(tmp_path, eml_doc_with_image):
    c = ParseCache(tmp_path)
    with pytest.raises(KeyError):
        c.set_ocr_text("0" * 64, 0, "text")
    c.put(eml_doc_with_image)
    with pytest.raises(KeyError):
        c.set_ocr_text(eml_doc_with_image.sha256, 7, "text")


def test_sha256_file_is_the_hash_of_the_bytes(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_cache_put_survives_concurrent_reader(tmp_path, eml_doc):
    c = ParseCache(tmp_path / "cache")
    c.put(eml_doc)
    stop = threading.Event()
    failures = []

    def read():
        while not stop.is_set():
            try:
                c.get(eml_doc.sha256)
            except BaseException as e:  # noqa: BLE001 - the thread hands whatever went wrong to the test
                failures.append(e)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for _ in range(200):
            c.put(eml_doc)
    finally:
        stop.set()
        reader.join()
    assert failures == []
    assert c.get(eml_doc.sha256) == eml_doc
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [f"{eml_doc.sha256}.json"]


@pytest.fixture
def lock_rename(monkeypatch):
    """A function that, once called, makes every rename into place fail as a Windows reader holding the target does."""

    def lock():
        def refuse(src, dst):
            raise PermissionError(13, "in use")

        monkeypatch.setattr(parse_cache, "_REPLACE_RETRY_S", 0)
        monkeypatch.setattr(parse_cache.os, "replace", refuse)

    return lock


def test_cache_put_keeps_an_existing_entry_when_the_rename_keeps_failing(tmp_path, eml_doc, lock_rename):
    c = ParseCache(tmp_path / "cache")
    c.put(eml_doc)
    lock_rename()
    c.put(eml_doc)
    assert c.get(eml_doc.sha256) == eml_doc
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [f"{eml_doc.sha256}.json"]  # the temp file is gone


def test_cache_put_raises_when_there_is_no_entry_to_fall_back_on(tmp_path, eml_doc, lock_rename):
    c = ParseCache(tmp_path / "cache")
    lock_rename()
    with pytest.raises(PermissionError):
        c.put(eml_doc)
    assert list((tmp_path / "cache").iterdir()) == []


def test_cache_set_ocr_text_is_not_dropped_when_the_rename_keeps_failing(tmp_path, eml_doc_with_image, lock_rename):
    c = ParseCache(tmp_path / "cache")
    c.put(eml_doc_with_image)
    lock_rename()
    with pytest.raises(PermissionError):
        c.set_ocr_text(eml_doc_with_image.sha256, 0, "recovered words")
