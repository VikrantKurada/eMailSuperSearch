"""Parse an Outlook ``.msg`` file into a ``ParsedMessage`` using extract-msg (GPL-3, 0.56.x).

``parse_msg`` opens the file and hands the open message to ``_convert``, which is also what turns an
embedded ``.msg`` attachment into ``ParsedAttachment.nested`` (up to ``_MAX_NESTING`` levels deep).
"""

from __future__ import annotations

import codecs
import mimetypes
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import extract_msg
from extract_msg.enums import AttachmentType, RecipientType
from extract_msg.msg_classes import MessageBase
from extract_msg.null_date import NullDate

from emailsupersearch.ingest.eml_adapter import _kind_for
from emailsupersearch.types import ParsedAttachment, ParsedMessage, Recipient

MSG_CONVERSATION_INDEX_STREAM = "__substg1.0_00710102"
MSG_CONVERSATION_TOPIC_STREAM = "__substg1.0_0070"
MSG_ATTACH_CONTENT_ID_STREAM = "__substg1.0_3712"

_MAX_NESTING = 3  # a message nested deeper than this keeps its attachment entry but gets no ``nested``
_PR_INTERNET_CPID = "3FDE0003"
_PR_ATTACH_SIZE = "0E200003"
_RECIPIENT_KINDS = {RecipientType.TO: "to", RecipientType.CC: "cc", RecipientType.BCC: "bcc"}
_WHITESPACE = re.compile(r"\s+")
# ISO 8859-n and a few more code pages that Python names differently from ``cp<number>``.
_CODEPAGE_CODECS = {28590 + n: f"iso8859-{n}" for n in range(1, 10)} | {28605: "iso8859-15", 20127: "ascii"}


def _clean(value: str | None) -> str:
    """A string stream value without the NUL padding some writers leave behind."""
    return (value or "").replace("\x00", "")


def _clean_id(value: str | None) -> str | None:
    """A Message-ID, In-Reply-To or Content-ID without whitespace and one pair of angle brackets."""
    token = _WHITESPACE.sub("", _clean(value))
    if len(token) >= 2 and token[0] == "<" and token[-1] == ">":
        token = token[1:-1].strip()
    return token or None


def _ts(when: object) -> int | None:
    """Epoch seconds of a datetime; extract-msg's null dates and anything unrepresentable give ``None``."""
    if not isinstance(when, datetime) or isinstance(when, NullDate):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    try:
        return int(when.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _header_ts(msg: MessageBase) -> int | None:
    value = msg.header["Date"]
    if value is None:
        return None
    try:
        return _ts(parsedate_to_datetime(str(value)))
    except (TypeError, ValueError):
        return None


def _optional[T](read: Callable[[], T]) -> T | None:
    """``read()``, or ``None`` when extract-msg cannot produce the value; a bad string encoding still raises."""
    try:
        return read()
    except UnicodeDecodeError:
        raise  # the caller retries with a detected encoding
    except Exception:  # noqa: BLE001 - extract-msg and RTFDE raise assorted types on damaged bodies
        return None


def _sender(msg: MessageBase) -> tuple[str, str]:
    raw = _clean(msg.sender).strip()
    name, addr = parseaddr(raw)
    if "@" in addr:
        return name, addr
    return (name if "<" in raw else raw), ""  # a bare display name or an Exchange alias is not an address


def _references(msg: MessageBase) -> list[str]:
    raw = " ".join(_clean(str(v)) for v in msg.header.get_all("References") or [])
    tokens = re.findall(r"<[^<>]*>", raw) or raw.split()  # folding can put a line break inside one id
    return [i for i in (_clean_id(t) for t in tokens) if i]


def _recipients(msg: MessageBase) -> list[Recipient]:
    found: list[Recipient] = []
    for rcpt in msg.recipients:
        kind = _RECIPIENT_KINDS.get(rcpt.type)
        name, addr = _clean(rcpt.name).strip(), _clean(rcpt.email).strip()
        if kind is not None and (name or addr):
            found.append(Recipient(kind, name, addr))
    return found


def _rtf_codec(rtf: bytes) -> str:
    match = re.search(rb"\\ansicpg(\d+)", rtf[:2048])
    name = _CODEPAGE_CODECS.get(int(match.group(1)), f"cp{match.group(1).decode()}") if match else "cp1252"
    try:
        codecs.lookup(name)
    except LookupError:
        return "cp1252"
    return name


_RTF_TOKEN = re.compile(rb"\\'([0-9a-fA-F]{2})|\\([a-zA-Z]+)(-?\d+)? ?|\\([^a-zA-Z])|([{}])|([^\\{}]+)")
_RTF_BREAKS = {b"par": "\n", b"line": "\n", b"sect": "\n", b"page": "\n", b"tab": "\t"}
_RTF_SYMBOLS = {b"~": " ", b"\\": "\\", b"{": "{", b"}": "}"}
# Destinations whose content is not body text (``\*`` marks any further ignorable destination).
_RTF_SKIPPED = frozenset(
    [b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"header", b"footer", b"footnote"]
    + [b"listtable", b"listoverridetable", b"rsidtbl", b"themedata", b"colorschememapping"]
    + [b"latentstyles", b"datastore", b"generator", b"fldinst", b"object"]
)


def _strip_rtf(rtf: bytes) -> str:
    """Visible text of an RTF document: control words and ignorable groups removed, ``\\'hh``/``\\uN`` decoded."""
    codec = _rtf_codec(rtf)
    out: list[str] = []
    depth = 0
    skip_from: int | None = None  # group depth at which an ignorable destination started
    group_start = False
    unicode_skip = 0  # characters that stand in for the last \uN and must not be emitted
    for match in _RTF_TOKEN.finditer(rtf):
        hexcode, word, number, symbol, brace, text = match.groups()
        if brace:
            if brace == b"{":
                depth += 1
                group_start = True
            else:
                if skip_from == depth:
                    skip_from = None
                depth -= 1
                group_start = False
            continue
        starts_group, group_start = group_start, False
        if skip_from is not None:
            continue
        if starts_group and (symbol == b"*" or word in _RTF_SKIPPED):
            skip_from = depth
        elif hexcode:
            if unicode_skip:
                unicode_skip -= 1
            else:
                out.append(bytes([int(hexcode, 16)]).decode(codec, errors="replace"))
        elif word == b"u" and number:
            out.append(chr(int(number) % 65536))
            unicode_skip = 1
        elif word is not None:
            out.append(_RTF_BREAKS.get(word, ""))
        elif symbol is not None:
            out.append(_RTF_SYMBOLS.get(symbol, ""))
        elif text:
            visible = text.replace(b"\r", b"").replace(b"\n", b"")
            if unicode_skip:
                visible, unicode_skip = visible[unicode_skip:], max(0, unicode_skip - len(visible))
            out.append(visible.decode(codec, errors="replace"))
    text = "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")  # pairs up \u surrogates
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _body_text(msg: MessageBase) -> str:
    """The plain-text body; a message that only has an RTF body gets its text from that."""
    body = _clean(_optional(lambda: msg.body))  # None when extract-msg could not de-encapsulate the RTF
    if body.strip():
        return body
    rtf = _optional(lambda: msg.rtfBody)
    return _clean(_strip_rtf(rtf)) if rtf else body


def _decode_html(raw: bytes, codepage: object) -> str:
    """HTML bytes as text: UTF-8 when valid, else the message's declared code page, else cp1252."""
    try:
        return _clean(raw.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    if isinstance(codepage, int):
        try:
            return _clean(raw.decode(_CODEPAGE_CODECS.get(codepage, f"cp{codepage}")))
        except (LookupError, UnicodeDecodeError):
            pass
    return _clean(raw.decode("cp1252", errors="replace"))


def _body_html(msg: MessageBase) -> str | None:
    raw = _optional(lambda: msg.htmlBody)
    if not raw:
        return None
    return _decode_html(raw, msg.getPropertyVal(_PR_INTERNET_CPID))


def _attachments(msg: MessageBase, depth: int) -> list[ParsedAttachment]:
    found: list[ParsedAttachment] = []
    for ordinal, att in enumerate(msg.attachments):
        filename = _clean(att.longFilename or att.shortFilename or att.name) or f"attachment-{ordinal}"
        mime = _clean(att.mimetype) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        content_id = _clean_id(att.getStringStream(MSG_ATTACH_CONTENT_ID_STREAM))
        data: bytes | None = None
        nested: ParsedMessage | None = None
        if att.type == AttachmentType.DATA:
            data = att.data if isinstance(att.data, bytes) else None
            kind = _kind_for(filename, mime)  # Task 9 replaces this with the shared kind_for
        elif att.type == AttachmentType.MSG:
            kind = "msg"
            if depth < _MAX_NESTING:
                nested = _convert(att.data, depth + 1)
        else:
            kind = "other"
        size = len(data) if data is not None else int(att.getPropertyVal(_PR_ATTACH_SIZE, 0) or 0)
        found.append(ParsedAttachment(ordinal, filename, mime, size, kind, data, nested, content_id))
    return found


def _convert(msg: MessageBase, depth: int) -> ParsedMessage:
    """Turn an open message into a ``ParsedMessage``.

    ``sent_ts`` is left ``None`` with ``date_source="none"`` when the message carries no date at all;
    ``parse_msg`` then substitutes the file's mtime for the top-level message.
    """
    sender_name, sender_addr = _sender(msg)
    received_ts = _ts(msg.receivedTime)
    sent_ts, date_source = _ts(msg.date), "sent"
    if sent_ts is None:
        sent_ts, date_source = received_ts, "received"
    if sent_ts is None:
        sent_ts, date_source = _header_ts(msg), "header_date"
    if sent_ts is None:
        date_source = "none"
    return ParsedMessage(
        message_id=_clean_id(msg.messageId),
        subject=_clean(msg.subject),
        sender_name=sender_name,
        sender_addr=sender_addr,
        recipients=_recipients(msg),
        sent_ts=sent_ts,
        received_ts=received_ts,
        date_source=date_source,
        body_text=_body_text(msg),
        body_html=_body_html(msg),
        in_reply_to=_clean_id(msg.inReplyTo),
        references=_references(msg),
        conversation_index=msg.getStream(MSG_CONVERSATION_INDEX_STREAM),
        conversation_topic=_clean(msg.getStringStream(MSG_CONVERSATION_TOPIC_STREAM)).strip() or None,
        attachments=_attachments(msg, depth),
    )


def _open_and_convert(path: Path, **options: str) -> ParsedMessage:
    with extract_msg.openMsg(str(path), delayAttachments=True, **options) as msg:
        return _convert(msg, 0)


def parse_msg(path: Path) -> ParsedMessage:
    """Parse one ``.msg`` file; a message with no date at all gets the file's mtime as ``sent_ts``."""
    path = Path(path)
    try:
        message = _open_and_convert(path)
    except UnicodeDecodeError:  # strings in a code page the file does not declare correctly
        message = _open_and_convert(path, overrideEncoding="chardet")
    if message.sent_ts is None:
        message.sent_ts, message.date_source = int(path.stat().st_mtime), "mtime"
    return message
