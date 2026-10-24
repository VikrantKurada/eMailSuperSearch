"""OCR through Windows.Media.Ocr, the engine built into Windows 10 and 11 (the ``winrt-*`` wheels).

It reads the languages whose Windows language pack is installed; nothing is downloaded. winrt is imported inside
the functions, so importing this module costs nothing where the wheels are absent.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from PIL import Image

from emailsupersearch.ingest.ocr.base import OcrUnavailable

MIN_IMAGE_SIDE = 50  # the engine reads nothing from very small images, so they are scaled up to this


def _fit(width: int, height: int, max_side: int) -> tuple[int, int]:
    """The size to hand the engine: no side over ``max_side`` and none under `MIN_IMAGE_SIDE`."""
    scale = min(1.0, max_side / max(width, height))
    if min(width, height) * scale < MIN_IMAGE_SIDE:
        scale = MIN_IMAGE_SIDE / min(width, height)
    return (
        max(MIN_IMAGE_SIDE, min(max_side, round(width * scale))),
        max(MIN_IMAGE_SIDE, min(max_side, round(height * scale))),
    )


def _tag_matches(tag: str, language: str) -> bool:
    """Whether the engine's tag (``en-GB``) is ``language`` itself or a regional variant of it (``en``)."""
    tag, language = tag.lower(), language.lower()
    return tag == language or tag.startswith(language + "-")


def _create_engine(language: str) -> Any | None:
    """A Windows OCR engine for ``language``, or None when winrt or the language pack is missing."""
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
    except (ImportError, OSError):
        return None
    try:
        engine = OcrEngine.try_create_from_language(Language(language))
    except OSError:  # a malformed language tag
        engine = None
    if engine is not None:
        return engine
    try:
        engine = OcrEngine.try_create_from_user_profile_languages()
    except OSError:
        return None
    if engine is not None and _tag_matches(engine.recognizer_language.language_tag, language):
        return engine
    return None


async def _recognize(engine: Any, image_bytes: bytes) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(image_bytes)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines)


class WindowsOcr:
    """Windows OCR for one language (``recognize``); ``available`` checks any language."""

    name = "windows"

    def __init__(self, language: str = "en") -> None:
        self.language = language
        self._engines: dict[str, Any | None] = {}

    def _engine(self, language: str) -> Any | None:
        if language not in self._engines:
            self._engines[language] = _create_engine(language)
        return self._engines[language]

    def available(self, language: str) -> bool:
        """Whether Windows has an OCR language pack for ``language`` (a BCP-47 tag such as ``en``)."""
        return self._engine(language) is not None

    def recognize(self, png: bytes) -> str:
        """The text of an image, one recognised line per line; raises ``OcrUnavailable`` without the language pack."""
        engine = self._engine(self.language)
        if engine is None:
            raise OcrUnavailable(f"no Windows OCR language pack for {self.language!r}")
        from winrt.windows.media.ocr import OcrEngine

        with Image.open(io.BytesIO(png)) as opened:
            image = opened.convert("RGB")
        size = _fit(image.width, image.height, OcrEngine.max_image_dimension)
        if size != image.size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, "BMP")  # uncompressed: much faster to write and decode than PNG for a full page
        return asyncio.run(_recognize(engine, buf.getvalue()))
