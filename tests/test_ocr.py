import pytest
from helpers import make_eml_with_png_bytes, write_text_png

from emailsupersearch.ingest.ocr import base
from emailsupersearch.ingest.ocr.base import select_engine
from emailsupersearch.ingest.ocr.rapid_ocr import RapidOcr
from emailsupersearch.ingest.ocr.windows_ocr import WindowsOcr
from emailsupersearch.ingest.ocr.worker import OcrJobFailed, OcrRunner, run_ocr_job


def test_windows_ocr_reads_rendered_text():
    eng = WindowsOcr(); eng.available("en") or pytest.skip("no en OCR language"); assert "hello world" in eng.recognize(write_text_png("hello world")).lower()


def test_rapid_ocr_reads_rendered_text():
    pytest.importorskip("rapidocr"); assert "hello world" in RapidOcr().recognize(write_text_png("hello world")).lower()


def test_select_engine_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(WindowsOcr, "available", lambda self, l: False); monkeypatch.setattr(base, "_rapid_importable", lambda: False); assert select_engine("en") is None


def test_windows_ocr_available_false_for_unknown_language():
    assert WindowsOcr().available("xx-ZZ") is False


def test_run_ocr_job_on_image_attachment(tmp_path):
    p = tmp_path / "img.eml"; p.write_bytes(make_eml_with_png_bytes("zebra crossing")); select_engine("en") or pytest.skip(); assert "zebra" in run_ocr_job(str(p), 0, [0], "en").lower()


def test_ocr_runner_subprocess_roundtrip(tmp_path):
    p = tmp_path / "img.eml"; p.write_bytes(make_eml_with_png_bytes("zebra crossing")); select_engine("en") or pytest.skip()
    assert "zebra" in OcrRunner().run({"path": str(p), "ordinal": 0, "pages": [0]}, "en").lower()


def test_ocr_runner_reports_child_error(tmp_path):
    p = tmp_path / "missing" / "nope.eml"
    with pytest.raises(OcrJobFailed) as caught:
        OcrRunner().run({"path": str(p), "ordinal": 0, "pages": [0]}, "en")
    text = str(caught.value)
    assert ("ParseError" in text or "FileNotFoundError" in text) and str(p) not in text and "nope.eml" not in text
