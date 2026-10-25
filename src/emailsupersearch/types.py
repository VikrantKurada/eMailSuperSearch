"""Dataclasses shared by every parser, indexer and search component."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AttachmentKind = Literal["pdf", "docx", "xlsx", "pptx", "image", "msg", "eml", "other"]
# "skipped": not content at all (an inline logo or signature image), so it is neither indexed nor reported missing.
AttachmentStatus = Literal["ok", "needs_ocr", "unsupported", "failed", "partial", "skipped"]


@dataclass(frozen=True)
class Person:
    name: str
    addr: str


@dataclass(frozen=True)
class Recipient:
    kind: Literal["to", "cc", "bcc"]
    name: str
    addr: str


@dataclass
class ParsedAttachment:
    ordinal: int
    filename: str
    mime: str
    size: int
    kind: AttachmentKind
    data: bytes | None = None
    nested: ParsedMessage | None = None
    content_id: str | None = None


@dataclass
class ParsedMessage:
    message_id: str | None
    subject: str
    sender_name: str
    sender_addr: str
    recipients: list[Recipient]
    sent_ts: int | None
    received_ts: int | None
    date_source: str
    body_text: str
    body_html: str | None
    in_reply_to: str | None
    references: list[str]
    conversation_index: bytes | None
    conversation_topic: str | None
    attachments: list[ParsedAttachment]


@dataclass
class AttachmentText:
    ordinal: int
    filename: str
    kind: AttachmentKind
    size: int
    status: AttachmentStatus
    text: str
    page_count: int
    needs_ocr_pages: list[int]
    sha256: str | None = None
    reason: str = ""


@dataclass
class ParsedDocument:
    sha256: str
    message: ParsedMessage
    attachments: list[AttachmentText]


@dataclass
class SearchFilters:
    query: str = ""
    from_addrs: list[str] = field(default_factory=list)
    recipient_addrs: list[str] = field(default_factory=list)
    date_from: int | None = None
    date_to: int | None = None
    attachment_kinds: list[str] = field(default_factory=list)
    has_attachments: bool | None = None
    group_by_chain: bool = True
    only_with_conclusion: bool = False
    sort: Literal["relevance", "date"] = "relevance"
