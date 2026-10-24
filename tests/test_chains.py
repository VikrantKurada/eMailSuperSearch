import dataclasses
import json

from emailsupersearch.index.chains import (
    assign_chain,
    chain_depth,
    thread_guid,
    update_chain_row,
)
from emailsupersearch.ingest.identity import message_key
from emailsupersearch.ingest.parse import parse_file
from emailsupersearch.types import Recipient

DAY = 86400
T0 = 1773705600  # an arbitrary fixed send time


def _ci(fill: int, depth: int = 0) -> bytes:
    """A conversation index: 6 header bytes, a 16-byte thread GUID of ``fill`` bytes, 5 bytes per reply."""
    return bytes(6) + bytes([fill]) * 16 + bytes(5 * depth)


def _guid(fill: int) -> str:
    return f"{fill:02x}" * 16


def _store(store, m):
    """Do what the ingest pipeline does for one message: assign its chain, write it, refresh the chain row."""
    key = message_key(m)
    chain_id, depth = assign_chain(store, m, key)
    store.upsert_message(m, key, new_text=m.body_text, chain_id=chain_id, chain_depth=depth)
    update_chain_row(store, chain_id)
    return key, chain_id, depth


def _chain_row(store, chain_id):
    return store.conn.execute("SELECT * FROM chains WHERE chain_id = ?", (chain_id,)).fetchone()


def test_thread_guid_and_depth_from_synthetic_index():
    guid = bytes(range(1, 17))
    index = bytes(6) + guid + bytes(5 * 2)  # 22 + 5*2 bytes
    assert len(index) == 32
    assert thread_guid(index) == guid.hex()
    assert chain_depth(index) == 2

    assert thread_guid(index[:22]) == guid.hex()
    assert chain_depth(index[:22]) == 0

    for short in (None, b"", bytes(21)):
        assert thread_guid(short) is None
        assert chain_depth(short) == 0


def test_assign_chain_by_in_reply_to(tmp_store, corpus_dir):
    parsed = {}
    for path in sorted(corpus_dir.glob("*.eml")):
        m = parse_file(path)
        parsed[m.message_id] = m
    child = next(m for m in parsed.values() if m.in_reply_to is not None)
    parent = parsed[child.in_reply_to]
    assert child.conversation_index is None
    # A different subject keeps the subject rule from being what joins the two.
    child = dataclasses.replace(child, subject="Something else entirely")

    _, parent_chain, parent_depth = _store(tmp_store, parent)
    child_key, child_chain, child_depth = _store(tmp_store, child)

    assert parent_chain.startswith("c-")
    assert child_chain == parent_chain
    assert child_depth == parent_depth + 1
    assert tmp_store.message(child_key)["chain_id"] == parent_chain
    assert _chain_row(tmp_store, parent_chain)["message_count"] == 2


def test_assign_chain_by_references_picks_nearest_ancestor(tmp_store, parsed_factory):
    far = parsed_factory(message_id="<far@example.com>", subject="Far")
    near = parsed_factory(message_id="<near@example.com>", subject="Near", sent_ts=T0 + 90 * DAY)
    tmp_store.upsert_message(far, message_key(far), far.body_text, "c-far", 1)
    tmp_store.upsert_message(near, message_key(near), near.body_text, "c-near", 3)

    last_reference = parsed_factory(
        message_id="<r1@example.com>", subject="Unrelated one", references=["<far@example.com>", "<near@example.com>"]
    )
    assert assign_chain(tmp_store, last_reference, message_key(last_reference)) == ("c-near", 4)

    in_reply_to_wins = parsed_factory(
        message_id="<r2@example.com>",
        subject="Unrelated two",
        in_reply_to="<far@example.com>",
        references=["<far@example.com>", "<near@example.com>"],
    )
    assert assign_chain(tmp_store, in_reply_to_wins, message_key(in_reply_to_wins)) == ("c-far", 2)

    unknown = parsed_factory(
        message_id="<r3@example.com>", subject="Unrelated three", in_reply_to="<nobody@example.com>"
    )
    chain_id, depth = assign_chain(tmp_store, unknown, message_key(unknown))
    assert (chain_id, depth) == ("c-" + message_key(unknown)[:16], 0)


def test_assign_chain_prefers_conversation_index(tmp_store, parsed_factory):
    other = parsed_factory(message_id="<other@example.com>", subject="Elsewhere")
    _store(tmp_store, other)

    first = parsed_factory(message_id="<ci-1@example.com>", subject="Topic A", conversation_index=_ci(7))
    second = parsed_factory(
        message_id="<ci-2@example.com>",
        subject="A completely different subject",
        recipients=[Recipient("to", "Zed", "zed@example.net")],
        conversation_index=_ci(7, depth=1),
        in_reply_to="<other@example.com>",  # the conversation index outranks the headers
    )

    _, chain_a, depth_a = _store(tmp_store, first)
    _, chain_b, depth_b = _store(tmp_store, second)

    assert chain_a == chain_b == "t-" + _guid(7)
    assert (depth_a, depth_b) == (0, 1)


def test_assign_chain_by_subject_and_participants_within_30_days(tmp_store, parsed_factory):
    first = parsed_factory(message_id="<s-1@example.com>", subject="Budget review", sent_ts=T0)
    reply = parsed_factory(
        message_id="<s-2@example.com>",
        subject="Re: Budget review",
        sender_name="Bob",
        sender_addr="BOB@example.org",
        recipients=[Recipient("to", "Jane Doe", "jane@example.com")],
        sent_ts=T0 + 30 * DAY,  # exactly 30 days later is still inside the window
    )
    later = parsed_factory(
        message_id="<s-3@example.com>",
        subject="RE: RE: Budget  review",
        recipients=[Recipient("cc", "Bob", "bob@example.org")],
        sent_ts=T0 + 59 * DAY,  # 29 days after the reply, 59 after the first message
    )

    _, chain_1, depth_1 = _store(tmp_store, first)
    _, chain_2, depth_2 = _store(tmp_store, reply)
    _, chain_3, depth_3 = _store(tmp_store, later)

    assert chain_1.startswith("c-")
    assert chain_2 == chain_1
    assert chain_3 == chain_1
    assert (depth_1, depth_2, depth_3) == (0, 1, 2)
    assert _chain_row(tmp_store, chain_1)["message_count"] == 3


def test_unrelated_message_gets_new_chain(tmp_store, parsed_factory):
    first = parsed_factory(message_id="<unrelated-0001@example.com>", subject="Budget review", sent_ts=T0)
    other_subject = parsed_factory(message_id="<unrelated-0002@example.com>", subject="Lunch", sent_ts=T0 + DAY)
    other_people = parsed_factory(
        message_id="<unrelated-0003@example.com>",
        subject="Budget review",
        sender_name="Carol",
        sender_addr="carol@example.net",
        recipients=[Recipient("to", "Dave", "dave@example.net")],
        sent_ts=T0 + 2 * DAY,
    )
    too_late = parsed_factory(message_id="<unrelated-0004@example.com>", subject="Budget review", sent_ts=T0 + 31 * DAY)

    _, chain_1, _ = _store(tmp_store, first)
    chains = {chain_1}
    for m in (other_subject, other_people, too_late):
        key, chain_id, depth = _store(tmp_store, m)
        assert chain_id == "c-" + key[:16]
        assert depth == 0
        chains.add(chain_id)

    assert len(chains) == 4
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM chains").fetchone()[0] == 4


def test_subject_rule_ignores_empty_subject_and_missing_date(tmp_store, parsed_factory):
    blank = parsed_factory(message_id="<blank-1@example.com>", subject="", sent_ts=T0)
    prefix_only = parsed_factory(message_id="<blank-2@example.com>", subject="Re:", sent_ts=T0 + DAY)
    _, blank_chain, _ = _store(tmp_store, blank)
    key, prefix_chain, depth = _store(tmp_store, prefix_only)
    assert prefix_chain != blank_chain
    assert (prefix_chain, depth) == ("c-" + key[:16], 0)

    dated = parsed_factory(message_id="<dated-1@example.com>", subject="Plan", sent_ts=T0)
    undated = parsed_factory(message_id="<dated-2@example.com>", subject="Plan", sent_ts=None)
    _, dated_chain, _ = _store(tmp_store, dated)
    key, undated_chain, depth = _store(tmp_store, undated)
    assert (undated_chain, depth) == ("c-" + key[:16], 0)
    assert undated_chain != dated_chain

    # The undated message is no candidate for later ones either.
    follow_up = parsed_factory(message_id="<dated-3@example.com>", subject="Re: Plan", sent_ts=T0 + DAY)
    _, follow_chain, follow_depth = _store(tmp_store, follow_up)
    assert (follow_chain, follow_depth) == (dated_chain, 1)


def test_update_chain_row_counts_and_deletes_empty_chain(tmp_store, parsed_factory):
    guid = 9
    late = parsed_factory(
        message_id="<u-late@example.com>",
        subject="Re: Printer lease",
        sender_name="Bob",
        sender_addr="bob@example.org",
        recipients=[Recipient("to", "Jane Doe", "jane@example.com")],
        sent_ts=T0 + 2 * DAY,
        conversation_index=_ci(guid, 2),
    )
    root = parsed_factory(
        message_id="<u-root@example.com>",
        subject="Printer  lease",
        recipients=[Recipient("to", "Bob", "bob@example.org"), Recipient("cc", "Carol", "carol@example.net")],
        sent_ts=T0,
        conversation_index=_ci(guid),
    )
    undated = parsed_factory(
        message_id="<u-undated@example.com>",
        subject="Re: Printer lease",
        sent_ts=None,
        conversation_index=_ci(guid, 1),
    )
    keys = {}
    for name, m in (("late", late), ("root", root), ("undated", undated)):
        keys[name], chain_id, _ = _store(tmp_store, m)
        assert chain_id == "t-" + _guid(guid)
    chain_id = "t-" + _guid(guid)

    row = _chain_row(tmp_store, chain_id)
    assert row["message_count"] == 3
    assert row["first_ts"] == T0
    assert row["last_ts"] == T0 + 2 * DAY
    assert row["root_message_key"] == keys["root"]
    assert row["subject"] == "printer lease"
    assert json.loads(row["participants_json"]) == {
        "from": [{"name": "Jane Doe", "addr": "jane@example.com"}, {"name": "Bob", "addr": "bob@example.org"}],
        "to": [{"name": "Bob", "addr": "bob@example.org"}, {"name": "Jane Doe", "addr": "jane@example.com"}],
        "cc": [{"name": "Carol", "addr": "carol@example.net"}],
    }

    tmp_store.delete_message(keys["root"])
    update_chain_row(tmp_store, chain_id)
    row = _chain_row(tmp_store, chain_id)
    assert row["message_count"] == 2
    assert row["root_message_key"] == keys["late"]  # the undated message sorts after the dated one
    assert row["first_ts"] == row["last_ts"] == T0 + 2 * DAY

    tmp_store.delete_message(keys["late"])
    tmp_store.delete_message(keys["undated"])
    update_chain_row(tmp_store, chain_id)
    assert _chain_row(tmp_store, chain_id) is None

    update_chain_row(tmp_store, "t-never-existed")  # nothing to delete, nothing to raise
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM chains").fetchone()[0] == 0


def test_update_chain_row_participants_distinct_by_address(tmp_store, parsed_factory):
    first = parsed_factory(
        message_id="<p-1@example.com>",
        subject="Contract",
        sender_name="Jane Doe",
        sender_addr="jane@example.com",
        recipients=[
            Recipient("to", "Bob", "bob@example.org"),
            Recipient("cc", "Carol", "carol@example.net"),
            Recipient("bcc", "Eve", "eve@example.net"),
        ],
        sent_ts=T0,
    )
    second = parsed_factory(
        message_id="<p-2@example.com>",
        subject="Re: Contract",
        sender_name="J. Doe",
        sender_addr="JANE@Example.com",
        recipients=[
            Recipient("to", "Robert", "BOB@EXAMPLE.ORG"),
            Recipient("to", "Bob", "bob@example.org"),
            Recipient("cc", "Carol D", "Carol@Example.net"),
        ],
        sent_ts=T0 + DAY,
        in_reply_to="<p-1@example.com>",
    )
    _, chain_id, _ = _store(tmp_store, first)
    _, second_chain, _ = _store(tmp_store, second)
    assert second_chain == chain_id

    participants = json.loads(_chain_row(tmp_store, chain_id)["participants_json"])
    assert participants == {
        "from": [{"name": "Jane Doe", "addr": "jane@example.com"}],
        "to": [{"name": "Bob", "addr": "bob@example.org"}],
        "cc": [{"name": "Carol", "addr": "carol@example.net"}],
    }
