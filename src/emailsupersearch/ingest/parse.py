"""Dispatch a mail file to the adapter for its suffix; every failure surfaces as ``ParseError``."""

from __future__ import annotations

from pathlib import Path

from emailsupersearch.ingest.eml_adapter import parse_eml
from emailsupersearch.ingest.msg_adapter import parse_msg
from emailsupersearch.types import ParsedMessage


class ParseError(Exception):
    """A file that cannot be parsed; ``reason`` is the text the pipeline records for it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_file(path: Path) -> ParsedMessage:
    """Parse a ``.msg`` or ``.eml`` file; any other suffix, and any adapter exception, raises ``ParseError``."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".msg":
        adapter = parse_msg
    elif suffix == ".eml":
        adapter = parse_eml
    else:
        raise ParseError(f"unsupported file type: {suffix}")
    try:
        return adapter(path)
    except Exception as e:
        raise ParseError(f"{type(e).__name__}: {e}") from e
