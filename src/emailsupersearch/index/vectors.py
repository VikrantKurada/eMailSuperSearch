"""Chunk embeddings in a sqlite-vec ``vec0`` table, searchable with a candidate pre-filter."""

import sqlite3

import numpy as np
import sqlite_vec

from emailsupersearch.index.candidates import candidate_clause

# sqlite-vec rejects a KNN query with a larger ``k``.
MAX_K = 4096


def serialize_vector(vector: np.ndarray, dim: int) -> bytes:
    """The ``chunks_vec`` blob of one vector; raises ``ValueError`` for a wrong shape or a NaN or infinity."""
    if vector.shape != (dim,):
        raise ValueError(f"expected a vector of shape ({dim},), got {vector.shape}")
    if not np.isfinite(vector).all():
        raise ValueError("vector contains NaN or infinity")
    return sqlite_vec.serialize_float32(vector.astype(np.float32).tolist())


def _similarity(distance: float | None) -> float:
    """``1 - distance / 2`` clamped to [0, 1]; sqlite-vec reports a NULL distance for a row holding NaN."""
    if distance is None:
        return 0.0
    return min(1.0, max(0.0, 1.0 - distance / 2.0))


class VectorIndex:
    """Nearest-neighbour index over ``chunks_vec``, keyed by chunk rowid."""

    def __init__(self, conn: sqlite3.Connection, dim: int = 384) -> None:
        self.conn = conn
        self.dim = dim

    def upsert(self, rows: list[tuple[int, np.ndarray]]) -> None:
        """Store each vector under its chunk rowid, replacing any existing one, in one transaction."""
        blobs = [(int(rowid), serialize_vector(vector, self.dim)) for rowid, vector in rows]
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
        blob = serialize_vector(query, self.dim)
        k = min(k, MAX_K)
        if k < 1 or (candidates is not None and not candidates):
            return []
        sql = "SELECT chunk_rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
        if candidates is None:
            rows = self.conn.execute(sql + " ORDER BY distance", (blob, k)).fetchall()
        else:
            clause, params = candidate_clause("chunk_rowid", candidates)
            rows = self.conn.execute(f"{sql} AND {clause} ORDER BY distance", (blob, k, *params)).fetchall()
        hits = [(int(rowid), _similarity(distance)) for rowid, distance in rows]
        hits.sort(key=lambda hit: -hit[1])  # SQLite orders a NULL distance first; a corrupt row must not outrank real hits
        return hits

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0])
