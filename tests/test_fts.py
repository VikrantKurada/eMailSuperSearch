import pytest

from emailsupersearch.index.fts import (
    bm25_search,
    keyword_search,
    looks_like_identifier,
    sanitize_query,
    trigram_search,
)
from emailsupersearch.ingest.chunker import ChunkText


@pytest.mark.parametrize("q", ['"unterminated', "a*b", "col:val", "-neg (x", "NEAR(a b)", "   ", ""])
def test_sanitize_query_never_raises(store_with_chunks, q):
    keyword_search(store_with_chunks.conn, q, 10, None)


def test_sanitize_query_quotes_every_token():
    assert sanitize_query('  INV-2024-00017   "renewal terms ') == '"INV-2024-00017" "renewal" "terms"'
    assert sanitize_query('" "" a"b') == '"ab"'
    assert sanitize_query('  "" ') == ""


def test_bm25_ranks_exact_identifier_first(store_with_chunks):
    rows = bm25_search(store_with_chunks.conn, "INV-2024-00017", 10, None)
    assert rows and rows[0][0] == store_with_chunks.invoice_rowid


def test_trigram_finds_partial_identifier(store_with_chunks):
    rows = trigram_search(store_with_chunks.conn, "2024-000", 10, None)
    assert store_with_chunks.invoice_rowid in [r for r, _ in rows]


def test_trigram_term_shorter_than_three_characters_matches_nothing(store_with_chunks):
    assert trigram_search(store_with_chunks.conn, " 20 ", 10, None) == []


def test_candidates_restrict_results(store_with_chunks):
    allowed = store_with_chunks.rowids[:2]
    rows = bm25_search(store_with_chunks.conn, "renewal", 10, allowed)
    assert rows and all(r in allowed for r, _ in rows)


def test_empty_candidates_return_nothing(store_with_chunks):
    assert keyword_search(store_with_chunks.conn, "renewal", 10, []) == []


def test_looks_like_identifier():
    assert looks_like_identifier("INV-2024-00017") and not looks_like_identifier("renewal terms")


def test_keyword_search_merges_bm25_and_trigram(store_with_chunks):
    rows = keyword_search(store_with_chunks.conn, "INV-2024-00017", 10, None)
    assert [r for r, _ in rows].count(store_with_chunks.invoice_rowid) == 1
    assert dict(rows)[store_with_chunks.invoice_rowid] > 0


def test_large_candidate_list_uses_json_each_on_read_only_connection(tmp_store):
    chunks = [ChunkText(i, f"renewal note {i}", 3) for i in range(1000)]
    rowids = tmp_store.replace_chunks("m-many", "body", chunks)
    conn = tmp_store.read_conn()  # PRAGMA query_only=1: no TEMP table can be written here
    for search in (bm25_search, trigram_search):
        rows = search(conn, "renewal", 10, rowids)
        assert len(rows) == 10 and all(r in rowids for r, _ in rows)
        allowed = rowids[50:]  # 950 candidates, still over the inline limit; the other 50 must stay out
        rows = search(conn, "renewal", 2000, allowed)
        assert {r for r, _ in rows} == set(allowed)


def test_sentence_final_word_is_found(tmp_store):
    rowids = tmp_store.replace_chunks("m-end", "body", [ChunkText(0, "Please read the summary.", 4)])
    assert [r for r, _ in bm25_search(tmp_store.read_conn(), "summary", 10, None)] == rowids


def test_dotted_identifier_still_matches_as_phrase(tmp_store):
    rowids = tmp_store.replace_chunks("m-dots", "body", [ChunkText(0, "see v1.2.3 and example.com", 4)])
    conn = tmp_store.read_conn()
    assert [r for r, _ in keyword_search(conn, "v1.2.3", 10, None)] == rowids
    assert [r for r, _ in keyword_search(conn, "example.com", 10, None)] == rowids
    assert keyword_search(conn, "com.example", 10, None) == []  # the phrase "com example" is in the wrong order
