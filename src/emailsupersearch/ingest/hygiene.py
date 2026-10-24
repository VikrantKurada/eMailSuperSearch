"""Content hygiene: the text of a message that is actually new.

Every reply in a chain quotes the earlier messages and every message ends with a
signature. Without stripping them the index would embed the first message once per
reply and every email would "mention" the company's phone number. ``new_text`` is
what gets chunked, embedded and sent to NER; the full body is kept for display.

Bodies arriving here are already LF-normalised by the adapters.
"""

from __future__ import annotations

import re

_FLAGS = re.MULTILINE | re.IGNORECASE

# Quoted-reply markers. Each match starts at a line start, and the body is cut there.
_QUOTE_MARKERS = (
    # "On Tue, Mar 3 Priya wrote:"
    re.compile(r"^On .+ wrote:[ \t]*$", _FLAGS),
    # Outlook header block: a From: line with a Sent: or To: line within the next 4 lines.
    re.compile(r"^From: .+(?:\n.*){0,3}\n(?:Sent: |To: )", _FLAGS),
    re.compile(r"^-----[ \t]*Original Message[ \t]*-----", _FLAGS),
    # A run of one or more lines starting with ">" begins at its first line.
    re.compile(r"^>", _FLAGS),
    # Outlook's "________________________________" separator.
    re.compile(r"^_{20,}[ \t]*$", _FLAGS),
)

# Signature markers: the body is cut at the first of them.
_SIGNATURE_MARKERS = (
    re.compile(r"^-- $", _FLAGS),
    re.compile(r"^(?:Kind|Best|Warm) regards,?[ \t]*$", _FLAGS),
    re.compile(r"^Thanks,?[ \t]*$", _FLAGS),
    re.compile(r"^Sent from my ", _FLAGS),
)

_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_SIGNATURE_MAX_LINES = 6
_SIGNATURE_MAX_LINE_LENGTH = 60  # a signature line is shorter than this

_HORIZONTAL_SPACE_RUN = re.compile(r"[ \t]+")
_NEWLINE_RUN = re.compile(r"\n{3,}")


def _cut_at_earliest(body: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    """Return ``body`` up to the earliest match of any pattern, or None if none match."""
    starts = [m.start() for p in patterns if (m := p.search(body))]
    return body[: min(starts)] if starts else None


def strip_quoted(body: str) -> str:
    """Remove the quoted earlier messages: everything from the first reply marker on."""
    cut = _cut_at_earliest(body, _QUOTE_MARKERS)
    return body if cut is None else cut


def _strip_trailing_phone_block(body: str) -> str:
    """Remove a trailing block of at most 6 short non-empty lines that has a phone number.

    The block is the whole run of short lines at the end of the body, bounded above by
    a blank line or a long line. A longer run is body text, not a signature, so it stays.
    """
    lines = body.split("\n")
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while (
        start > 0
        and lines[start - 1].strip()
        and len(lines[start - 1].strip()) < _SIGNATURE_MAX_LINE_LENGTH
    ):
        start -= 1
    block = lines[start:end]
    if 0 < len(block) <= _SIGNATURE_MAX_LINES and any(_PHONE.search(line) for line in block):
        return "\n".join(lines[:start]) + "\n" if start else ""
    return body


def strip_signature(body: str) -> str:
    """Remove the signature: from the first signature marker on, else a trailing phone block."""
    cut = _cut_at_earliest(body, _SIGNATURE_MARKERS)
    return _strip_trailing_phone_block(body) if cut is None else cut


def _collapse_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs to one space and 3+ newlines to 2, then strip."""
    text = _HORIZONTAL_SPACE_RUN.sub(" ", text)
    return _NEWLINE_RUN.sub("\n\n", text).strip()


def new_text(body: str, has_parent: bool) -> str:
    """The new content of a message: no quoted history (when it has a parent), no signature.

    Quote stripping applies only when the message has a parent in the thread; without
    one, the quoted text is the only copy of that content in the index. The signature is
    always stripped. Never empty when the body had text: if stripping removes everything,
    the whitespace-collapsed original body is returned.
    """
    text = strip_quoted(body) if has_parent else body
    result = _collapse_whitespace(strip_signature(text))
    return result or _collapse_whitespace(body)
