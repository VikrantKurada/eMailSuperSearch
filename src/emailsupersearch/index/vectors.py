"""Chunk embeddings in a sqlite-vec ``vec0`` table, searchable with a candidate pre-filter."""

import sqlite3
import threading

import numpy as np
import sqlite_vec

# Above this many candidates the rowids go through a temp table instead of bound parameters.
INLINE_CANDIDATE_LIMIT = 900
# sqlite-vec rejects a KNN query with a larger ``k``.
MAX_K = 4096

_CREATE_CANDIDATES = "CREATE TEMP TABLE IF NOT EXISTS vec_candidates(rowid INTEGER PRIMARY KEY)"


def _blob(vector: np.ndarray, dim: int) -> bytes:
    if vector.shape != (dim,):
        raise ValueError(f"expected a vector of shape ({dim},), got {vector.shape}")
    return sqlite_vec.serialize_float32(vector.astype(np.float32).tolist())


class VectorIndex:
    """Nearest-neighbour index over ``chunks_vec``, keyed by chunk rowid."""

    def __init__(self, conn: sqlite3.Connection, dim: int = 384) -> None:
        self.conn = conn
        self.dim = dim
        # The temp candidate table is per connection, so concurrent large-candidate searches must not interleave.
        self._candidates_lock = threading.Lock()

    def upsert(self, rows: list[tuple[int, np.ndarray]]) -> None:
        """Store each vector under its chunk rowid, replacing any existing one, in one transaction."""
        blobs = [(int(rowid), _blob(vector, self.dim)) for rowid, vector in rows]
        with self.conn:
            self.conn.executemany("DELETE FROM chunks_vec WHERE chunk_rowid = ?", [(rowid,) for rowid, _ in blobs])
            self.conn.executemany("INSERT INTO chunks_vec(chunk_rowid, embedding) VALUES (?, ?)", blobs)

    def delete(self, chunk_rowids: list[int]) -> None:
        with self.conn:
            self.conn.executemany("DELETE FROM chunks_vec WHERE chunk_rowid = ?", [(int(r),) for r in chunk_rowids])

    def knn(self, query: np.ndarray, k: int, candidates: list[int] | None) -> list[tuple[int, float]]:
        """Up to ``k`` nearest chunks as ``(chunk_rowid, similarity)``, most similar first.

        Similarity is ``1 - distance / 2`` in [0, 1], which assumes unit-length vectors. ``candidates``
        restricts the search to those chunk rowids before the nearest-neighbour scan; an empty list
        matches nothing and ``None`` searches everything.
        """
        blob = _blob(query, self.dim)
        k = min(k, MAX_K)
        if k < 1 or (candidates is not None and not candidates):
            return []
        sql = "SELECT chunk_rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
        if candidates is None:
            rows = self.conn.execute(sql + " ORDER BY distance", (blob, k)).fetchall()
        elif len(candidates) <= INLINE_CANDIDATE_LIMIT:
            marks = ",".join("?" * len(candidates))
            params = (blob, k, *(int(c) for c in candidates))
            rows = self.conn.execute(f"{sql} AND chunk_rowid IN ({marks}) ORDER BY distance", params).fetchall()
        else:
            rows = self._knn_via_temp_table(sql, blob, k, candidates)
        return [(int(rowid), min(1.0, max(0.0, 1.0 - distance / 2.0))) for rowid, distance in rows]

    def _knn_via_temp_table(self, sql: str, blob: bytes, k: int, candidates: list[int]) -> list[sqlite3.Row]:
        with self._candidates_lock, self.conn:  # leaving the block commits, so no read snapshot lingers
            self.conn.execute(_CREATE_CANDIDATES)
            self.conn.execute("DELETE FROM vec_candidates")
            self.conn.executemany("INSERT OR IGNORE INTO vec_candidates(rowid) VALUES (?)", [(int(c),) for c in candidates])
            return self.conn.execute(
                f"{sql} AND chunk_rowid IN (SELECT rowid FROM vec_candidates) ORDER BY distance", (blob, k)
            ).fetchall()

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0])
