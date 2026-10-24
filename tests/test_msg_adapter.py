import shutil
import struct
from datetime import UTC, datetime
from pathlib import Path

import compressed_rtf
import pytest
from extract_msg.ole_writer import OleWriter

from emailsupersearch.ingest.msg_adapter import _strip_rtf, parse_msg
from emailsupersearch.ingest.parse import ParseError, parse_file


def test_parse_msg_every_fixture_has_subject_or_body(msg_fixtures):
    for p in msg_fixtures:
        m = parse_msg(p)
        assert m.subject or m.body_text, p.name


def test_parse_msg_rtf_only_body_has_text(msg_fixtures):
    for p in msg_fixtures:
        m = parse_msg(p)
        if m.body_html is None and m.body_text == "":
            pytest.fail(f"{p.name}: no body text recovered")


def test_parse_msg_dates_are_utc_ints_with_source(msg_fixtures):
    for p in msg_fixtures:
        m = parse_msg(p)
        assert m.sent_ts is None or (
            isinstance(m.sent_ts, int) and m.date_source in {"sent", "received", "header_date", "mtime"}
        )


def test_parse_msg_releases_file_handle(msg_fixtures, tmp_path):
    dst = tmp_path / msg_fixtures[0].name
    shutil.copy(msg_fixtures[0], dst)
    parse_msg(dst)
    dst.unlink()


def test_parse_file_rejects_unknown_suffix(tmp_path):
    (tmp_path / "x.txt").write_text("hi")
    with pytest.raises(ParseError):
        parse_file(tmp_path / "x.txt")


# The tests below parse messages written byte by byte with extract-msg's OLE writer, so the inputs
# that no downloaded sample covers (an RTF-only body, a mislabelled code page, nesting) stay tested.

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
_SENT = datetime(2024, 3, 5, 12, 0, tzinfo=UTC)
_RECEIVED = datetime(2024, 3, 5, 12, 5, tzinfo=UTC)
_HEADER_DATE = datetime(2024, 3, 5, 12, 10, tzinfo=UTC)
_DATE_HEADER = "Date: Tue, 05 Mar 2024 14:10:00 +0200"
_RUSSIAN = "Привет, мир! Это тестовое письмо о квартальном бюджете и сроках поставки оборудования."


def _prop(prop_id: int, prop_type: int, value: bytes) -> bytes:
    return struct.pack("<HHI", prop_type, prop_id, 6) + value.ljust(8, b"\x00")


def _filetime(when: datetime) -> bytes:
    return struct.pack("<Q", int((when - _FILETIME_EPOCH).total_seconds() * 10_000_000))


def _add_message(
    out: OleWriter,
    prefix: str,
    subject: str,
    *,
    body: str | None = None,
    html: str | None = None,
    rtf: bytes | None = None,
    header: str | None = None,
    in_reply_to: str | None = None,
    topic: str | None = None,
    conversation_index: bytes | None = None,
    sender_name: str | None = None,
    sender_addr: str | None = None,
    recipients: tuple = (),
    sent: datetime | None = None,
    received: datetime | None = None,
    attachments: tuple = (),
    cp1251_labelled_utf8: bool = False,
) -> None:
    """Write one message under ``prefix`` ("" for the file itself, else an embedded-message storage)."""
    entries = [_prop(0x340D, 3, struct.pack("<i", 0 if cp1251_labelled_utf8 else 0x40000))]
    if cp1251_labelled_utf8:  # 8-bit strings in Windows-1251, but the message claims UTF-8
        entries.append(_prop(0x3FFD, 3, struct.pack("<i", 65001)))
    for prop_id, when in ((0x0039, sent), (0x0E06, received)):
        if when is not None:
            entries.append(_prop(prop_id, 0x40, _filetime(when)))
    n_att, n_rcpt = len(attachments), len(recipients)
    counts = b"\x00" * 8 + struct.pack("<IIII", n_rcpt, n_att, n_rcpt, n_att) + (b"" if prefix else b"\x00" * 8)
    out.addEntry(f"{prefix}__properties_version1.0", counts + b"".join(entries))
    suffix, codec = ("001E", "cp1251") if cp1251_labelled_utf8 else ("001F", "utf-16-le")
    strings = {"001A": "IPM.Note", "0037": subject, "1000": body, "007D": header, "1042": in_reply_to, "0070": topic}
    strings |= {"0C1A": sender_name, "5D01": sender_addr}
    for prop_id, text in strings.items():
        if text is not None:
            out.addEntry(f"{prefix}__substg1.0_{prop_id}{suffix}", text.encode(codec))
    if conversation_index is not None:
        out.addEntry(f"{prefix}__substg1.0_00710102", conversation_index)
    for number, (rcpt_type, name, address) in enumerate(recipients):
        rcpt = f"{prefix}__recip_version1.0_#{number:08X}/"
        out.addEntry(f"{rcpt}__properties_version1.0", b"\x00" * 8 + _prop(0x0C15, 3, struct.pack("<i", rcpt_type)))
        out.addEntry(f"{rcpt}__substg1.0_3001001F", name.encode("utf-16-le"))
        out.addEntry(f"{rcpt}__substg1.0_39FE001F", address.encode("utf-16-le"))
    if html is not None:
        out.addEntry(f"{prefix}__substg1.0_10130102", html.encode("utf-8"))
    if rtf is not None:
        out.addEntry(f"{prefix}__substg1.0_10090102", compressed_rtf.compress(rtf))
    for number, (kind, filename, content, content_id) in enumerate(attachments):
        att = f"{prefix}__attach_version1.0_#{number:08X}/"
        method = 5 if kind == "msg" else 1
        out.addEntry(f"{att}__properties_version1.0", b"\x00" * 8 + _prop(0x3705, 3, struct.pack("<i", method)))
        out.addEntry(f"{att}__substg1.0_3707001F", filename.encode("utf-16-le"))
        if kind == "data":
            out.addEntry(f"{att}__substg1.0_37010102", content)
            if content_id:
                out.addEntry(f"{att}__substg1.0_3712001F", content_id.encode("utf-16-le"))
        elif kind == "msg":  # ``content`` holds the embedded message's own fields
            _add_message(out, f"{att}__substg1.0_3701000D/", **content)
        # "broken": a by-value attachment without its data stream, which extract-msg rejects


def make_msg(path: Path, subject: str, **fields) -> Path:
    out = OleWriter()
    out.addEntry("__nameid_version1.0", storage=True)  # the named-property maps, empty
    for stream in ("00020102", "00030102", "00040102"):
        out.addEntry(f"__nameid_version1.0/__substg1.0_{stream}", b"")
    _add_message(out, "", subject, **fields)
    out.write(str(path))
    return path


def test_parse_msg_rtf_only_message_gets_text_from_the_rtf(tmp_path):
    rtf = rb"{\rtf1\ansi\ansicpg1252{\fonttbl{\f0 Arial;}}{\*\generator Riched20;}\pard Hello caf\'e9 "
    rtf += b"\\" + rb"u8364? world\par Second line {\b bold}\par}"
    m = parse_msg(make_msg(tmp_path / "rtf.msg", "RTF only", rtf=rtf))
    assert m.body_text == f"Hello caf\xe9 {chr(0x20AC)} world\nSecond line bold"
    assert m.body_html is None


def test_strip_rtf_uses_the_declared_code_page_and_joins_surrogates():
    assert _strip_rtf(rb"{\rtf1\ansi\ansicpg1251\pard \'cf\'f0\'e8\'e2\'e5\'f2\par}") == "Привет"
    emoji = rb"{\rtf1\ansi " + b"\\" + rb"u-10179?" + b"\\" + rb"u-8704?!}"
    assert _strip_rtf(emoji) == chr(0x1F600) + "!"


def test_parse_msg_retries_with_a_detected_encoding(tmp_path):
    m = parse_msg(make_msg(tmp_path / "ru.msg", _RUSSIAN, body=_RUSSIAN, cp1251_labelled_utf8=True))
    assert m.subject == _RUSSIAN and m.body_text == _RUSSIAN


@pytest.mark.parametrize(
    ("fields", "source", "expected"),
    [
        ({"sent": _SENT, "received": _RECEIVED, "header": _DATE_HEADER}, "sent", _SENT),
        ({"received": _RECEIVED, "header": _DATE_HEADER}, "received", _RECEIVED),
        ({"header": _DATE_HEADER}, "header_date", _HEADER_DATE),
        ({}, "mtime", None),
    ],
)
def test_parse_msg_date_source_order(tmp_path, fields, source, expected):
    p = make_msg(tmp_path / "d.msg", "dates", **fields)
    m = parse_msg(p)
    assert m.date_source == source
    assert m.sent_ts == (int(expected.timestamp()) if expected else int(p.stat().st_mtime))
    assert m.received_ts == (int(_RECEIVED.timestamp()) if "received" in fields else None)


def test_parse_msg_ids_lose_brackets_and_references_are_split(tmp_path):
    header = (
        "Message-ID: <abc@example.com>\r\nIn-Reply-To: <from-header@example.com>\r\n"
        "References: <r1@example.com>\r\n\t<r2@example.com>\r\nSubject: x\r\n"
    )
    m = parse_msg(make_msg(tmp_path / "ids.msg", "ids", header=header, in_reply_to="<r2@example.com>"))
    assert m.message_id == "abc@example.com"
    assert m.in_reply_to == "r2@example.com"  # the stream wins over the header
    assert m.references == ["r1@example.com", "r2@example.com"]


def test_parse_msg_in_reply_to_falls_back_to_the_first_header_id(tmp_path):
    header = "In-Reply-To: <first@example.com>\r\n\t<second@example.com>\r\nSubject: x\r\n"
    m = parse_msg(make_msg(tmp_path / "irt.msg", "no 0x1042 stream", header=header))
    assert m.in_reply_to == "first@example.com"
    assert parse_msg(make_msg(tmp_path / "none.msg", "no header either")).in_reply_to is None


def test_parse_msg_text_fields_use_lf_line_breaks(tmp_path):
    p = make_msg(tmp_path / "crlf.msg", "one\r\ntwo", body="a\r\nb\rc\r\n", topic="topic\r\nline")
    m = parse_msg(p)
    assert m.body_text == "a\nb\nc\n"
    assert m.subject == "one\ntwo" and m.conversation_topic == "topic\nline"
    assert "\r" not in m.body_text + m.subject + m.conversation_topic


def test_parse_msg_html_only_message_gets_text_from_the_html(tmp_path):
    html = "<html><body><p>Quarterly <b>budget</b></p><table><tr><td>Name</td><td>Value</td></tr></table></body></html>"
    m = parse_msg(make_msg(tmp_path / "html.msg", "HTML only", html=html))
    assert m.body_html == html
    assert m.body_text == "Quarterly budget\n\nName Value"


def test_parse_msg_damaged_attachment_degrades_to_other(tmp_path):
    attachments = (
        ("data", "first.pdf", b"%PDF-1.4", None),
        ("broken", "damaged.bin", None, None),
        ("data", "last.png", b"\x89PNG", None),
    )
    m = parse_msg(make_msg(tmp_path / "broken.msg", "damaged attachment", attachments=attachments))
    assert [(a.ordinal, a.filename, a.kind) for a in m.attachments] == [
        (0, "first.pdf", "pdf"),
        (1, "damaged.bin", "other"),
        (2, "last.png", "image"),
    ]
    assert m.attachments[1].data is None and m.attachments[1].size == 0


def test_parse_msg_data_attachment_keeps_bytes_and_content_id(tmp_path):
    payload = b"%PDF-1.4 not really"
    attachments = (("data", "report.pdf", payload, "<cid-1@example.com>"), ("data", "", b"xx", None))
    m = parse_msg(make_msg(tmp_path / "a.msg", "att", attachments=attachments))
    first, second = m.attachments
    assert (first.ordinal, first.filename, first.kind, first.data, first.size) == (0, "report.pdf", "pdf", payload, 19)
    assert first.mime == "application/pdf" and first.content_id == "cid-1@example.com"
    assert (second.ordinal, second.filename, second.content_id) == (1, "attachment-1", None)


def test_parse_msg_nested_messages_stop_at_depth_three(tmp_path):
    def level(n: int) -> dict:
        fields = {"subject": f"level {n}", "body": f"body {n}"}
        if n < 4:
            fields["attachments"] = (("msg", f"level{n + 1}.msg", level(n + 1), None),)
        return fields

    top = level(0)
    m = parse_msg(make_msg(tmp_path / "n.msg", top.pop("subject"), **top))
    subjects, current = [], m
    while current is not None:
        subjects.append(current.subject)
        att = current.attachments[0] if current.attachments else None
        assert att is None or (att.kind == "msg" and att.data is None)
        current = att.nested if att else None
    assert subjects == ["level 0", "level 1", "level 2", "level 3"]  # level 3 still lists its attachment, unparsed


def test_parse_file_wraps_adapter_errors(tmp_path):
    bad = tmp_path / "broken.msg"
    bad.write_bytes(b"this is not an OLE file" * 100)
    with pytest.raises(ParseError, match=r"^\w+: ") as excinfo:
        parse_file(bad)
    assert excinfo.value.reason == str(excinfo.value)


def test_parse_file_dispatches_on_suffix(tmp_path, corpus_dir):
    assert parse_file(next(corpus_dir.glob("*.eml"))).subject
    assert parse_file(make_msg(tmp_path / "UPPER.MSG", "upper")).subject == "upper"


def _sample(msg_fixtures, name: str) -> Path:
    found = next((p for p in msg_fixtures if p.name == name), None)
    if found is None:
        pytest.skip(f"{name} not fetched")
    return found


def test_parse_msg_sample_recipients_sender_and_conversation_fields(msg_fixtures):
    m = parse_msg(_sample(msg_fixtures, "multi-to.msg"))
    # The file lists Alice (To), Dave (Cc), Carol (To), then Alice and Dave once more.
    assert [(r.kind, r.name, r.addr) for r in m.recipients] == [
        ("to", "Alice Smith", "alice@example.com"),
        ("cc", "Dave Jones", "dave@example.com"),
        ("to", "Carol Wilson", "carol@example.com"),
        ("to", "Alice Smith", "alice@example.com"),
        ("cc", "Dave Jones", "dave@example.com"),
    ]
    assert (m.sender_name, m.sender_addr) == ("Bob Sender", "bob@example.com")
    assert m.conversation_index == bytes.fromhex("010189833d862a66781086d37a839975026a0ac9967a0228ac1d9e01d8b9d676")
    assert m.conversation_topic == "Test: multiple To recipients"
    other = parse_msg(_sample(msg_fixtures, "unicode.msg"))
    assert other.conversation_index == bytes.fromhex("01021a170704432903788667ec3dae49a04d7733109d")
    assert len(other.conversation_index) == 22 and other.conversation_topic == "Test for TIF files"


def test_parse_msg_recipient_types_map_to_to_cc_bcc(tmp_path):
    recipients = (
        (1, "Ann Lee", "ann@example.com"),
        (2, "Cy Dunn", "cy@example.com"),
        (3, "Bo Hidden", "bo@example.org"),
        (0, "Sender Echo", "echo@example.com"),  # a sender-type entry is not a recipient
    )
    m = parse_msg(make_msg(tmp_path / "r.msg", "recipients", recipients=recipients))
    assert [(r.kind, r.name, r.addr) for r in m.recipients] == [
        ("to", "Ann Lee", "ann@example.com"),
        ("cc", "Cy Dunn", "cy@example.com"),
        ("bcc", "Bo Hidden", "bo@example.org"),
    ]


def test_parse_msg_conversation_fields_come_from_their_streams(tmp_path):
    index = bytes(range(22))
    m = parse_msg(make_msg(tmp_path / "c.msg", "not the topic", topic="Quarterly budget", conversation_index=index))
    assert m.conversation_index == index
    assert m.conversation_topic == "Quarterly budget"
    bare = parse_msg(make_msg(tmp_path / "bare.msg", "no conversation fields"))
    assert bare.conversation_index is None and bare.conversation_topic is None


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"sender_name": "Jane Doe", "sender_addr": "jane@example.com"}, ("Jane Doe", "jane@example.com")),
        ({"header": 'From: "Doe, Jane" <jane@example.com>\r\nSubject: x\r\n'}, ("Doe, Jane", "jane@example.com")),
        ({"sender_name": "Facilities Desk"}, ("Facilities Desk", "")),  # a name without an address
        ({}, ("", "")),
    ],
)
def test_parse_msg_sender_name_and_address(tmp_path, fields, expected):
    m = parse_msg(make_msg(tmp_path / "s.msg", "sender", **fields))
    assert (m.sender_name, m.sender_addr) == expected


def test_parse_msg_nested_message_without_a_date_has_no_timestamp(tmp_path):
    attachments = (("msg", "inner.msg", {"subject": "inner", "body": "no date here"}, None),)
    m = parse_msg(make_msg(tmp_path / "outer.msg", "outer", sent=_SENT, attachments=attachments))
    inner = m.attachments[0].nested
    assert (m.date_source, m.sent_ts) == ("sent", int(_SENT.timestamp()))
    assert (inner.subject, inner.sent_ts, inner.date_source) == ("inner", None, "none")


@pytest.mark.parametrize("cut", [600, 2000, 5000])
def test_parse_file_rejects_a_truncated_file(tmp_path, cut):
    fields = {"body": "text " * 50, "attachments": (("data", "a.bin", b"x" * 3000, None),)}
    whole = make_msg(tmp_path / "whole.msg", "whole", **fields).read_bytes()
    assert parse_file(tmp_path / "whole.msg").attachments[0].size == 3000
    short = tmp_path / "short.msg"
    short.write_bytes(whole[:-cut])
    with pytest.raises(ParseError, match="OLE"):
        parse_file(short)


@pytest.mark.parametrize("cut", [10_000, 300_000])
def test_parse_file_rejects_a_truncated_sample(msg_fixtures, tmp_path, cut):
    short = tmp_path / "short.msg"
    short.write_bytes(_sample(msg_fixtures, "unicode.msg").read_bytes()[:-cut])
    with pytest.raises(ParseError):
        parse_file(short)


def test_parse_msg_truncated_attachment_data_is_never_returned_shortened(tmp_path):
    payload = bytes(range(256)) * 800
    whole = make_msg(tmp_path / "big.msg", "big", attachments=(("data", "big.pdf", payload, None),)).read_bytes()
    short = tmp_path / "short.msg"
    short.write_bytes(whole[:-5000])
    att = parse_msg(short).attachments[0]
    assert (att.kind, att.data, att.size) == ("other", None, 0)  # a damaged attachment, not a shorter pdf
