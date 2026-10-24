"""Keyword search over the two FTS5 tables: ``chunks_fts`` (BM25 words) and ``chunks_trigram`` (partial identifiers).

``chunks_fts`` keeps ``-_#@.`` inside tokens, so ``INV-2024-00017`` is one term; ``chunks_trigram`` matches any
substring of three or more characters, which finds ``2024-000``. Every function takes a connection and runs a
plain SELECT, so callers pass ``ProjectStore.read_conn()``. That connection is ``query_only``, which refuses
writes to TEMP tables too, so a candidate list longer than ``INLINE_CANDIDATE_LIMIT`` is bound as one JSON array
parameter and filtered with ``json_each`` (see ``candidates.py``).

Scores are ``-bm25()``, so higher is better. Task 23 fuses these rankings with the vector ranking by rank, so only
the order matters, not the scale.
"""

import sqlite3

from emailsupersearch.index.candidates import candidate_clause

_BM25_SQL = "SELECT rowid, -bm25(chunks_fts) AS score FROM chunks_fts WHERE chunks_fts MATCH ?"
_TRIGRAM_SQL = "SELECT rowid, -bm25(chunks_trigram) AS score FROM chunks_trigram WHERE chunks_trigram MATCH ?"
_ORDER_LIMIT = " ORDER BY score DESC LIMIT ?"
_MIN_TRIGRAM_CHARS = 3
# What SQLite says when it cannot parse a MATCH expression; any other OperationalError is a real fault.
_MATCH_PARSE_ERRORS = ("fts5:", "unterminated string", "no such column")


def sanitize_query(q: str) -> str:
    """Make every whitespace-separated word a double-quoted FTS5 phrase, so operators and punctuation are plain text.

    Quotes inside a word are dropped and words that are empty after that are skipped. Space-separated phrases mean
    AND. Returns ``""`` when nothing remains.
    """
    tokens = (token.replace('"', "") for token in q.split())
    return " ".join(f'"{token}"' for token in tokens if token)


def looks_like_identifier(q: str) -> bool:
    """True for a single token that reads as an ID, such as ``INV-2024-00017`` or ``PO#4471``.

    It needs a digit plus one of ``-``, ``_``, ``#`` or ``/``; or at least six characters mixing letters and digits.
    """
    token = q.strip()
    if not token or any(ch.isspace() for ch in token):
        return False
    has_digit = any(ch.isdigit() for ch in token)
    if has_digit and any(ch in "-_#/" for ch in token):
        return True
    return len(token) >= 6 and has_digit and any(ch.isalpha() for ch in token)


def _run(
    conn: sqlite3.Connection, sql: str, match: str, k: int, candidates: list[int] | None
) -> list[tuple[int, float]]:
    if k < 1 or (candidates is not None and not candidates):
        return []
    params: list[object] = [match]
    if candidates is not None:
        # The unary plus stops SQLite from using the candidates as a rowid constraint on the FTS5 table, which
        # re-runs the MATCH once per candidate (measured: 5 s for 20k candidates, against 15 ms without). The MATCH
        # runs once and the candidate set filters its rows.
        clause, extra = candidate_clause("+rowid", candidates)
        sql += f" AND {clause}"
        params += extra
    params.append(k)
    try:
        rows = conn.execute(sql + _ORDER_LIMIT, params).fetchall()
    except sqlite3.OperationalError as exc:
        if str(exc).startswith(_MATCH_PARSE_ERRORS):
            return []  # sanitising should make this unreachable
        raise
    return [(int(rowid), float(score)) for rowid, score in rows]


def bm25_search(conn: sqlite3.Connection, q: str, k: int, candidates: list[int] | None) -> list[tuple[int, float]]:
    """Up to ``k`` chunks containing every word of ``q``, as ``(chunk_rowid, score)``, best first.

    ``candidates`` restricts the search to those chunk rowids; an empty list matches nothing and ``None`` searches
    everything. A query with no words matches nothing.
    """
    match = sanitize_query(q)
    return _run(conn, _BM25_SQL, match, k, candidates) if match else []


def trigram_search(conn: sqlite3.Connection, term: str, k: int, candidates: list[int] | None) -> list[tuple[int, float]]:
    """Up to ``k`` chunks whose text contains ``term`` as a substring (case-insensitive), best first.

    The term is matched as written, quotes and whitespace at its ends aside; one shorter than three characters
    matches nothing. ``candidates`` works as in ``bm25_search``.
    """
    term = term.replace('"', "").strip()
    if len(term) < _MIN_TRIGRAM_CHARS:
        return []
    return _run(conn, _TRIGRAM_SQL, f'"{term}"', k, candidates)


def keyword_search(conn: sqlite3.Connection, q: str, k: int, candidates: list[int] | None) -> list[tuple[int, float]]:
    """BM25 hits for ``q``, plus trigram hits when ``q`` looks like an identifier, merged by best score."""
    best = dict(bm25_search(conn, q, k, candidates))
    if looks_like_identifier(q):
        for rowid, score in trigram_search(conn, q.strip(), k, candidates):
            best[rowid] = max(score, best.get(rowid, score))
    merged = sorted(best.items(), key=lambda hit: (-hit[1], hit[0]))
    return merged[:k]
