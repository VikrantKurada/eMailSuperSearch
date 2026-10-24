"""Hybrid search over the session's indexed 12-thread corpus, plus the pure fusion and snippet helpers."""

from helpers import paraphrase

from emailsupersearch.diagnostics.corpus import PEOPLE, TOPICS
from emailsupersearch.ingest.chunker import ChunkText, context_line_for
from emailsupersearch.search.hybrid import PAGE_SIZE, _snippet, rrf, search
from emailsupersearch.store import FileRecord
from emailsupersearch.types import SearchFilters

INVOICE = "INV-2024-00017"


def _all_cards(r):
    """Every message card of a response, whether grouped into chains or not."""
    return r.messages + [m for c in r.chains for m in c.messages]


def test_rrf_scores():
    s = rrf([[1, 2, 3], [3, 1]])
    assert s[1] > s[2] and abs(rrf([[1]])[1] - 1 / 61) < 1e-9


def test_identifier_query_finds_invoice_message(indexed_store, embedder):
    r = search(indexed_store, embedder, SearchFilters(query=INVOICE, group_by_chain=False))
    assert INVOICE in r.messages[0].snippet


def test_semantic_query_ranks_topic_thread_first(indexed_store, embedder):
    subject, body, _ = TOPICS[4]
    query = paraphrase(body)
    assert query not in body  # at least one word was swapped
    r = search(indexed_store, embedder, SearchFilters(query=query))
    assert r.chains[0].subject.lower().endswith(subject.lower())


def test_filters_narrow_results(indexed_store, embedder):
    addr = PEOPLE[0].addr
    everyone = search(indexed_store, embedder, SearchFilters(query="renewal", group_by_chain=False))
    r = search(indexed_store, embedder, SearchFilters(query="renewal", from_addrs=[addr], group_by_chain=False))
    assert r.messages and r.total_messages < everyone.total_messages
    assert all(m.sender.addr == addr for m in r.messages)


def test_group_by_chain_groups_and_pages(indexed_store, embedder):
    r = search(indexed_store, embedder, SearchFilters(query="renewal"), page_size=2)
    assert len(r.chains) <= 2 <= r.total_chains
    assert all(m.chain_id == c.chain_id for c in r.chains for m in c.messages)
    second = search(indexed_store, embedder, SearchFilters(query="renewal"), page=1, page_size=2)
    assert second.chains and {c.chain_id for c in r.chains}.isdisjoint(c.chain_id for c in second.chains)


def test_sort_by_date_orders_cards_desc(indexed_store, embedder):
    r = search(indexed_store, embedder, SearchFilters(query="meeting", sort="date"))
    ts = [c.last_ts for c in r.chains]
    assert ts and ts == sorted(ts, reverse=True)
    flat = search(indexed_store, embedder, SearchFilters(query="meeting", sort="date", group_by_chain=False))
    sent = [m.sent_ts for m in flat.messages]
    assert sent and sent == sorted(sent, reverse=True)


def test_keyword_only_mode_without_embedder(indexed_store):
    r = search(indexed_store, None, SearchFilters(query="renewal"))
    assert r.mode == "keyword_only" and r.chains


def test_filter_only_search_returns_by_date(indexed_store, embedder):
    r = search(indexed_store, embedder, SearchFilters(query="", date_from=0, group_by_chain=False))
    ts = [m.sent_ts for m in r.messages]
    assert ts and ts == sorted(ts, reverse=True)
    assert r.total_messages > len(r.messages) == PAGE_SIZE
    assert all(m.score == 0.0 and m.snippet == "" for m in r.messages)


def test_tombstoned_message_hidden_from_search_before_purge(indexed_store, embedder):
    store = indexed_store
    key = search(store, embedder, SearchFilters(query=INVOICE, group_by_chain=False)).messages[0].message_key
    paths = store.paths_for_message(key)
    assert len(paths) == 1
    store.tombstone(paths, now=2)
    try:
        for f in (
            SearchFilters(query=INVOICE, group_by_chain=False),
            SearchFilters(query=INVOICE),
            SearchFilters(query="", group_by_chain=False),
        ):
            r = search(store, embedder, f, page_size=1000)
            assert key not in {m.message_key for m in _all_cards(r)}
    finally:
        with store.lock, store.conn:
            store.conn.execute("UPDATE files SET tombstoned_at = NULL WHERE path = ?", (paths[0],))


def test_related_chains_share_participants(indexed_store, embedder):
    r = search(indexed_store, embedder, SearchFilters(query="budget"))
    assert any(c.related for c in r.chains)
    for c in r.chains:
        assert len(c.related) <= 3 and all(cid != c.chain_id and subject for cid, subject in c.related)


def test_snippet_strips_context_line_and_is_bounded(indexed_store):
    line = context_line_for("Alice Morgan", 0, "Printer lease renewal", "body")
    assert _snippet(line + "\nPlease  confirm\n\nby Friday. ") == "Please confirm by Friday."
    assert _snippet(line + "\n" + "x" * 300) == "x" * 240 + "…"
    r = search(indexed_store, None, SearchFilters(query="renewal", group_by_chain=False), page_size=100)
    assert r.messages
    assert all(m.snippet and len(m.snippet) <= 241 and not m.snippet.startswith("From ") for m in r.messages)


def test_live_filter_drops_message_without_live_path(tmp_store, parsed_factory):
    line = context_line_for("Jane Doe", None, "Hello", "body")
    for key in ("live", "orphan"):
        tmp_store.upsert_message(parsed_factory(message_id=f"<{key}@example.com>"), key, "", None, 0)
        tmp_store.replace_chunks(key, "body", [ChunkText(0, line + "\nThe warehouse inventory is ready.", 9)])
    tmp_store.upsert_file(
        FileRecord(
            path="/mail/live.eml", size=1, mtime_ns=1, sha256=None, file_id=None, message_key="live",
            parse_status="indexed", status_reason="", last_seen_scan_id=1, tombstoned_at=None,
        )
    )
    for grouped in (False, True):
        r = search(tmp_store, None, SearchFilters(query="warehouse", group_by_chain=grouped))
        cards = _all_cards(r)
        assert [m.message_key for m in cards] == ["live"]
        assert cards[0].paths == ["/mail/live.eml"] and r.total_messages == 1
