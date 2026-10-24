"""Message identity: an email is never identified by its file path.

Outlook names exported files by subject and re-exports are byte-different, so the
key comes from the Message-ID header, with a metadata hash as the fallback.
"""

from __future__ import annotations

import hashlib
import re

from emailsupersearch.types import ParsedMessage

_REPLY_PREFIX = re.compile(r"\s*(?:re|fwd|fw|aw|wg)(?:\[\d+\])?\s*:", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def _strip_angle_brackets(s: str) -> str:
    """Strip surrounding whitespace and a single pair of angle brackets."""
    s = s.strip()
    if len(s) >= 2 and s[0] == "<" and s[-1] == ">":
        s = s[1:-1].strip()
    return s


def normalize_addr(s: str) -> str:
    """Lower-case an address and strip whitespace and angle brackets.

    An X.500 address (``/O=.../CN=...``) is kept whole, lower-cased.
    """
    lowered = s.strip().lower()
    if lowered.startswith("/o="):
        return lowered
    return _strip_angle_brackets(lowered)


def normalize_subject(s: str) -> str:
    """Drop repeated reply/forward prefixes, collapse whitespace and lower-case."""
    s = s.strip()
    while True:
        match = _REPLY_PREFIX.match(s)
        if match is None:
            break
        s = s[match.end():]
    return _WHITESPACE.sub(" ", s).strip().lower()


def message_key(m: ParsedMessage) -> str:
    """Stable identity of a message: its Message-ID, else a hash of its metadata."""
    if m.message_id:
        key = _strip_angle_brackets(m.message_id)
        if key:
            return key
    material = f"{normalize_addr(m.sender_addr)}|{m.sent_ts}|{normalize_subject(m.subject)}|{m.body_text[:2048]}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
