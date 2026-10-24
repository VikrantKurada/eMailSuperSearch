import numpy as np
import pytest

from emailsupersearch.db.connection import connect
from emailsupersearch.db.schema import migrate
from emailsupersearch.index.vectors import VectorIndex

DIM = 384


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "v.sqlite")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def index(conn):
    return VectorIndex(conn)


def unit_vectors(n: int, seed: int = 0) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal((n, DIM)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_upsert_and_count(index):
    vecs = unit_vectors(3)
    assert index.count() == 0
    index.upsert([(1, vecs[0]), (2, vecs[1]), (3, vecs[2])])
    assert index.count() == 3
    index.upsert([(2, vecs[2])])  # replaces rowid 2 rather than adding a fourth row
    assert index.count() == 3


def test_upsert_replaces_the_stored_vector(index):
    vecs = unit_vectors(2)
    index.upsert([(7, vecs[0])])
    index.upsert([(7, vecs[1])])
    (rowid, similarity), = index.knn(vecs[1], k=1, candidates=None)
    assert rowid == 7 and similarity == pytest.approx(1.0, abs=1e-4)


def test_upsert_rejects_wrong_dimension(index):
    with pytest.raises(ValueError):
        index.upsert([(1, np.zeros(DIM - 1, dtype=np.float32))])
    assert index.count() == 0


def test_knn_returns_nearest_first(index):
    vecs = unit_vectors(3)  # random 384-d unit vectors are near-orthogonal
    index.upsert([(10, vecs[0]), (20, vecs[1]), (30, vecs[2])])
    query = vecs[1] * 0.9 + vecs[0] * 0.1
    query /= np.linalg.norm(query)
    hits = index.knn(query, k=3, candidates=None)
    assert [rowid for rowid, _ in hits] == [20, 10, 30]
    similarities = [s for _, s in hits]
    assert similarities == sorted(similarities, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in similarities)
    assert similarities[0] > 0.9


def test_knn_similarity_of_identical_vector_is_one(index):
    vecs = unit_vectors(2)
    index.upsert([(1, vecs[0]), (2, vecs[1])])
    (_, top), _ = index.knn(vecs[0], k=2, candidates=None)
    assert top == pytest.approx(1.0, abs=1e-4)


def test_knn_candidates_filter(index):
    vecs = unit_vectors(3)
    index.upsert([(10, vecs[0]), (20, vecs[1]), (30, vecs[2])])
    hits = index.knn(vecs[0], k=3, candidates=[20, 30])
    rowids = [rowid for rowid, _ in hits]
    assert 10 not in rowids and set(rowids) == {20, 30}


def brute_force(vecs: np.ndarray, query: np.ndarray, candidates: list[int], k: int) -> list[int]:
    """Chunk rowids (index + 1) of the k candidates nearest the query by L2 distance."""
    ordered = sorted(candidates, key=lambda rowid: float(np.linalg.norm(vecs[rowid - 1] - query)))
    return ordered[:k]


def test_knn_small_candidate_set_matches_brute_force(index):
    vecs = unit_vectors(500)
    index.upsert([(i + 1, vecs[i]) for i in range(500)])
    candidates = [11, 90, 91, 250, 499]  # filtering happens before the scan, so all five are reachable
    query = unit_vectors(1, seed=5)[0]
    hits = index.knn(query, k=5, candidates=candidates)
    assert [rowid for rowid, _ in hits] == brute_force(vecs, query, candidates, 5)


def test_knn_with_many_candidates_uses_temp_table(index, conn):
    vecs = unit_vectors(1001)
    index.upsert([(i + 1, vecs[i]) for i in range(1001)])
    candidates = list(range(2, 1002))  # 1000 candidates; rowid 1, the exact match, is excluded
    hits = index.knn(vecs[0], k=5, candidates=candidates)
    assert [rowid for rowid, _ in hits] == brute_force(vecs, vecs[0], candidates, 5)
    assert conn.execute("SELECT count(*) FROM vec_candidates").fetchone()[0] == 1000
    index.knn(vecs[0], k=5, candidates=candidates[:950])  # the temp table is refilled, not appended to
    assert conn.execute("SELECT count(*) FROM vec_candidates").fetchone()[0] == 950
    assert not conn.in_transaction


def test_knn_tolerates_duplicate_candidates_beyond_the_inline_limit(index):
    vecs = unit_vectors(3)
    index.upsert([(1, vecs[0]), (2, vecs[1]), (3, vecs[2])])
    hits = index.knn(vecs[0], k=3, candidates=[1, 2, 3] * 400)
    assert hits[0][0] == 1 and len(hits) == 3


def test_knn_empty_candidates_returns_empty(index):
    index.upsert([(1, unit_vectors(1)[0])])
    assert index.knn(unit_vectors(1, seed=1)[0], k=5, candidates=[]) == []


def test_knn_k_larger_than_row_count_returns_what_exists(index):
    vecs = unit_vectors(2)
    index.upsert([(1, vecs[0]), (2, vecs[1])])
    assert len(index.knn(vecs[0], k=50, candidates=None)) == 2
    assert len(index.knn(vecs[0], k=50, candidates=[1, 2])) == 2


def test_delete_removes_rows(index):
    vecs = unit_vectors(3)
    index.upsert([(1, vecs[0]), (2, vecs[1]), (3, vecs[2])])
    index.delete([1, 3])
    assert index.count() == 1
    assert [rowid for rowid, _ in index.knn(vecs[0], k=5, candidates=None)] == [2]
    index.delete([])  # nothing to do
    index.delete([99])  # absent rowid is ignored
    assert index.count() == 1


def test_knn_on_empty_index_returns_empty(index):
    assert index.knn(unit_vectors(1)[0], k=5, candidates=None) == []
    assert index.knn(unit_vectors(1)[0], k=5, candidates=[1, 2]) == []


def test_knn_caps_k_at_the_sqlite_vec_limit(index):
    vecs = unit_vectors(2)
    index.upsert([(1, vecs[0]), (2, vecs[1])])
    assert len(index.knn(vecs[0], k=100_000, candidates=None)) == 2


def test_knn_non_positive_k_returns_empty(index):
    vecs = unit_vectors(1)
    index.upsert([(1, vecs[0])])
    assert index.knn(vecs[0], k=0, candidates=None) == []


def test_knn_rejects_wrong_query_dimension(index):
    with pytest.raises(ValueError):
        index.knn(np.zeros(DIM + 1, dtype=np.float32), k=1, candidates=None)


def test_delete_handles_more_rowids_than_one_statement_could_bind(index):
    vecs = unit_vectors(3)
    index.upsert([(i, vecs[i % 3]) for i in range(1, 41)])
    index.delete(list(range(1, 40_000)))
    assert index.count() == 0


def test_rowids_may_be_numpy_integers(index):
    vecs = unit_vectors(2)
    index.upsert([(np.int64(5), vecs[0]), (np.int64(6), vecs[1])])
    assert [rowid for rowid, _ in index.knn(vecs[0], k=2, candidates=[5, 6])] == [5, 6]
    index.delete([np.int64(5)])
    assert index.count() == 1
