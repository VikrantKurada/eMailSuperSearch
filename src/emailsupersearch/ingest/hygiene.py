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

# "On Tue, Mar 3 Priya wrote:"
_ON_WROTE = re.compile(r"^On .+ wrote:[ \t]*$", _FLAGS)

# Quoted-reply markers. Each match starts at a line start, and the body is cut there.
_QUOTE_MARKERS = (
    _ON_WROTE,
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
_PHONE_MIN_DIGITS = 9
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SIGNATURE_MAX_LINES = 6
_SIGNATURE_MAX_LINE_LENGTH = 60  # a signature line is shorter than this

_MARKERS = _QUOTE_MARKERS + _SIGNATURE_MARKERS
_QUOTED_RUN = re.compile(r"^>.*(?:\n>.*)*", re.MULTILINE)
_NON_BLANK_LINE = re.compile(r"^[ \t]*\S.*$", re.MULTILINE)

_HORIZONTAL_SPACE_RUN = re.compile(r"[ \t]+")
_NEWLINE_RUN = re.compile(r"\n{3,}")


def _cut_at_earliest(body: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    """Return ``body`` up to the earliest match of any pattern, or None if none match."""
    starts = [m.start() for p in patterns if (m := p.search(body))]
    return body[: min(starts)] if starts else None


def _is_interleaved(body: str) -> bool:
    """True if the author wrote between the quotes, not only above or below them.

    That is: after the first run of ``>`` lines there is a non-blank line that is not
    quoted. Scanning stops at a signature or quote marker line, because what follows
    it is a signature or older history, not an answer.
    """
    first_run = _QUOTED_RUN.search(body)
    if first_run is None:
        return False
    for line in _NON_BLANK_LINE.finditer(body, first_run.end()):
        if line.group().startswith(">"):
            continue
        return not any(p.match(body, line.start()) for p in _MARKERS)
    return False


def strip_quoted(body: str) -> str:
    """Remove the quoted earlier messages.

    Normally everything from the first reply marker on goes. When the replies are
    interleaved with the quotes, only the ``>`` lines and the ``On ... wrote:`` header
    lines go, so the answers survive.
    """
    if _is_interleaved(body):
        lines = body.split("\n")
        return "\n".join(ln for ln in lines if not ln.startswith(">") and not _ON_WROTE.match(ln))
    cut = _cut_at_earliest(body, _QUOTE_MARKERS)
    return body if cut is None else cut


def _has_phone_number(line: str) -> bool:
    """True if the line holds a grouped phone number and no ISO date.

    A phone number is a ``_PHONE`` match of at least 9 digits with a separator between
    digits, so reference numbers (``4500123456``) and dates (``2026-10-24 12:00``) are not.
    """
    if _ISO_DATE.search(line):
        return False
    for match in _PHONE.finditer(line):
        run = match.group()
        digits = sum(ch.isdigit() for ch in run)
        grouped = any(not ch.isdigit() and ch != "+" for ch in run)
        if digits >= _PHONE_MIN_DIGITS and grouped:
            return True
    return False


def _strip_trailing_phone_block(body: str) -> str:
    """Remove the last paragraph when it looks like a signature block.

    A signature is separated from the text by a blank line, so only the run of lines after
    the last blank line is considered, and only when a blank line precedes it. It goes when
    it has at most 6 lines, each under 60 characters, and at least one holds a phone number.
    """
    lines = body.split("\n")
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start > 0 and lines[start - 1].strip():
        start -= 1
    if start == 0:
        return body  # no blank line above the last paragraph: nothing separates a signature
    block = lines[start:end]
    if (
        0 < len(block) <= _SIGNATURE_MAX_LINES
        and all(len(line.strip()) < _SIGNATURE_MAX_LINE_LENGTH for line in block)
        and any(_has_phone_number(line) for line in block)
    ):
        return "\n".join(lines[:start]) + "\n"
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
    always stripped. Never empty when the body had text: if stripping both leaves nothing
    (a bare "Thanks," above the quoted chain), the quote-stripped text is used, and if that
    is empty too (a body that is only a quote), the whitespace-collapsed original body.
    """
    unquoted = strip_quoted(body) if has_parent else body
    return (
        _collapse_whitespace(strip_signature(unquoted))
        or _collapse_whitespace(unquoted)
        or _collapse_whitespace(body)
    )
