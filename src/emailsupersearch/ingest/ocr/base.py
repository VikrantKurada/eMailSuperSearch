"""The OCR engine interface and the choice between Windows OCR and rapidocr.

``select_engine`` says which engine can run, for the host: the Windows OCR language pack for the language first,
else rapidocr when that package is installed with its model files, else None. It answers with an ``EngineInfo`` (a
name only) and never constructs an engine: rapidocr is found with ``importlib.util.find_spec`` and its model files
on disk, without importing it, so the host never loads rapidocr's native stack (onnxruntime, OpenCV). Engines are
built only inside the OCR child process, by ``build_engine``. Neither function raises, and engines never download
anything.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# The model files rapidocr needs, by the parameter that takes each, under the installed ``rapidocr/models`` folder.
RAPID_MODEL_GLOBS = {"Det.model_path": "*det*.onnx", "Cls.model_path": "*cls*.onnx", "Rec.model_path": "*rec*.onnx"}


class OcrUnavailable(Exception):
    """No OCR engine can run: the package, its model files or the language pack is missing."""


class OcrEngine(Protocol):
    name: str

    def available(self, language: str) -> bool: ...

    def recognize(self, png: bytes) -> str: ...


@dataclass(frozen=True)
class EngineInfo:
    """Which OCR engine can run: ``windows`` or ``rapid``. A name only; the engine itself is built in the OCR child."""

    name: str


def rapid_model_paths() -> dict[str, str]:
    """The detection, classification and recognition model files of the installed rapidocr, by the parameter that takes
    each; raises ``OcrUnavailable`` when rapidocr or a model file is missing. Finds the package without importing it."""
    try:
        spec = importlib.util.find_spec("rapidocr")
    except (ImportError, ValueError) as e:  # a broken install counts as absent
        raise OcrUnavailable("rapidocr is not installed") from e
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    if not locations:
        raise OcrUnavailable("rapidocr is not installed")
    models = Path(locations[0]) / "models"
    params: dict[str, str] = {}
    for key, pattern in RAPID_MODEL_GLOBS.items():
        found = sorted(models.glob(pattern)) if models.is_dir() else []
        if not found:
            raise OcrUnavailable(f"rapidocr model file missing: {pattern}")
        params[key] = str(found[0])
    return params


def _rapid_available() -> bool:
    """Whether rapidocr is installed with its model files, judged without importing it."""
    try:
        rapid_model_paths()
    except OcrUnavailable:
        return False
    return True


def _windows_available(language: str) -> bool:
    from emailsupersearch.ingest.ocr.windows_ocr import WindowsOcr

    try:
        return WindowsOcr(language).available(language)
    except Exception as e:  # noqa: BLE001 - choosing an engine must never fail the caller
        log.warning("Windows OCR could not be checked: %s", type(e).__name__)
        return False


def select_engine(language: str) -> EngineInfo | None:
    """Which engine can OCR ``language``: Windows OCR when it has the language pack, else rapidocr when installed with
    its models, else None. Constructs no engine (see the module docstring)."""
    if _windows_available(language):
        return EngineInfo("windows")
    if _rapid_available():
        return EngineInfo("rapid")
    return None


def build_engine(language: str) -> OcrEngine | None:
    """Construct the engine ``select_engine`` names, for the OCR child process (and the ``bench`` command, a process of
    its own); None when none can run. Never raises."""
    from emailsupersearch.ingest.ocr.windows_ocr import WindowsOcr

    if _windows_available(language):
        return WindowsOcr(language)
    if not _rapid_available():
        return None
    from emailsupersearch.ingest.ocr.rapid_ocr import RapidOcr

    try:
        return RapidOcr()
    except OcrUnavailable:
        log.info("rapidocr is installed but cannot start")
    except Exception as e:  # noqa: BLE001 - see above
        log.warning("rapidocr could not be started: %s", type(e).__name__)
    return None
