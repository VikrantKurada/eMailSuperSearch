"""Turn the UI's ``SearchFilters`` into the sets both retrievers are restricted to, and list messages by date.

``candidate_message_keys`` answers "which messages may a search return" and ``chunk_rowids_for`` maps that answer to
chunk rowids for ``keyword_search`` and the vector search. A message whose files are all tombstoned (deleted on disk,
not yet purged) never qualifies: a candidate list holds live messages only, and without a filter (no list at all)
the search drops such messages when it builds its cards. Every list is bound as ONE JSON array parameter and unpacked
with ``json_each``;
the SQL text only ever holds the fixed fragments below, so no value is interpolated. Everything runs on a plain
connection, so callers pass ``ProjectStore.read_conn()``.

``only_with_conclusion`` is ignored in this milestone: chain summaries arrive in M2.
"""

import json
import sqlite3

from emailsupersearch.types import SearchFilters

LIVE_MESSAGE_SQL = "EXISTS (SELECT 1 FROM files f WHERE f.message_key = m.message_key AND f.tombstoned_at IS NULL)"

_SELECT_KEYS = "SELECT m.message_key FROM messages m WHERE "
_NEWEST_FIRST = " ORDER BY m.sent_ts DESC, m.message_key"


def _addresses(values: list[str]) -> list[str]:
    """The addresses stripped and lower-cased, with blanks dropped."""
    return [addr for addr in (value.strip().lower() for value in values) if addr]


def _where(f: SearchFilters) -> tuple[str, list]:
    """The WHERE condition over ``messages m`` and its parameters, in order.

    It always holds ``LIVE_MESSAGE_SQL`` and then one AND-ed clause per filter that is set. A filter is set when it
    has a value (``has_attachments=False`` counts); address lists that are empty once blanks are dropped are not.
    A date bound excludes messages with no ``sent_ts``.
    """
    clauses = [LIVE_MESSAGE_SQL]
    params: list = []
    # The address predicates are written as exactly ``lower(sender_addr)`` and ``lower(addr)`` so the expression
    # indexes serve them; the bound addresses are already stripped and lower-cased (the parsers store them stripped).
    senders = _addresses(f.from_addrs)
    if senders:
        clauses.append("lower(m.sender_addr) IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(senders))
    recipients = _addresses(f.recipient_addrs)
    if recipients:
        clauses.append(
            "m.message_key IN (SELECT r.message_key FROM recipients r"
            " WHERE lower(r.addr) IN (SELECT value FROM json_each(?)) AND r.kind IN ('to','cc'))"
        )
        params.append(json.dumps(recipients))
    if f.date_from is not None:
        clauses.append("m.sent_ts >= ?")
        params.append(f.date_from)
    if f.date_to is not None:
        clauses.append("m.sent_ts <= ?")
        params.append(f.date_to)
    if f.attachment_kinds:
        clauses.append(
            "EXISTS (SELECT 1 FROM attachments a WHERE a.message_key = m.message_key"
            " AND a.kind IN (SELECT value FROM json_each(?)))"
        )
        params.append(json.dumps(list(f.attachment_kinds)))
    if f.has_attachments is not None:
        clauses.append("m.has_attachments = 1" if f.has_attachments else "m.has_attachments = 0")
    return " AND ".join(clauses), params


def candidate_message_keys(conn: sqlite3.Connection, f: SearchFilters) -> list[str] | None:
    """Keys of the live messages that pass every set filter, newest first; ``None`` means "no restriction".

    ``None`` is returned whenever no filter is set, tombstoned files or not: building a list of every message
    (100k keys at the owner's scale) would cost more than the search itself. Liveness is enforced where the results
    are built instead: ``build_message_cards`` keeps only messages with a live file, and ``messages_by_date`` and the
    counts carry ``LIVE_MESSAGE_SQL``. ``query``, ``group_by_chain``, ``sort`` and ``only_with_conclusion`` are not
    candidate filters.
    """
    where, params = _where(f)
    if where == LIVE_MESSAGE_SQL:
        return None
    return [row[0] for row in conn.execute(_SELECT_KEYS + where + _NEWEST_FIRST, params)]


def chunk_rowids_for(conn: sqlite3.Connection, message_keys: list[str] | None) -> list[int] | None:
    """The chunk rowids of ``message_keys`` in rowid order; ``None`` (no restriction) stays ``None``."""
    if message_keys is None:
        return None
    if not message_keys:
        return []
    rows = conn.execute(
        "SELECT id FROM chunks WHERE message_key IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(message_keys),),
    )
    return [row[0] for row in rows]


def messages_by_date(conn: sqlite3.Connection, f: SearchFilters, limit: int, offset: int) -> list[str]:
    """One page of live message keys passing the filters, newest first, for searches with an empty query.

    Messages without a ``sent_ts`` come last. ``limit < 1``, or an offset beyond SQLite's integer range (a page
    number no archive reaches), returns ``[]``.
    """
    if limit < 1 or offset >= 2**63:
        return []
    where, params = _where(f)
    sql = _SELECT_KEYS + where + _NEWEST_FIRST + " LIMIT ? OFFSET ?"
    return [row[0] for row in conn.execute(sql, [*params, limit, offset])]


def count_messages(conn: sqlite3.Connection, f: SearchFilters) -> int:
    """How many live messages pass the filters: the total behind the pages of ``messages_by_date``."""
    where, params = _where(f)
    return int(conn.execute("SELECT COUNT(*) FROM messages m WHERE " + where, params).fetchone()[0])


def count_chains(conn: sqlite3.Connection, f: SearchFilters) -> int:
    """How many chains the live messages passing the filters belong to; a message without a chain is one of its own."""
    where, params = _where(f)
    sql = "SELECT COUNT(DISTINCT COALESCE(m.chain_id, m.message_key)) FROM messages m WHERE " + where
    return int(conn.execute(sql, params).fetchone()[0])
