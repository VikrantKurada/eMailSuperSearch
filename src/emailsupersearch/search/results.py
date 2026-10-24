"""The cards a search returns: what the routes serialise and the UI renders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from emailsupersearch.types import Person


@dataclass
class AttachmentInfo:
    """One attachment of a message as a card shows it; the statuses are the ``attachments`` row's."""

    ordinal: int
    filename: str
    kind: str
    size: int | None
    extract_status: str | None
    ocr_status: str | None
    embedded_partial: bool
    page_count: int | None


@dataclass
class MessageCard:
    """One live message that matched: ``snippet`` is its best chunk's text, ``paths`` its live files."""

    message_key: str
    subject: str
    sent_ts: int | None
    sender: Person
    to: list[Person]
    cc: list[Person]
    snippet: str
    attachments: list[AttachmentInfo]
    paths: list[str]
    chain_id: str | None
    score: float


@dataclass
class ChainCard:
    """One reply chain holding matched messages; ``participants`` has the keys ``from``, ``to`` and ``cc``.

    ``messages`` are only the matched ones, oldest first; ``message_count`` counts the whole chain.
    ``related`` lists ``(chain_id, subject)`` of other chains. ``conclusion`` arrives with chain summaries (M2).
    """

    chain_id: str
    subject: str
    first_ts: int | None
    last_ts: int | None
    message_count: int
    participants: dict[str, list[Person]]
    conclusion: str | None
    attachments: list[AttachmentInfo]
    related: list[tuple[str, str]]
    messages: list[MessageCard]
    score: float


@dataclass
class SearchResponse:
    """One page of results: ``chains`` when grouped by chain, else ``messages``; the totals cover every page."""

    total_messages: int
    total_chains: int
    page: int
    page_size: int
    chains: list[ChainCard]
    messages: list[MessageCard]
    mode: Literal["hybrid", "keyword_only"]
