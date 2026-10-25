"""Hybrid search: keyword and semantic hits over the filtered candidates, fused by rank and turned into cards.

``search`` asks FTS5 (``keyword_search``) and, when an embedder is loaded, sqlite-vec (``VectorIndex.knn``) for the
best ``CHUNK_FETCH_FACTOR * K_EACH`` chunks each, restricted to the chunks of the messages the filters allow. Each
retriever's chunks are mapped to their messages and only its best ``K_EACH`` messages (by their best chunk) go on,
so one long message cannot crowd the others out. BM25 and cosine scores are on different scales, so the two message
rankings are fused by position only, with reciprocal rank fusion (``rrf``); an identifier query (``INV-2024-00017``)
is ranked by the keyword list alone, because a semantic neighbour must never outrank the exact hit. Each message
shows the snippet of its best chunk. A message without a live (not tombstoned) file gets no card, whatever the
candidate filter let through. The cards are optionally grouped into reply chains.

A query's results are therefore its top matches, at most ``K_EACH`` per retriever, and ``total_messages`` counts
those; only a search with an empty query (filters alone) counts every matching message.

Every read runs on ``ProjectStore.read_conn()``, never on the writer. Every list of keys or ids is bound as ONE
JSON array parameter and unpacked with ``json_each``, and cards are built with one query per table, not per card.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Hashable, Sequence
from typing import TYPE_CHECKING, Literal

from emailsupersearch.index.fts import keyword_search, looks_like_identifier
from emailsupersearch.index.vectors import VectorIndex
from emailsupersearch.ingest.attachments import INLINE_IMAGE_MIN_BYTES
from emailsupersearch.ingest.identity import normalize_subject
from emailsupersearch.search.filters import (
    LIVE_MESSAGE_SQL,
    candidate_message_keys,
    chunk_rowids_for,
    count_chains,
    count_messages,
    messages_by_date,
)
from emailsupersearch.search.results import (
    AttachmentInfo,
    ChainCard,
    MessageCard,
    SearchResponse,
)
from emailsupersearch.types import Person, SearchFilters

if TYPE_CHECKING:
    from emailsupersearch.index.embedder import OnnxEmbedder
    from emailsupersearch.store import ProjectStore

_Mode = Literal["hybrid", "keyword_only"]

RRF_K = 60
K_EACH = 50  # messages each retriever contributes to a search
K_EXPORT = 2000  # messages each retriever contributes to an export of a query
CHUNK_FETCH_FACTOR = 4  # each retriever fetches this many chunks per message it may keep
PAGE_SIZE = 20
# An address on more than this share of the messages (the mailbox owner, a team list) links no chains as related.
STOP_ADDRESS_SHARE = 0.05
# A filter-only search grouped by chain groups at most this many of the newest matching messages, so beyond it the
# chains shown and their count cover only those messages (``total_messages`` still counts every match).
FILTER_ONLY_GROUP_LIMIT = 2000

_SNIPPET_CHARS = 240
_MAX_CHAIN_ATTACHMENTS = 10
_MAX_RELATED = 3

_CHUNKS_SQL = "SELECT id, message_key, text FROM chunks WHERE id IN (SELECT value FROM json_each(?))"
_LIVE_PATHS_SQL = (
    "SELECT message_key, path FROM files WHERE tombstoned_at IS NULL"
    " AND message_key IN (SELECT value FROM json_each(?)) ORDER BY path"
)
_MESSAGES_SQL = (
    "SELECT message_key, subject, sent_ts, sender_name, sender_addr, chain_id FROM messages"
    " WHERE message_key IN (SELECT value FROM json_each(?))"
)
_RECIPIENTS_SQL = (
    "SELECT message_key, kind, name, addr FROM recipients"
    " WHERE kind IN ('to', 'cc') AND message_key IN (SELECT value FROM json_each(?)) ORDER BY rowid"
)
_ATTACHMENTS_SQL = (
    "SELECT message_key, ordinal, filename, kind, size, extract_status, ocr_status, embedded_partial, page_count"
    " FROM attachments WHERE message_key IN (SELECT value FROM json_each(?)) ORDER BY message_key, ordinal"
)
_CHAINS_SQL = (
    "SELECT chain_id, subject, first_ts, last_ts, message_count, participants_json FROM chains"
    " WHERE chain_id IN (SELECT value FROM json_each(?))"
)
# Related chains count only live messages (``LIVE_MESSAGE_SQL``), so a chain whose files are all tombstoned is never
# linked. Written with exactly ``lower(sender_addr)`` and ``lower(addr)`` so SQLite uses the expression indexes; the
# addresses are bound lower-cased. Recipients are ``to`` and ``cc`` only, like a chain's own participants.
_RELATED_BY_PEOPLE = (
    "SELECT chain_id, COUNT(DISTINCT addr) FROM ("
    "SELECT m.chain_id, lower(m.sender_addr) AS addr FROM messages m"
    f" WHERE lower(m.sender_addr) IN (SELECT value FROM json_each(?)) AND {LIVE_MESSAGE_SQL}"
    " UNION SELECT m.chain_id, lower(r.addr) FROM recipients r JOIN messages m ON m.message_key = r.message_key"
    f" WHERE lower(r.addr) IN (SELECT value FROM json_each(?)) AND r.kind IN ('to', 'cc') AND {LIVE_MESSAGE_SQL})"
    " WHERE chain_id IS NOT NULL AND chain_id != ? GROUP BY chain_id HAVING COUNT(DISTINCT addr) >= 2"
    " ORDER BY COUNT(DISTINCT addr) DESC, chain_id LIMIT 3"
)
# A shared image smaller than ``INLINE_IMAGE_MIN_BYTES`` is a logo or signature, so it links nothing (both sides).
_RELATED_BY_ATTACHMENT = (
    "SELECT DISTINCT m.chain_id FROM attachments a JOIN messages m ON m.message_key = a.message_key"
    " WHERE a.sha256 IN (SELECT a2.sha256 FROM attachments a2 JOIN messages m2 ON m2.message_key = a2.message_key"
    " WHERE m2.chain_id = ? AND a2.sha256 IS NOT NULL AND NOT (a2.kind = 'image' AND a2.size < ?))"
    " AND NOT (a.kind = 'image' AND a.size < ?)"
    f" AND m.chain_id IS NOT NULL AND m.chain_id != ? AND {LIVE_MESSAGE_SQL} ORDER BY m.chain_id LIMIT 3"
)
_CHAIN_MEMBERS_SQL = (
    f"SELECT m.message_key FROM messages m WHERE m.chain_id = ? AND {LIVE_MESSAGE_SQL}"
    " ORDER BY m.sent_ts IS NULL, m.sent_ts, m.message_key"
)
_SUBJECTS_SQL = (
    "SELECT c.chain_id, c.subject FROM chains c WHERE c.chain_id IN (SELECT value FROM json_each(?))"
    f" AND EXISTS (SELECT 1 FROM messages m WHERE m.chain_id = c.chain_id AND {LIVE_MESSAGE_SQL})"
)
# The addresses on more than a given number of messages, as sender or ``to``/``cc`` recipient, in one GROUP BY
# (``!= ''`` also drops NULL). UNION ALL with COUNT(DISTINCT) counts each message once per address, like a UNION, at
# a quarter of its cost (measured at 100k messages: 0.3 s against 1.4 s).
_STOP_ADDRESSES_SQL = (
    "SELECT addr FROM ("
    "SELECT lower(sender_addr) AS addr, message_key FROM messages WHERE sender_addr != ''"
    " UNION ALL SELECT lower(addr), message_key FROM recipients WHERE kind IN ('to', 'cc') AND addr != '')"
    " GROUP BY addr HAVING COUNT(DISTINCT message_key) > ?"
)

# Per database file: (message count and share when computed, stop addresses). Recomputed when the count changes.
_stop_cache: dict[str, tuple[int, float, frozenset[str]]] = {}
_stop_lock = threading.Lock()


def rrf[Item: Hashable](ranked_lists: Sequence[Sequence[Item]], k: int = RRF_K) -> dict[Item, float]:
    """Reciprocal rank fusion: each id scores the sum of ``1 / (k + rank)`` over the lists it is in, ranks from 1.

    Only positions count, so rankings on different scales (BM25, cosine) fuse fairly. An id repeated within one
    list counts at its best rank. Ids are chunk rowids or message keys.
    """
    scores: dict[Item, float] = {}
    for ranked in ranked_lists:
        seen: set[Item] = set()
        for rank, item in enumerate(ranked, start=1):
            if item not in seen:
                seen.add(item)
                scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def rank_messages(fused: dict[int, float], chunk_rows: Sequence[Sequence]) -> list[tuple[str, float, int]]:
    """Each message's best chunk as ``(message_key, score, chunk_rowid)``, best score first, then by key.

    ``chunk_rows`` are ``(id, message_key, ...)`` rows of the fused chunks; a message's best chunk is its highest
    scoring one, the lower rowid on a tie. A row whose id has no fused score is ignored.
    """
    best: dict[str, tuple[float, int]] = {}
    for row in chunk_rows:
        rowid, key = row[0], row[1]
        if rowid not in fused:
            continue
        held = best.get(key)
        if held is None or fused[rowid] > held[0] or (fused[rowid] == held[0] and rowid < held[1]):
            best[key] = (fused[rowid], rowid)
    ranked = [(key, score, rowid) for key, (score, rowid) in best.items()]
    ranked.sort(key=lambda hit: (-hit[1], hit[0]))
    return ranked


def _snippet(chunk_text: str) -> str:
    """A chunk's text after its first line (the context line), whitespace collapsed, cut at 240 characters."""
    text = " ".join(chunk_text.partition("\n")[2].split())
    return text if len(text) <= _SNIPPET_CHARS else text[:_SNIPPET_CHARS].rstrip() + "…"


def _live_paths(conn: sqlite3.Connection, keys: list[str]) -> dict[str, list[str]]:
    """The live (not tombstoned) file paths of each of ``keys`` that has one, sorted."""
    paths: dict[str, list[str]] = {}
    for key, path in conn.execute(_LIVE_PATHS_SQL, (json.dumps(keys),)).fetchall():
        paths.setdefault(key, []).append(path)
    return paths


def _recipients(conn: sqlite3.Connection, keys_json: str) -> dict[str, dict[str, list[Person]]]:
    """The ``to`` and ``cc`` recipients of each message, in stored order; ``bcc`` is never shown."""
    found: dict[str, dict[str, list[Person]]] = {}
    for key, kind, name, addr in conn.execute(_RECIPIENTS_SQL, (keys_json,)).fetchall():
        found.setdefault(key, {"to": [], "cc": []})[kind].append(Person(name or "", addr or ""))
    return found


def _attachments(conn: sqlite3.Connection, keys_json: str) -> dict[str, list[AttachmentInfo]]:
    """The attachments of each message, by ordinal."""
    found: dict[str, list[AttachmentInfo]] = {}
    for key, ordinal, filename, kind, size, extract, ocr, partial, pages in conn.execute(
        _ATTACHMENTS_SQL, (keys_json,)
    ).fetchall():
        found.setdefault(key, []).append(
            AttachmentInfo(ordinal, filename or "", kind or "other", size, extract, ocr, bool(partial), pages)
        )
    return found


def build_message_cards(
    conn: sqlite3.Connection, scored: Sequence[tuple[str, float, int | None]], snippets: dict[int, str]
) -> list[MessageCard]:
    """Cards for the scored messages that have a live file, in ``scored`` order.

    ``scored`` holds ``(message_key, score, best_chunk_rowid)``; the card's snippet is ``snippets[rowid]``, or
    ``""`` when the rowid is ``None`` or absent. A message whose files are all tombstoned, that has no file row or
    no message row, gets no card. One query per table covers every card.
    """
    paths = _live_paths(conn, [key for key, _, _ in scored])
    keys_json = json.dumps(list(paths))
    messages = {row[0]: row for row in conn.execute(_MESSAGES_SQL, (keys_json,)).fetchall()}
    recipients = _recipients(conn, keys_json)
    attachments = _attachments(conn, keys_json)
    cards: list[MessageCard] = []
    for key, score, rowid in scored:
        if key not in messages:
            continue
        _, subject, sent_ts, sender_name, sender_addr, chain_id = messages[key]
        people = recipients.get(key, {"to": [], "cc": []})
        cards.append(
            MessageCard(
                key, subject or "", sent_ts, Person(sender_name or "", sender_addr or ""), people["to"],
                people["cc"], snippets.get(rowid, "") if rowid is not None else "", attachments.get(key, []),
                paths[key], chain_id, score,
            )
        )
    return cards


def _oldest_first(cards: list[MessageCard]) -> list[MessageCard]:
    """``cards`` by ``sent_ts`` ascending, undated last, then by key."""
    return sorted(cards, key=lambda c: (c.sent_ts is None, c.sent_ts or 0, c.message_key))


def cards_for_keys(conn: sqlite3.Connection, keys: list[str]) -> list[MessageCard]:
    """Unscored cards (score 0.0, no snippet) for the live messages among ``keys``, oldest first, undated last.

    A repeated key gives one card; a key without a live file or a message row gives none.
    """
    unique = list(dict.fromkeys(keys))
    return _oldest_first(build_message_cards(conn, [(key, 0.0, None) for key in unique], {}))


def chain_member_keys(conn: sqlite3.Connection, chain_id: str) -> list[str]:
    """The keys of the chain's live messages, by ``sent_ts`` (undated last), then by key."""
    return [row[0] for row in conn.execute(_CHAIN_MEMBERS_SQL, (chain_id,)).fetchall()]


def _participants(raw: str | None) -> dict[str, list[Person]]:
    """A ``chains.participants_json`` value as ``Person`` lists under ``from``, ``to`` and ``cc``.

    A value that is not valid JSON or not an object gives empty lists, as does a key whose value is not a list.
    """
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    participants: dict[str, list[Person]] = {}
    for kind in ("from", "to", "cc"):
        found = data.get(kind)
        people = found if isinstance(found, list) else []
        participants[kind] = [Person(p.get("name") or "", p.get("addr") or "") for p in people if isinstance(p, dict)]
    return participants


def _distinct(people: list[Person]) -> list[Person]:
    """``people`` without repeats of a lower-cased address; the first seen wins."""
    seen: dict[str, Person] = {}
    for person in people:
        seen.setdefault(person.addr.lower(), person)
    return list(seen.values())


def _chain_card(chain_id: str, members: list[MessageCard], row: Sequence | None) -> ChainCard:
    """The card of one chain from its ``chains`` row, or from its matched messages when it has no row."""
    members = _oldest_first(members)
    shown: dict[tuple[str, int | None], AttachmentInfo] = {}
    for att in (a for card in members for a in card.attachments):
        shown.setdefault((att.filename, att.size), att)
    attachments = list(shown.values())[:_MAX_CHAIN_ATTACHMENTS]
    score = max(card.score for card in members)
    if row is not None:
        _, subject, first_ts, last_ts, count, raw = row
        return ChainCard(
            chain_id, subject or "", first_ts, last_ts, count, _participants(raw), None, attachments, [], members, score
        )
    dated = [card.sent_ts for card in members if card.sent_ts is not None]
    participants = {
        "from": _distinct([card.sender for card in members]),
        "to": _distinct([p for card in members for p in card.to]),
        "cc": _distinct([p for card in members for p in card.cc]),
    }
    return ChainCard(
        chain_id, normalize_subject(members[0].subject), min(dated, default=None), max(dated, default=None),
        len(members),
        participants, None, attachments, [], members, score,
    )


def group_chains(cards: list[MessageCard], conn: sqlite3.Connection) -> list[ChainCard]:
    """Group ``cards`` into chain cards, best chain score first (ties keep the order the cards came in).

    A message without a chain id is a chain of its own whose id is its message key. Chain fields come from the
    ``chains`` row (one query for all chains); a chain's score is its best message's and ``related`` is left empty.
    """
    groups: dict[str, list[MessageCard]] = {}
    for card in cards:
        groups.setdefault(card.chain_id or card.message_key, []).append(card)
    real = sorted({card.chain_id for card in cards if card.chain_id})
    rows = {row[0]: row for row in conn.execute(_CHAINS_SQL, (json.dumps(real),)).fetchall()}
    chains = [
        _chain_card(cid, members, rows.get(cid) if members[0].chain_id else None) for cid, members in groups.items()
    ]
    chains.sort(key=lambda chain: -chain.score)
    return chains


def stop_addresses(conn: sqlite3.Connection) -> frozenset[str]:
    """The lower-cased addresses on more than ``STOP_ADDRESS_SHARE`` of the messages, as sender or ``to``/``cc``.

    In a personal archive the owner is on nearly every message, so sharing that address says nothing, and looking
    chains up by it reads the whole archive. Computed with one GROUP BY and cached per database file and message
    count, so it is recomputed only when the count changes.
    """
    count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    database = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "") or str(id(conn))
    share = STOP_ADDRESS_SHARE
    with _stop_lock:
        cached = _stop_cache.get(database)
    if cached is not None and cached[:2] == (count, share):
        return cached[2]
    found = frozenset(row[0] for row in conn.execute(_STOP_ADDRESSES_SQL, (count * share,)).fetchall())
    with _stop_lock:
        _stop_cache[database] = (count, share, found)
    return found


def _related(conn: sqlite3.Connection, chains: list[ChainCard]) -> None:
    """Fill each chain's ``related``: up to 3 other chains with a live message that share at least 2 participant
    addresses or an attachment sha256. Participant matches come first, most shared addresses first, then attachment
    matches. Stop addresses (``stop_addresses``) do not count: two chains are related by people only when they share
    at least 2 other addresses."""
    stop = stop_addresses(conn) if chains else frozenset()
    found: dict[str, list[str]] = {}
    for chain in chains:
        addrs = sorted({p.addr.strip().lower() for people in chain.participants.values() for p in people} - stop)
        addrs = [addr for addr in addrs if addr]
        ids: list[str] = []
        if len(addrs) >= 2:
            addrs_json = json.dumps(addrs)
            rows = conn.execute(_RELATED_BY_PEOPLE, (addrs_json, addrs_json, chain.chain_id)).fetchall()
            ids = [row[0] for row in rows]
        by_attachment = (chain.chain_id, INLINE_IMAGE_MIN_BYTES, INLINE_IMAGE_MIN_BYTES, chain.chain_id)
        ids += [row[0] for row in conn.execute(_RELATED_BY_ATTACHMENT, by_attachment).fetchall()]
        found[chain.chain_id] = list(dict.fromkeys(ids))[:_MAX_RELATED]
    wanted = sorted({cid for ids in found.values() for cid in ids})
    subjects = {cid: subject for cid, subject in conn.execute(_SUBJECTS_SQL, (json.dumps(wanted),)).fetchall()}
    for chain in chains:
        chain.related = [(cid, subjects[cid] or "") for cid in found[chain.chain_id] if cid in subjects]


def _newest_first(cards: list[MessageCard]) -> list[MessageCard]:
    """``cards`` by ``sent_ts`` descending, undated last; equal dates keep their order."""
    return sorted(cards, key=lambda c: (c.sent_ts is None, -(c.sent_ts or 0)))


def _page(items: list, page: int, page_size: int) -> list:
    return items[page * page_size : (page + 1) * page_size]


def _chain_count(cards: list[MessageCard]) -> int:
    return len({card.chain_id or card.message_key for card in cards})


def _grouped(
    conn: sqlite3.Connection, cards: list[MessageCard], total_messages: int, page: int, page_size: int, mode: _Mode,
    by_date: bool,
) -> SearchResponse:
    """One page of chain cards, by score or (``by_date``) by ``last_ts`` descending; ``related`` for that page only."""
    chains = group_chains(cards, conn)
    if by_date:
        chains.sort(key=lambda c: (c.last_ts is None, -(c.last_ts or 0)))
    shown = _page(chains, page, page_size)
    _related(conn, shown)
    return SearchResponse(total_messages, len(chains), page, page_size, shown, [], mode)


def _filter_only(conn: sqlite3.Connection, f: SearchFilters, page: int, page_size: int, mode: _Mode) -> SearchResponse:
    """An empty query lists the matching messages newest first, unscored and without snippets.

    Ungrouped, ``total_chains`` counts the chains of every match (``count_chains``). Grouped, only the newest
    ``FILTER_ONLY_GROUP_LIMIT`` messages are grouped, so ``total_chains`` counts the chains among those, which is
    what the pages can show.
    """
    total = count_messages(conn, f)
    if not f.group_by_chain:
        keys = messages_by_date(conn, f, page_size, page * page_size)
        cards = build_message_cards(conn, [(key, 0.0, None) for key in keys], {})
        return SearchResponse(total, count_chains(conn, f), page, page_size, [], cards, mode)
    keys = messages_by_date(conn, f, FILTER_ONLY_GROUP_LIMIT, 0)
    cards = build_message_cards(conn, [(key, 0.0, None) for key in keys], {})
    return _grouped(conn, cards, total, page, page_size, mode, by_date=True)


def _top_messages(hits: Sequence[int], keys: dict[int, str], k: int) -> list[str]:
    """The distinct messages of ``hits`` (chunk rowids, best first), each at the rank of its best chunk; at most ``k``."""
    found: dict[str, None] = {}
    for rowid in hits:
        key = keys.get(rowid)
        if key is not None and key not in found:
            found[key] = None
            if len(found) == k:
                break
    return list(found)


def _query_cards(
    conn: sqlite3.Connection, embedder: OnnxEmbedder | None, f: SearchFilters, query: str, k_each: int
) -> tuple[list[MessageCard], bool]:
    """The cards of a query's top matches, best fused score first, and whether the keyword retriever came back full.

    Each retriever fetches ``CHUNK_FETCH_FACTOR * k_each`` chunks and contributes its best ``k_each`` messages; the
    message rankings are fused with ``rrf``. An identifier query uses the keyword ranking alone. The flag is True when
    the keyword retriever (BM25 and trigram) returned as many chunks as it asked for, or ``k_each`` messages: more
    emails contain the words than these. The vector retriever never sets it: a nearest-neighbour search always fills
    its quota, so its being full says nothing about how many emails match.
    """
    rowids = chunk_rowids_for(conn, candidate_message_keys(conn, f))
    if rowids == []:
        return [], False
    fetch = CHUNK_FETCH_FACTOR * k_each
    chunk_lists = [[rowid for rowid, _ in keyword_search(conn, query, fetch, rowids)]]
    if embedder is not None and not looks_like_identifier(query):
        vector = embedder.embed([query])[0]
        chunk_lists.append([rowid for rowid, _ in VectorIndex(conn).knn(vector, fetch, rowids)])
    fetched = list(dict.fromkeys(rowid for hits in chunk_lists for rowid in hits))
    chunk_rows = conn.execute(_CHUNKS_SQL, (json.dumps(fetched),)).fetchall()
    keys = {row[0]: row[1] for row in chunk_rows}
    message_lists = [_top_messages(hits, keys, k_each) for hits in chunk_lists]
    capped = len(chunk_lists[0]) >= fetch or len(message_lists[0]) >= k_each  # the keyword list only
    fused = rrf(message_lists)
    best_chunk = {key: rowid for key, _, rowid in rank_messages(rrf(chunk_lists), chunk_rows)}
    scored = sorted(((key, score, best_chunk[key]) for key, score in fused.items()), key=lambda hit: (-hit[1], hit[0]))
    shown = {rowid for _, _, rowid in scored}
    snippets = {row[0]: _snippet(row[2]) for row in chunk_rows if row[0] in shown}
    return build_message_cards(conn, scored, snippets), capped


def query_matches(
    store: ProjectStore, embedder: OnnxEmbedder | None, f: SearchFilters, k_each: int = K_EXPORT
) -> tuple[list[MessageCard], bool]:
    """Every top match of ``f``'s non-empty query as message cards, in ``f.sort`` order, and whether the keyword
    retriever came back full (more emails contain the words than these). The Excel export of a query uses it with
    ``K_EXPORT``."""
    conn = store.read_conn()
    cards, capped = _query_cards(conn, embedder, f, f.query.strip(), k_each)
    return (_newest_first(cards) if f.sort == "date" else cards), capped


def search(
    store: ProjectStore,
    embedder: OnnxEmbedder | None,
    f: SearchFilters,
    page: int = 0,
    page_size: int = PAGE_SIZE,
) -> SearchResponse:
    """One page of results for ``f``: chain cards when ``f.group_by_chain``, else message cards.

    Without an embedder only the keyword ranking is used (``mode == "keyword_only"``). ``f.sort == "date"`` orders
    the fused results by date, newest first; otherwise they are ordered by fused score. A query's totals count its
    top matches (at most ``K_EACH`` messages per retriever) and their chains, not every email that contains the
    words. An empty query lists the filtered messages by date instead, and its totals count every match
    (``_filter_only`` notes the one cap on its chain total).
    """
    conn = store.read_conn()
    mode: _Mode = "hybrid" if embedder is not None else "keyword_only"
    query = f.query.strip()
    if not query:
        return _filter_only(conn, f, page, page_size, mode)
    cards, _ = _query_cards(conn, embedder, f, query, K_EACH)
    if f.sort == "date":
        cards = _newest_first(cards)
    if f.group_by_chain:
        return _grouped(conn, cards, len(cards), page, page_size, mode, by_date=f.sort == "date")
    return SearchResponse(len(cards), _chain_count(cards), page, page_size, [], _page(cards, page, page_size), mode)
