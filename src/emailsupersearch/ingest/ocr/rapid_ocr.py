"""OCR through rapidocr (the optional ``ocr`` extra), for machines without a Windows OCR language pack.

The engine runs only with model files already inside the installed ``rapidocr/models`` directory: they are passed
by path so rapidocr never downloads one, and ``RapidOcr()`` raises ``OcrUnavailable`` when any is missing.
rapidocr is imported inside the functions, so importing this module costs nothing where it is absent.
"""

from __future__ import annotations

import io
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from emailsupersearch.ingest.ocr.base import OcrUnavailable

_MODEL_GLOBS = {"Det.model_path": "*det*.onnx", "Cls.model_path": "*cls*.onnx", "Rec.model_path": "*rec*.onnx"}


def _model_paths() -> dict[str, str]:
    """The detection, classification and recognition model files bundled with rapidocr; raises ``OcrUnavailable``."""
    try:
        models = Path(str(resources.files("rapidocr") / "models"))
    except (ImportError, TypeError) as e:  # TypeError: a namespace package with no files
        raise OcrUnavailable("rapidocr is not installed") from e
    params: dict[str, str] = {}
    for key, pattern in _MODEL_GLOBS.items():
        found = sorted(models.glob(pattern)) if models.is_dir() else []
        if not found:
            raise OcrUnavailable(f"rapidocr model file missing: {pattern}")
        params[key] = str(found[0])
    return params


def _texts(output: Any) -> list[str]:
    """The recognised lines from either result shape: an object with ``txts``, or rows of ``[box, text, score]``."""
    if hasattr(output, "txts"):
        return [text for text in (output.txts or ()) if text]
    rows = output[0] if isinstance(output, tuple) else output  # older releases return (rows, elapsed)
    return [row[1] for row in rows or () if row[1]]


class RapidOcr:
    """rapidocr with its bundled models; its models read Latin and Chinese script, whatever the language."""

    name = "rapid"

    def __init__(self) -> None:
        params = _model_paths()
        try:
            from rapidocr import RapidOCR

            self._engine = RapidOCR(params=params)
        except Exception as e:  # any failure to start means the engine cannot run here
            raise OcrUnavailable(f"rapidocr could not start: {type(e).__name__}") from e

    def available(self, language: str) -> bool:
        """True: an instance exists only once the model files were found, and they are not per language."""
        return True

    def recognize(self, png: bytes) -> str:
        """The text of an image, one recognised line per line."""
        with Image.open(io.BytesIO(png)) as opened:
            rgb = np.asarray(opened.convert("RGB"))
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])  # rapidocr takes arrays in OpenCV's channel order
        return "\n".join(_texts(self._engine(bgr)))
