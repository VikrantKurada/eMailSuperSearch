"""Hybrid search over the session's indexed 12-thread corpus, plus the pure fusion and snippet helpers."""

import numpy as np
from helpers import paraphrase

from emailsupersearch.diagnostics.corpus import PEOPLE, TOPICS
from emailsupersearch.index.chains import update_chain_row
from emailsupersearch.index.vectors import VectorIndex
from emailsupersearch.ingest.chunker import ChunkText, context_line_for
from emailsupersearch.search import hybrid
from emailsupersearch.search.hybrid import (
    K_EACH,
    PAGE_SIZE,
    _related,
    _snippet,
    cards_for_keys,
    group_chains,
    query_matches,
    rrf,
    search,
    stop_addresses,
)
from emailsupersearch.store import FileRecord
from emailsupersearch.types import Recipient, SearchFilters

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
    grouped = search(indexed_store, embedder, SearchFilters(query="", date_from=0))
    assert r.total_chains == grouped.total_chains > len({m.chain_id for m in r.messages})  # every match, not the page


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


def _no_stop_addresses(monkeypatch):
    """The 12-thread corpus has a handful of people, each on far more than 5% of its messages, so every address would
    be a stop address there; these tests check the linking itself."""
    monkeypatch.setattr(hybrid, "STOP_ADDRESS_SHARE", 1.0)


def test_related_chains_share_participants(indexed_store, embedder, monkeypatch):
    _no_stop_addresses(monkeypatch)
    r = search(indexed_store, embedder, SearchFilters(query="budget"))
    assert any(c.related for c in r.chains)
    for c in r.chains:
        assert len(c.related) <= 3 and all(cid != c.chain_id and subject for cid, subject in c.related)


def test_related_chain_hidden_when_tombstoned(indexed_store, embedder, monkeypatch):
    _no_stop_addresses(monkeypatch)
    store = indexed_store

    def related_ids():
        r = search(store, embedder, SearchFilters(query="budget"), page_size=100)
        return [cid for c in r.chains for cid, _ in c.related]

    target = related_ids()[0]
    keys = [row[0] for row in store.read_conn().execute("SELECT message_key FROM messages WHERE chain_id = ?", (target,))]
    paths = [path for key in keys for path in store.paths_for_message(key)]
    assert paths
    store.tombstone(paths, now=3)
    try:
        assert target not in related_ids()
    finally:
        with store.lock, store.conn:
            store.conn.executemany("UPDATE files SET tombstoned_at = NULL WHERE path = ?", [(p,) for p in paths])


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


def _seed(store, parsed_factory, key, texts, chain_id=None, sender="jane@example.com", recipients=()):
    """One live message under ``key`` with one body chunk per text (after a context line); returns its chunk rowids."""
    store.upsert_message(
        parsed_factory(message_id=f"<{key}@example.com>", sender_addr=sender, recipients=list(recipients)),
        key, "", chain_id, 0,
    )
    store.upsert_file(
        FileRecord(
            path=f"/mail/{key}.eml", size=1, mtime_ns=1, sha256=None, file_id=None, message_key=key,
            parse_status="indexed", status_reason="", last_seen_scan_id=1, tombstoned_at=None,
        )
    )
    line = context_line_for("Jane Doe", None, "Hello", "body")
    return store.replace_chunks(key, "body", [ChunkText(i, f"{line}\n{text}", 9) for i, text in enumerate(texts)])


def test_a_message_with_many_matching_chunks_does_not_crowd_out_the_others(tmp_store, parsed_factory):
    _seed(tmp_store, parsed_factory, "heavy", ["walrus walrus walrus"] * 120)
    for i in range(70):
        _seed(tmp_store, parsed_factory, f"light-{i:02d}", [f"one walrus among many other plain words, number {i}"])
    r = search(tmp_store, None, SearchFilters(query="walrus", group_by_chain=False), page_size=100)
    keys = [m.message_key for m in r.messages]
    # The best K_EACH messages, not the best K_EACH chunks (which would all be the heavy message's).
    assert keys[0] == "heavy" and len(keys) == r.total_messages == K_EACH
    cards, capped = query_matches(tmp_store, None, SearchFilters(query="walrus"), k_each=100)
    assert len(cards) == 71 and not capped  # 190 matching chunks, fewer than the 400 fetched: every match is in
    cards, capped = query_matches(tmp_store, None, SearchFilters(query="walrus"), k_each=10)
    assert [c.message_key for c in cards] == ["heavy"] and capped  # 40 chunks, all the heavy one's: more may match


class _QueryVector:
    """Stands in for the embedder: every text embeds to the same fixed unit vector."""

    id = "fixed"
    dim = 384

    def __init__(self, vector):
        self.vector = vector

    def embed(self, texts, batch_size=32):
        return np.array([self.vector] * len(texts), dtype=np.float32)


def _unit(*weights):
    v = np.zeros(384, dtype=np.float32)
    for i, w in enumerate(weights):
        v[i] = w
    return v / np.linalg.norm(v)


def test_only_the_keyword_list_can_cap_a_query(tmp_store, parsed_factory):
    rowids = [_seed(tmp_store, parsed_factory, f"filler-{i:02d}", [f"Filler text {i}."])[0] for i in range(30)]
    rowids += [_seed(tmp_store, parsed_factory, f"walrus-{i}", ["A walrus was seen."])[0] for i in range(2)]
    VectorIndex(tmp_store.conn).upsert([(rowid, _unit(1, i / 10)) for i, rowid in enumerate(rowids)])
    embedder = _QueryVector(_unit(1))
    # k_each 5: the vector list fills its 20 chunks, the keyword list holds 2 messages: nothing more contains "walrus".
    cards, capped = query_matches(tmp_store, embedder, SearchFilters(query="walrus"), k_each=5)
    assert {"walrus-0", "walrus-1"} <= {c.message_key for c in cards} and len(cards) > 2 and not capped
    # k_each 1: the keyword list keeps 1 of its 2 messages, so more emails contain the word than the result holds.
    _, capped = query_matches(tmp_store, embedder, SearchFilters(query="walrus"), k_each=1)
    assert capped


def test_identifier_query_is_ranked_by_the_keyword_list_alone(tmp_store, parsed_factory):
    exact = _seed(tmp_store, parsed_factory, "z-exact", ["Invoice INV-2031-00042 attached."])
    # A longer text holding the identifier inside a longer one: a weaker trigram hit, but the vector list's favourite.
    near = _seed(tmp_store, parsed_factory, "a-near", ["Old reference INV-2031-000421 from the archive, see the ledger."])
    fillers = [_seed(tmp_store, parsed_factory, f"m-filler-{i}", [f"Filler text {i}."])[0] for i in range(5)]
    vectors = [(near[0], _unit(1)), (exact[0], _unit(-1))]
    vectors += [(rowid, _unit(1, 0.5 + i / 10)) for i, rowid in enumerate(fillers)]
    VectorIndex(tmp_store.conn).upsert(vectors)
    embedder = _QueryVector(_unit(1))
    keyword = search(tmp_store, None, SearchFilters(query="INV-2031-00042", group_by_chain=False))
    assert [m.message_key for m in keyword.messages] == ["z-exact", "a-near"]
    r = search(tmp_store, embedder, SearchFilters(query="INV-2031-00042", group_by_chain=False))
    assert [m.message_key for m in r.messages] == ["z-exact", "a-near"] and r.mode == "hybrid"
    # Fused with the vector list (rank 1 for a-near, last for z-exact), a-near would have won.
    fused = rrf([["z-exact", "a-near"], ["a-near", *[f"m-filler-{i}" for i in range(5)], "z-exact"]])
    assert fused["a-near"] > fused["z-exact"]


def test_related_chains_ignore_an_address_on_most_messages(tmp_store, parsed_factory):
    owner = "owner@example.com"

    def add(key, chain, sender, cc=()):
        recipients = [Recipient("to", "Owner", owner), *(Recipient("cc", "", addr) for addr in cc)]
        _seed(tmp_store, parsed_factory, key, ["text"], chain_id=chain, sender=sender, recipients=recipients)
        update_chain_row(tmp_store, chain)

    for i in range(100):
        add(f"filler-{i:03d}", f"c-filler-{i:03d}", f"person{i}@example.com")
    add("a1", "c-a", "alice@example.com", ["bob@example.com"])
    add("b1", "c-b", "alice@example.com")  # shares alice and the owner with c-a: one address besides the owner
    add("c1", "c-c", "alice@example.com", ["bob@example.com"])  # shares alice and bob with c-a
    conn = tmp_store.read_conn()
    stop = stop_addresses(conn)
    assert owner in stop and "alice@example.com" not in stop
    chains = group_chains(cards_for_keys(conn, ["a1", "b1"]), conn)
    _related(conn, chains)
    related = {c.chain_id: [cid for cid, _ in c.related] for c in chains}
    assert related == {"c-a": ["c-c"], "c-b": []}
    for i in range(10):  # alice is now on 13 of 113 messages: over 5%, and the cache sees the new count
        add(f"alice-{i}", f"c-alice-{i}", "alice@example.com")
    assert "alice@example.com" in stop_addresses(conn)
