"""The OCR engine interface and the choice between Windows OCR and rapidocr.

``select_engine`` prefers the Windows OCR language pack for the language, falls back to rapidocr when that
package is installed with its model files, and returns None when neither can run; it never raises. Engines never
download anything.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class OcrUnavailable(Exception):
    """No OCR engine can run: the package, its model files or the language pack is missing."""


class OcrEngine(Protocol):
    name: str

    def available(self, language: str) -> bool: ...

    def recognize(self, png: bytes) -> str: ...


def _rapid_importable() -> bool:
    """Whether the optional ``rapidocr`` package imports in this environment."""
    try:
        import rapidocr  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install (a missing DLL, say) counts as absent
        return False
    return True


def select_engine(language: str) -> OcrEngine | None:
    """Windows OCR when it has ``language``, else rapidocr when installed with its models, else None."""
    from emailsupersearch.ingest.ocr.windows_ocr import WindowsOcr

    try:
        windows = WindowsOcr(language)
        if windows.available(language):
            return windows
    except Exception as e:  # noqa: BLE001 - choosing an engine must never fail the caller
        log.warning("Windows OCR could not be checked: %s", type(e).__name__)
    if not _rapid_importable():
        return None
    from emailsupersearch.ingest.ocr.rapid_ocr import RapidOcr

    try:
        return RapidOcr()
    except OcrUnavailable:
        log.info("rapidocr is installed but its model files are missing")
    except Exception as e:  # noqa: BLE001 - see above
        log.warning("rapidocr could not be started: %s", type(e).__name__)
    return None
