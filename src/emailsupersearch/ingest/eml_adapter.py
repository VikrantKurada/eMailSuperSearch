"""Parse an ``.eml`` file (RFC 5322 / MIME) into a ``ParsedMessage``.

New Outlook saves dragged mail as ``.eml``. Only the stdlib ``email`` package is used.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import re
from datetime import UTC
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from emailsupersearch.ingest.attachments import kind_for
from emailsupersearch.types import ParsedAttachment, ParsedMessage, Recipient

_WHITESPACE = re.compile(r"\s+")
_CONVERSATION_INDEX_MIN_BYTES = 22  # the header block of an Outlook conversation index: time and thread GUID
# fmt: off
_BLOCK_TAGS = frozenset([
    "address", "blockquote", "br", "dd", "div", "dt", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "li", "ol", "p", "pre", "table", "tr", "ul",
])
# fmt: on
_CELL_TAGS = frozenset(
    ["td", "th"]
)  # a space on both sides, so "<td>Name</td><td>Value</td>" stays two words


def _strip_brackets(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == "<" and token[-1] == ">":
        token = token[1:-1].strip()
    return token


def _raw_header(msg: EmailMessage, name: str) -> str | None:
    """The first ``name`` header exactly as stored, folding included (the parsed value can lose text)."""
    wanted = name.lower()
    return next((str(value) for key, value in msg.raw_items() if key.lower() == wanted), None)


def _ids(msg: EmailMessage, name: str) -> list[str]:
    """Ids in a Message-ID, In-Reply-To, References or Content-ID header, without brackets or whitespace.

    Folding can put a line break inside one id, so whitespace goes before the brackets do.
    """
    raw = _raw_header(msg, name) or ""
    tokens = re.findall(r"<[^<>]*>", raw) or raw.split()
    return [t for t in (_strip_brackets(_WHITESPACE.sub("", raw_id)) for raw_id in tokens) if t]


class _TextExtractor(HTMLParser):
    """Collect the visible text of an HTML document: block tags become line breaks, table cells spaces."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _CELL_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _CELL_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML document: block tags become line breaks, table cells spaces."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _text_of(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except LookupError:  # a charset Python does not know: keep the text rather than lose the message
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def _conversation_index(msg: EmailMessage) -> bytes | None:
    """The ``Thread-Index`` header (Outlook's conversation index, base64) as bytes; None when it is absent, not
    base64, or shorter than the 22-byte header that carries the thread GUID."""
    raw = _raw_header(msg, "Thread-Index")
    if raw is None:
        return None
    try:
        value = base64.b64decode(_WHITESPACE.sub("", raw), validate=True)
    except (binascii.Error, ValueError):
        return None
    return value if len(value) >= _CONVERSATION_INDEX_MIN_BYTES else None


def _sent_ts(msg: EmailMessage) -> int | None:
    header = msg["Date"]
    if header is None:
        return None
    try:
        when = parsedate_to_datetime(str(header))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return int(when.timestamp())


def _recipients(msg: EmailMessage) -> list[Recipient]:
    found: list[Recipient] = []
    for kind in ("to", "cc", "bcc"):
        values = [str(v) for v in msg.get_all(kind, [])]
        found.extend(Recipient(kind, name, addr) for name, addr in getaddresses(values) if addr)
    return found


def _attachment_bytes(part: EmailMessage) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else part.as_bytes()


def _leaves(part: EmailMessage):
    """Every non-multipart descendant of ``part`` in document order, at any nesting depth.

    A ``message/rfc822`` part is a leaf: its own parts belong to the message inside it.
    """
    if part.get_content_type() == "message/rfc822" or part.get_content_maintype() != "multipart":
        yield part
        return
    for sub in part.iter_parts():
        yield from _leaves(sub)


def _is_attachment(part: EmailMessage) -> bool:
    return (
        part.is_attachment()
        or part["Content-ID"] is not None
        or bool(part.get_filename())
        or part.get_content_maintype() != "text"
    )


def _attachments(
    msg: EmailMessage, mtime: int, bodies: tuple[EmailMessage | None, ...]
) -> list[ParsedAttachment]:
    """Attachments and inline parts at any depth: HTML mail nests its images under alternative > related."""
    found: list[ParsedAttachment] = []
    for part in _leaves(msg):
        if any(part is body for body in bodies):
            continue
        mime = part.get_content_type()
        if mime != "message/rfc822" and not _is_attachment(part):
            continue
        ordinal = len(found)
        filename = part.get_filename() or f"part-{ordinal}"
        nested = None
        if mime == "message/rfc822":
            inner = part.get_content()
            data, nested, kind = inner.as_bytes(), _convert(inner, mtime), "eml"
        else:
            data = _attachment_bytes(part)
            kind = kind_for(filename, mime)
        found.append(
            ParsedAttachment(
                ordinal,
                filename,
                mime,
                len(data),
                kind,
                data=data,
                nested=nested,
                content_id=next(iter(_ids(part, "Content-ID")), None),
            )
        )
    return found


def _convert(msg: EmailMessage, mtime: int) -> ParsedMessage:
    sender_name, sender_addr = parseaddr(str(msg["From"] or ""))
    sent_ts = _sent_ts(msg)
    plain = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    body_html = _text_of(html_part) if html_part is not None else None
    if plain is not None:
        body_text = _text_of(plain)
    else:
        body_text = html_to_text(body_html) if body_html else ""
    return ParsedMessage(
        message_id=next(iter(_ids(msg, "Message-ID")), None),
        subject=str(msg["Subject"] or ""),
        sender_name=sender_name,
        sender_addr=sender_addr,
        recipients=_recipients(msg),
        sent_ts=mtime if sent_ts is None else sent_ts,
        received_ts=None,
        date_source="mtime" if sent_ts is None else "header_date",
        body_text=body_text,
        body_html=body_html,
        in_reply_to=next(iter(_ids(msg, "In-Reply-To")), None),
        references=_ids(msg, "References"),
        conversation_index=_conversation_index(msg),
        conversation_topic=None,
        attachments=_attachments(msg, mtime, (plain, html_part)),
    )


def parse_eml(path: Path) -> ParsedMessage:
    """Parse one ``.eml`` file; a missing or unreadable ``Date`` falls back to the file's mtime."""
    with open(path, "rb") as fp:
        msg = email.message_from_binary_file(fp, policy=email.policy.default)
    return _convert(msg, int(Path(path).stat().st_mtime))
