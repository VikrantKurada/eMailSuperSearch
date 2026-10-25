import importlib.machinery
import importlib.util
import subprocess
import sys

import pytest
from helpers import make_eml_with_png_bytes, write_text_png

from emailsupersearch.ingest.ocr import base
from emailsupersearch.ingest.ocr.base import (
    EngineInfo,
    OcrUnavailable,
    build_engine,
    select_engine,
)
from emailsupersearch.ingest.ocr.rapid_ocr import RapidOcr
from emailsupersearch.ingest.ocr.windows_ocr import WindowsOcr
from emailsupersearch.ingest.ocr.worker import OcrJobFailed, OcrRunner, run_ocr_job


def test_windows_ocr_reads_rendered_text():
    eng = WindowsOcr(); eng.available("en") or pytest.skip("no en OCR language"); assert "hello world" in eng.recognize(write_text_png("hello world")).lower()


def test_rapid_ocr_reads_rendered_text():
    pytest.importorskip("rapidocr"); assert "hello world" in RapidOcr().recognize(write_text_png("hello world")).lower()


def test_select_engine_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(WindowsOcr, "available", lambda self, l: False); monkeypatch.setattr(base, "_rapid_available", lambda: False); assert select_engine("en") is None


def _fake_rapidocr(monkeypatch, tmp_path, models=("det", "cls", "rec")):
    """Make ``find_spec("rapidocr")`` point at a package folder under ``tmp_path`` holding the given model files."""
    package = tmp_path / "rapidocr"
    (package / "models").mkdir(parents=True)
    for name in models:
        (package / "models" / f"ch_PP-OCRv4_{name}_infer.onnx").write_bytes(b"onnx")
    real = importlib.util.find_spec
    spec = importlib.machinery.ModuleSpec("rapidocr", None, is_package=True)
    spec.submodule_search_locations = [str(package)]
    monkeypatch.setattr(base.importlib.util, "find_spec", lambda name, *a: spec if name == "rapidocr" else real(name, *a))
    return package


def test_select_engine_names_rapid_without_constructing_it(monkeypatch, tmp_path):
    package = _fake_rapidocr(monkeypatch, tmp_path)
    monkeypatch.setattr(WindowsOcr, "available", lambda self, l: False)

    def no_construction(self):
        raise AssertionError("the host must never construct an OCR engine")

    monkeypatch.setattr(RapidOcr, "__init__", no_construction)
    assert select_engine("en") == EngineInfo("rapid")
    assert base.rapid_model_paths()["Det.model_path"] == str(package / "models" / "ch_PP-OCRv4_det_infer.onnx")


def test_rapid_without_its_model_files_is_not_available(monkeypatch, tmp_path):
    _fake_rapidocr(monkeypatch, tmp_path, models=("det", "cls"))
    monkeypatch.setattr(WindowsOcr, "available", lambda self, l: False)
    assert select_engine("en") is None and build_engine("en") is None
    with pytest.raises(OcrUnavailable, match="rec"):
        base.rapid_model_paths()


def test_select_engine_imports_no_engine_package():
    script = (
        "import sys\n"
        "from emailsupersearch.ingest.ocr.base import select_engine\n"
        "engine = select_engine('en')\n"
        "print(engine.name if engine else None, 'rapidocr' in sys.modules, 'onnxruntime' in sys.modules)\n"
    )
    done = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    name, rapid_loaded, onnx_loaded = done.stdout.split()
    assert name in ("windows", "rapid", "None") and (rapid_loaded, onnx_loaded) == ("False", "False")


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
