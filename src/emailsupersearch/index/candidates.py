"""Restrict a search to a list of chunk rowids without a TEMP table.

Read paths run on ``ProjectStore.read_conn()``, whose ``PRAGMA query_only=1`` refuses every write, TEMP tables
included. Up to ``INLINE_CANDIDATE_LIMIT`` rowids are bound one per ``?``; a longer list is bound as ONE JSON
array parameter and unpacked by ``json_each``, so the statement never exceeds SQLite's bound-parameter limit.
"""

import json

# At or below this many candidates each rowid gets its own parameter; above it the list is one JSON array.
INLINE_CANDIDATE_LIMIT = 900


def candidate_clause(column: str, candidates: list[int]) -> tuple[str, list[object]]:
    """The SQL fragment ``column IN (...)`` and its parameters, in the order the fragment uses them.

    ``column`` is interpolated into the SQL, so pass a fixed column name, never user input. Callers treat an
    empty list as "match nothing" before getting here. Duplicates are harmless: ``IN`` is a set test.
    """
    rowids = [int(c) for c in candidates]
    if len(rowids) <= INLINE_CANDIDATE_LIMIT:
        return f"{column} IN ({','.join('?' * len(rowids))})", list(rowids)
    return f"{column} IN (SELECT value FROM json_each(?))", [json.dumps(rowids)]
