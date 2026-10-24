from types import SimpleNamespace

import pytest

from emailsupersearch.ingest.chunker import ChunkText
from emailsupersearch.search.filters import (
    candidate_message_keys,
    chunk_rowids_for,
    count_chains,
    count_messages,
    messages_by_date,
)
from emailsupersearch.store import FileRecord
from emailsupersearch.types import (
    AttachmentText,
    ParsedAttachment,
    Recipient,
    SearchFilters,
)

T = 1_773_705_600
DAY = 86_400


def _add(store, parsed_factory, key, chunk_texts=(), **overrides):
    """Index one message with a live file row and one chunk per text; return its chunk rowids."""
    overrides.setdefault("message_id", f"<{key}@example.com>")
    store.upsert_message(parsed_factory(**overrides), key, "", None, 0)
    store.upsert_file(
        FileRecord(
            path=f"/mail/{key}.eml", size=1, mtime_ns=1, sha256=None, file_id=None, message_key=key,
            parse_status="indexed", status_reason="", last_seen_scan_id=1, tombstoned_at=None,
        )
    )
    chunks = [ChunkText(i, text, len(text.split())) for i, text in enumerate(chunk_texts)]
    return store.replace_chunks(key, "body", chunks)


@pytest.fixture
def seeded(tmp_store, parsed_factory):
    """Four live messages, oldest first, read through the store's read-only connection.

    m1 Alice (mixed-case address) to Bob, cc Carol, bcc Secret; m2 Bob to Alice with a PDF; m3 Carol to Alice,
    cc Dave; m4 Dave to Bob. ``rowids`` maps each key to its chunk rowids.
    """
    pdf = ParsedAttachment(0, "report.pdf", "application/pdf", 10, "pdf")
    rowids = {
        "m1": _add(
            tmp_store, parsed_factory, "m1", ["one a", "one b"],
            sender_addr="Alice@Example.COM", sent_ts=T,
            recipients=[
                Recipient("to", "Bob", "Bob@Example.org"),
                Recipient("cc", "Carol", "carol@example.org"),
                Recipient("bcc", "Secret", "secret@example.org"),
            ],
        ),
        "m2": _add(
            tmp_store, parsed_factory, "m2", ["two a"],
            sender_addr="bob@example.org", sent_ts=T + DAY, attachments=[pdf],
            recipients=[Recipient("to", "Alice", "alice@example.com")],
        ),
        "m3": _add(
            tmp_store, parsed_factory, "m3", ["three a", "three b"],
            sender_addr="carol@example.org", sent_ts=T + 2 * DAY,
            recipients=[Recipient("to", "Alice", "alice@example.com"), Recipient("cc", "Dave", "dave@example.org")],
        ),
        "m4": _add(
            tmp_store, parsed_factory, "m4", ["four a"],
            sender_addr="Dave@Example.org", sent_ts=T + 3 * DAY,
            recipients=[Recipient("to", "Bob", "bob@example.org")],
        ),
    }
    tmp_store.upsert_attachments("m2", [AttachmentText(0, "report.pdf", "pdf", 10, "ok", "text", 1, [])], "none")
    return SimpleNamespace(store=tmp_store, conn=tmp_store.read_conn(), rowids=rowids)


def test_no_filters_returns_none(seeded):
    assert candidate_message_keys(seeded.conn, SearchFilters()) is None
    # query, grouping, conclusion and sort are not candidate filters
    unrelated = SearchFilters(query="renewal", group_by_chain=False, only_with_conclusion=True, sort="date")
    assert candidate_message_keys(seeded.conn, unrelated) is None


def test_from_filter_is_case_insensitive(seeded):
    assert candidate_message_keys(seeded.conn, SearchFilters(from_addrs=["ALICE@example.com"])) == ["m1"]
    assert candidate_message_keys(seeded.conn, SearchFilters(from_addrs=["  dave@EXAMPLE.org "])) == ["m4"]
    both = SearchFilters(from_addrs=["dave@example.org", "alice@example.com"])
    assert candidate_message_keys(seeded.conn, both) == ["m4", "m1"]


def test_recipient_filter_matches_to_and_cc_not_bcc(seeded):
    def keys(addr):
        return candidate_message_keys(seeded.conn, SearchFilters(recipient_addrs=[addr]))

    assert keys("bob@example.org") == ["m4", "m1"]  # "to"; m1 stores it as Bob@Example.org
    assert keys("CAROL@example.org") == ["m1"]  # "cc"
    assert keys("dave@example.org") == ["m3"]  # "cc"
    assert keys("secret@example.org") == []  # "bcc" only


def test_date_range_inclusive(seeded):
    def keys(**bounds):
        return candidate_message_keys(seeded.conn, SearchFilters(**bounds))

    assert keys(date_from=T + DAY, date_to=T + 2 * DAY) == ["m3", "m2"]  # both ends are inclusive
    assert keys(date_from=T + 2 * DAY) == ["m4", "m3"]
    assert keys(date_to=T + DAY) == ["m2", "m1"]
    assert keys(date_from=T + DAY + 1, date_to=T + 2 * DAY - 1) == []


def test_date_bounds_exclude_null_sent_ts(seeded, parsed_factory):
    _add(seeded.store, parsed_factory, "m5", sender_addr="alice@example.com", sent_ts=None)

    def keys(**filters):
        return candidate_message_keys(seeded.conn, SearchFilters(**filters))

    assert keys(from_addrs=["alice@example.com"]) == ["m1", "m5"]  # undated messages sort last
    assert keys(from_addrs=["alice@example.com"], date_from=0) == ["m1"]
    assert keys(from_addrs=["alice@example.com"], date_to=T + 10 * DAY) == ["m1"]
    assert "m5" not in keys(date_from=0)
    assert "m5" not in keys(date_to=T + 10 * DAY)


def test_attachment_kind_filter(seeded):
    def keys(**filters):
        return candidate_message_keys(seeded.conn, SearchFilters(**filters))

    assert keys(attachment_kinds=["pdf"]) == ["m2"]
    assert keys(attachment_kinds=["docx", "pdf"]) == ["m2"]
    assert keys(attachment_kinds=["docx"]) == []
    assert keys(has_attachments=True) == ["m2"]
    assert keys(has_attachments=False) == ["m4", "m3", "m1"]
    assert keys(attachment_kinds=["pdf"], has_attachments=False) == []


def test_filters_combine_with_and(seeded):
    def keys(**filters):
        return candidate_message_keys(seeded.conn, SearchFilters(**filters))

    assert keys(from_addrs=["alice@example.com"], recipient_addrs=["bob@example.org"], date_from=T) == ["m1"]
    assert keys(from_addrs=["alice@example.com"], recipient_addrs=["bob@example.org"], date_from=T + 1) == []
    senders = ["alice@example.com", "bob@example.org"]
    assert keys(from_addrs=senders, recipient_addrs=["alice@example.com"]) == ["m2"]  # m1 is from Alice, not to her
    assert keys(from_addrs=senders, recipient_addrs=["alice@example.com"], has_attachments=False) == []


def test_empty_address_strings_are_ignored(seeded):
    def keys(**filters):
        return candidate_message_keys(seeded.conn, SearchFilters(**filters))

    assert keys(from_addrs=["", "   "]) is None  # nothing left, so the filter is not set
    assert keys(from_addrs=["", "alice@example.com"]) == ["m1"]
    assert keys(recipient_addrs=[" "], date_from=T + 3 * DAY) == ["m4"]  # an empty recipient list does not exclude all


def test_tombstoned_messages_excluded(seeded):
    seeded.store.tombstone(["/mail/m3.eml"], now=1)

    keys = candidate_message_keys(seeded.conn, SearchFilters())
    assert keys == ["m4", "m2", "m1"]  # a list now, not None: the tombstoned message must not surface
    assert candidate_message_keys(seeded.conn, SearchFilters(from_addrs=["carol@example.org"])) == []
    assert candidate_message_keys(seeded.conn, SearchFilters(recipient_addrs=["dave@example.org"])) == []
    assert messages_by_date(seeded.conn, SearchFilters(), 10, 0) == ["m4", "m2", "m1"]


def test_message_with_a_live_and_a_tombstoned_file_stays_visible(seeded):
    seeded.store.upsert_file(
        FileRecord(
            path="/mail/copy-of-m3.eml", size=1, mtime_ns=1, sha256=None, file_id=None, message_key="m3",
            parse_status="indexed", status_reason="", last_seen_scan_id=1, tombstoned_at=None,
        )
    )
    seeded.store.tombstone(["/mail/m3.eml"], now=1)
    assert candidate_message_keys(seeded.conn, SearchFilters()) == ["m4", "m3", "m2", "m1"]


def test_chunk_rowids_for_subset(seeded):
    rowids = seeded.rowids
    assert chunk_rowids_for(seeded.conn, None) is None
    assert chunk_rowids_for(seeded.conn, []) == []
    assert chunk_rowids_for(seeded.conn, ["no-such-key"]) == []
    assert chunk_rowids_for(seeded.conn, ["m3", "m1"]) == sorted(rowids["m1"] + rowids["m3"])
    assert chunk_rowids_for(seeded.conn, ["m2"]) == rowids["m2"]


def test_messages_by_date_orders_pages_and_filters(seeded):
    everything = SearchFilters()
    assert messages_by_date(seeded.conn, everything, 2, 0) == ["m4", "m3"]
    assert messages_by_date(seeded.conn, everything, 2, 2) == ["m2", "m1"]
    assert messages_by_date(seeded.conn, everything, 10, 4) == []
    assert messages_by_date(seeded.conn, everything, 0, 0) == []
    assert messages_by_date(seeded.conn, everything, -1, 0) == []
    senders = SearchFilters(from_addrs=["ALICE@example.com", "bob@example.org"])
    assert messages_by_date(seeded.conn, senders, 10, 0) == ["m2", "m1"]
    assert messages_by_date(seeded.conn, senders, 1, 1) == ["m1"]


def test_count_messages_and_chains(seeded):
    with seeded.store.lock, seeded.store.conn:
        seeded.store.conn.execute("UPDATE messages SET chain_id = 'c1' WHERE message_key IN ('m1', 'm2')")
    everything = SearchFilters()
    assert count_messages(seeded.conn, everything) == 4
    assert count_chains(seeded.conn, everything) == 3  # m1 and m2 share c1; m3 and m4 have no chain, one each
    assert count_chains(seeded.conn, SearchFilters(from_addrs=["bob@example.org", "carol@example.org"])) == 2
    seeded.store.tombstone(["/mail/m3.eml"], now=1)
    assert count_messages(seeded.conn, everything) == 3 and count_chains(seeded.conn, everything) == 2
