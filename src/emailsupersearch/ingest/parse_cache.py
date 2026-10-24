"""The parsed-text cache, shared by every project and keyed by the file's SHA-256.

One JSON file per mail file, ``<cache_dir>/<sha256>.json`` (the flat layout of spec 6.2). Two projects
watching the same folder therefore parse each file once, and a re-index reads the cache instead of
re-parsing. Attachment bytes are never stored: the OCR step re-parses the mail file when it needs them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from emailsupersearch.types import (
    AttachmentText,
    ParsedAttachment,
    ParsedDocument,
    ParsedMessage,
    Recipient,
)

_READ_BLOCK_BYTES = 1024 * 1024
_REPLACE_ATTEMPTS = 10  # a reader holding the entry open makes the rename fail with PermissionError on Windows
_REPLACE_RETRY_S = 0.02

# Per class, the fields that hold other dataclasses: (their class, whether the field is a list of them).
_CHILDREN: dict[type, dict[str, tuple[type, bool]]] = {
    ParsedDocument: {"message": (ParsedMessage, False), "attachments": (AttachmentText, True)},
    ParsedMessage: {"recipients": (Recipient, True), "attachments": (ParsedAttachment, True)},
    ParsedAttachment: {"nested": (ParsedMessage, False)},
}


def sha256_file(path: Path) -> str:
    """The hex SHA-256 of a file's bytes, read in blocks so a huge file is never held in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(_READ_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def append_ocr_text(existing: str, ocr_text: str) -> str:
    """An attachment's text once OCR has read its scanned pages: the text layer, a blank line, then the OCR text.

    OCR reads only the pages that had no text layer, so the two are complementary and both are kept.
    """
    return f"{existing}\n\n{ocr_text}" if existing else ocr_text


def _to_json_value(message: dict[str, Any]) -> None:
    """Make an ``asdict`` message JSON-safe in place: no attachment bytes, the conversation index as hex."""
    if message["conversation_index"] is not None:
        message["conversation_index"] = message["conversation_index"].hex()
    for attachment in message["attachments"]:
        attachment["data"] = None
        if attachment["nested"] is not None:
            _to_json_value(attachment["nested"])


def _to_dataclass(cls: type, data: dict[str, Any]) -> Any:
    """Rebuild a dataclass (and the dataclasses inside it) from the dict ``put`` stored."""
    fields = dict(data)
    for name, (child, many) in _CHILDREN.get(cls, {}).items():
        value = fields[name]
        if value is not None:
            fields[name] = [_to_dataclass(child, v) for v in value] if many else _to_dataclass(child, value)
    if cls is ParsedMessage and fields["conversation_index"] is not None:
        fields["conversation_index"] = bytes.fromhex(fields["conversation_index"])
    return cls(**fields)


class ParseCache:
    """Parsed documents on disk, looked up by the SHA-256 of the mail file they came from."""

    def __init__(self, cache_dir: Path) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        return self._dir / f"{sha256}.json"

    def get(self, sha256: str) -> ParsedDocument | None:
        """The cached document, or None when there is no entry or it cannot be read (it is then re-parsed)."""
        try:
            return _to_dataclass(ParsedDocument, json.loads(self._path(sha256).read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, doc: ParsedDocument) -> None:
        """Store a document. The file is written whole under a temporary name and renamed into place,
        so a reader never sees half a file.

        On Windows the rename fails while a reader has the entry open, so it is retried. If it keeps failing and
        the entry already exists, that is left as it is: the entry is named by the hash of the mail file, so a
        document parsed from the same file is interchangeable with it.
        """
        self._store(doc, keep_existing_on_conflict=True)

    def _store(self, doc: ParsedDocument, *, keep_existing_on_conflict: bool) -> None:
        data = dataclasses.asdict(doc)
        _to_json_value(data["message"])
        target = self._path(doc.sha256)
        # Unique per writer: two projects can cache the same file at the same moment.
        tmp = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            for attempt in range(1, _REPLACE_ATTEMPTS + 1):
                try:
                    os.replace(tmp, target)
                    return
                except PermissionError:
                    if attempt == _REPLACE_ATTEMPTS:
                        if keep_existing_on_conflict and target.exists():
                            return
                        raise
                    time.sleep(_REPLACE_RETRY_S)
        finally:
            tmp.unlink(missing_ok=True)

    def set_ocr_text(self, sha256: str, ordinal: int, text: str) -> None:
        """Record the OCR result for one attachment: the OCR text appended to its text (``append_ocr_text``),
        status ``ok`` and no pages left to read.

        Raises ``KeyError`` when there is no entry for ``sha256`` or it has no such attachment.
        """
        doc = self.get(sha256)
        if doc is None:
            raise KeyError(sha256)
        for attachment in doc.attachments:
            if attachment.ordinal == ordinal:
                attachment.text = append_ocr_text(attachment.text, text)
                attachment.status, attachment.needs_ocr_pages = "ok", []
                break
        else:
            raise KeyError(f"{sha256}: no attachment {ordinal}")
        self._store(doc, keep_existing_on_conflict=False)  # new content: it must replace the entry
