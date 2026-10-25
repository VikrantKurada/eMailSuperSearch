"""Chains: every stored message belongs to one chain, an e-mail thread.

``assign_chain`` decides the chain of a message before it is written; ``update_chain_row`` recomputes the
``chains`` row that result cards read, after any message of the chain was written or deleted. A message joins
the chain named by, in this order:

1. the Outlook conversation index (``t-<thread GUID>``), which is exact and also gives the reply depth;
2. the ``In-Reply-To`` / ``References`` headers: the chain of the nearest ancestor already stored;
3. the same normalised subject sent within 30 days of a stored message that shares an address with it,
   which catches replies that lost their headers;
4. otherwise a new chain, ``fallback_chain_id``: ``c-`` and the first 16 hex digits of the key's SHA-256 (Exchange
   Message-IDs share long prefixes, so the key's own first characters would put unrelated messages in one chain).

Reads run on the writer connection under ``store.lock``, as plain SELECTs with no transaction of their own;
``update_chain_row`` writes under ``store.lock`` inside one transaction. ``recompute_chain_row`` is the same write for
a caller that already holds the lock and has a transaction open (the store's ``write_indexed_message``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from typing import TYPE_CHECKING

from emailsupersearch.ingest.identity import normalize_subject
from emailsupersearch.types import ParsedMessage

if TYPE_CHECKING:
    from emailsupersearch.store import ProjectStore

# PR_CONVERSATION_INDEX: a 22-byte header (6 bytes of time, then the 16-byte thread GUID), then 5 bytes per reply.
_HEADER_LEN = 22
_GUID_START = 6
_REPLY_LEN = 5
# Two messages with the same subject and a common address are one chain when sent at most this far apart.
_SUBJECT_WINDOW_SECONDS = 30 * 86400


def thread_guid(conversation_index: bytes | None) -> str | None:
    """Hex of the 16-byte thread GUID (bytes 6..22), or None when there is no index or it is too short."""
    if conversation_index is None or len(conversation_index) < _HEADER_LEN:
        return None
    return conversation_index[_GUID_START:_HEADER_LEN].hex()


def chain_depth(conversation_index: bytes | None) -> int:
    """How many replies deep the message is: one 5-byte block per reply after the 22-byte header."""
    if conversation_index is None or len(conversation_index) < _HEADER_LEN:
        return 0
    return (len(conversation_index) - _HEADER_LEN) // _REPLY_LEN


def fallback_chain_id(message_key: str) -> str:
    """The id of a new chain started by ``message_key``: ``c-`` and the first 16 hex digits of its SHA-256."""
    return "c-" + hashlib.sha256(message_key.encode("utf-8")).hexdigest()[:16]


def assign_chain(store: ProjectStore, m: ParsedMessage, message_key: str) -> tuple[str, int]:
    """The chain id and depth for ``m``, which is about to be stored under ``message_key``.

    A message that is already stored under ``message_key`` never counts as its own ancestor or chain mate,
    so re-ingesting it gives the same answer as ingesting it the first time.
    """
    guid = thread_guid(m.conversation_index)
    if guid is not None:
        return "t-" + guid, chain_depth(m.conversation_index)
    with store.lock:
        found = _by_headers(store.conn, m, message_key) or _by_subject(store.conn, m, message_key)
    if found is not None:
        return found
    return fallback_chain_id(message_key), 0


def _by_headers(conn: sqlite3.Connection, m: ParsedMessage, message_key: str) -> tuple[str, int] | None:
    """The chain of the nearest stored ancestor named by ``In-Reply-To`` or ``References``, one level deeper."""
    # Nearest first: the direct parent, then the references from the newest to the oldest.
    wanted = list(dict.fromkeys(i for i in (m.in_reply_to, *reversed(m.references)) if i))
    if not wanted:
        return None
    rows = conn.execute(
        "SELECT message_id, chain_id, chain_depth FROM messages"
        " WHERE message_id IN (SELECT value FROM json_each(?)) AND message_key != ? AND chain_id IS NOT NULL"
        " ORDER BY message_key",
        (json.dumps(wanted), message_key),
    ).fetchall()
    stored: dict[str, sqlite3.Row] = {}
    for row in rows:
        stored.setdefault(row["message_id"], row)
    for message_id in wanted:
        parent = stored.get(message_id)
        if parent is not None:
            return parent["chain_id"], (parent["chain_depth"] or 0) + 1
    return None


def _by_subject(conn: sqlite3.Connection, m: ParsedMessage, message_key: str) -> tuple[str, int] | None:
    """The chain of the closest stored message with the same subject and a shared address, one level deeper."""
    subject = normalize_subject(m.subject)
    if m.sent_ts is None or not subject:
        return None
    mine = {m.sender_addr.lower(), *(r.addr.lower() for r in m.recipients)} - {""}
    if not mine:
        return None
    candidates = conn.execute(
        "SELECT message_key, chain_id, sender_addr FROM messages"
        " WHERE normalized_subject = ? AND sent_ts BETWEEN ? AND ? AND message_key != ? AND chain_id IS NOT NULL"
        " ORDER BY ABS(sent_ts - ?), message_key",
        (subject, m.sent_ts - _SUBJECT_WINDOW_SECONDS, m.sent_ts + _SUBJECT_WINDOW_SECONDS, message_key, m.sent_ts),
    ).fetchall()
    if not candidates:
        return None
    theirs: dict[str, set[str]] = {c["message_key"]: set() for c in candidates}
    for c in candidates:
        if c["sender_addr"]:
            theirs[c["message_key"]].add(c["sender_addr"].lower())
    for r in conn.execute(
        "SELECT message_key, addr FROM recipients WHERE message_key IN (SELECT value FROM json_each(?))",
        (json.dumps(list(theirs)),),
    ):
        if r["addr"]:
            theirs[r["message_key"]].add(r["addr"].lower())
    for c in candidates:
        if mine & theirs[c["message_key"]]:
            deepest = conn.execute(
                "SELECT MAX(chain_depth) FROM messages WHERE chain_id = ? AND message_key != ?",
                (c["chain_id"], message_key),
            ).fetchone()[0]
            return c["chain_id"], (deepest or 0) + 1
    return None


def _distinct_people(rows: Iterable[tuple[str | None, str | None]]) -> list[dict[str, str]]:
    """``{"name", "addr"}`` objects distinct by lower-cased address; the first name seen for an address wins."""
    people: dict[str, dict[str, str]] = {}
    for name, addr in rows:
        people.setdefault((addr or "").lower(), {"name": name or "", "addr": addr or ""})
    return list(people.values())


def update_chain_row(store: ProjectStore, chain_id: str) -> None:
    """Recompute the ``chains`` row of ``chain_id`` from its messages, in its own transaction (``recompute_chain_row``)."""
    with store.lock, store.conn:
        recompute_chain_row(store.conn, chain_id)


def recompute_chain_row(conn: sqlite3.Connection, chain_id: str) -> None:
    """Recompute the ``chains`` row of ``chain_id`` from its messages; delete the row when none are left.

    The caller holds the store's lock and has a transaction open; this opens none of its own. The root is the
    earliest message (undated ones last, then by key). Participants are listed as senders, ``to`` and ``cc``
    recipients in the order the dated messages were sent; ``bcc`` is never listed.
    """
    count, first_ts, last_ts = conn.execute(
        "SELECT COUNT(*), MIN(sent_ts), MAX(sent_ts) FROM messages WHERE chain_id = ?", (chain_id,)
    ).fetchone()
    if count == 0:
        conn.execute("DELETE FROM chains WHERE chain_id = ?", (chain_id,))
        return
    rows = conn.execute(
        "SELECT message_key, subject, sender_name, sender_addr FROM messages WHERE chain_id = ?"
        " ORDER BY sent_ts IS NULL, sent_ts, message_key",
        (chain_id,),
    ).fetchall()
    recipients = conn.execute(
        "SELECT r.kind, r.name, r.addr FROM recipients r JOIN messages m ON m.message_key = r.message_key"
        " WHERE m.chain_id = ? AND r.kind IN ('to', 'cc')"
        " ORDER BY m.sent_ts IS NULL, m.sent_ts, m.message_key, r.rowid",
        (chain_id,),
    ).fetchall()
    root = rows[0]
    participants = {
        "from": _distinct_people((r["sender_name"], r["sender_addr"]) for r in rows),
        "to": _distinct_people((r["name"], r["addr"]) for r in recipients if r["kind"] == "to"),
        "cc": _distinct_people((r["name"], r["addr"]) for r in recipients if r["kind"] == "cc"),
    }
    conn.execute(
        "INSERT INTO chains(chain_id, root_message_key, subject, first_ts, last_ts, message_count, participants_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(chain_id) DO UPDATE SET root_message_key = excluded.root_message_key,"
        " subject = excluded.subject, first_ts = excluded.first_ts, last_ts = excluded.last_ts,"
        " message_count = excluded.message_count, participants_json = excluded.participants_json",
        (
            chain_id, root["message_key"], normalize_subject(root["subject"] or ""), first_ts, last_ts, count,
            json.dumps(participants),
        ),
    )
