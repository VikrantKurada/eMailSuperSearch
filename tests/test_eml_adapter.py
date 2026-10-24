import os
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from emailsupersearch.diagnostics.corpus import generate
from emailsupersearch.ingest.eml_adapter import parse_eml


def base_message(**headers: str) -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = headers.pop("Subject", "hello")
    m["From"] = headers.pop("From", "Alice Morgan <alice@example.com>")
    m["To"] = headers.pop("To", "Ben Carter <ben@example.org>")
    for name, value in headers.items():
        m[name.replace("_", "-")] = value
    return m


def write(tmp_path, m: EmailMessage | bytes, name: str = "m.eml"):
    p = tmp_path / name
    p.write_bytes(m if isinstance(m, bytes) else m.as_bytes())
    return p


def test_parse_eml_fields(tmp_path):
    m = parse_eml(generate(tmp_path, 1, seed=1)[0])
    assert (
        m.message_id
        and m.sender_addr.endswith(("example.com", "example.org"))
        and m.sent_ts
        and m.date_source == "header_date"
    )
    assert any(r.kind == "to" for r in m.recipients) and m.body_text.strip()


def test_parse_eml_replies_have_in_reply_to(tmp_path):
    paths = generate(tmp_path, 1, seed=2)
    assert all(parse_eml(p).in_reply_to for p in paths[1:])


def test_parse_eml_nested_rfc822(tmp_path):
    p = tmp_path / "n.eml"
    outer = EmailMessage()
    outer["Subject"] = "outer"
    outer["From"] = "a@example.com"
    outer["To"] = "b@example.com"
    outer.set_content("hi")
    inner = EmailMessage()
    inner["Subject"] = "inner"
    inner.set_content("x")
    outer.add_attachment(inner)
    p.write_bytes(outer.as_bytes())
    m = parse_eml(p)
    assert m.attachments[0].kind == "eml" and m.attachments[0].nested.subject == "inner"


def test_nested_message_data_is_its_own_rfc822_bytes(tmp_path):
    outer = base_message()
    outer.set_content("hi")
    inner = base_message(Subject="inner", Message_ID="<in@x>")
    inner.set_content("deep")
    outer.add_attachment(inner)
    att = parse_eml(write(tmp_path, outer)).attachments[0]
    assert att.mime == "message/rfc822" and att.filename == "part-0"
    assert att.size == len(att.data) and b"Subject: inner" in att.data
    assert att.nested.message_id == "in@x" and att.nested.body_text.strip() == "deep"


def test_addresses_names_and_recipient_kinds(tmp_path):
    m = base_message(
        From='"Morgan, Alice" <Alice@Example.com>',
        To="Ben Carter <ben@example.org>, carol@example.org",
        Cc="Dev Patel <dev@example.org>",
        Bcc="Elena <elena@example.com>",
    )
    m.set_content("hi")
    parsed = parse_eml(write(tmp_path, m))
    assert (parsed.sender_name, parsed.sender_addr) == ("Morgan, Alice", "Alice@Example.com")
    assert [(r.kind, r.name, r.addr) for r in parsed.recipients] == [
        ("to", "Ben Carter", "ben@example.org"),
        ("to", "", "carol@example.org"),
        ("cc", "Dev Patel", "dev@example.org"),
        ("bcc", "Elena", "elena@example.com"),
    ]


def test_header_date_is_converted_to_utc_epoch(tmp_path):
    m = base_message(Date="Tue, 06 Jan 2026 10:00:00 +0100")
    m.set_content("hi")
    parsed = parse_eml(write(tmp_path, m))
    assert parsed.sent_ts == int(datetime(2026, 1, 6, 9, 0, tzinfo=UTC).timestamp())
    assert parsed.date_source == "header_date" and parsed.received_ts is None


@pytest.mark.parametrize("date", [None, "not a date"])
def test_missing_or_bad_date_falls_back_to_file_mtime(tmp_path, date):
    m = base_message() if date is None else base_message(Date=date)
    m.set_content("hi")
    p = write(tmp_path, m)
    os.utime(p, (1_700_000_000, 1_700_000_000))
    parsed = parse_eml(p)
    assert parsed.sent_ts == 1_700_000_000 and parsed.date_source == "mtime"


def test_date_without_zone_is_read_as_utc(tmp_path):
    m = base_message(Date="Tue, 06 Jan 2026 10:00:00 -0000")
    m.set_content("hi")
    parsed = parse_eml(write(tmp_path, m))
    assert parsed.sent_ts == int(datetime(2026, 1, 6, 10, 0, tzinfo=UTC).timestamp())


def test_folded_message_id_is_one_key(tmp_path):
    raw = (
        b"From: a@example.com\r\nTo: b@example.org\r\nSubject: s\r\n"
        b"Message-ID: <abc\r\n def@example.com>\r\n\r\nbody\r\n"
    )
    assert parse_eml(write(tmp_path, raw)).message_id == "abcdef@example.com"
    raw2 = raw.replace(b"Message-ID: <abc\r\n def@example.com>", b"Message-ID:\r\n <whole@example.com>")
    assert parse_eml(write(tmp_path, raw2, "two.eml")).message_id == "whole@example.com"


def test_missing_message_id_is_none(tmp_path):
    m = base_message()
    m.set_content("hi")
    assert parse_eml(write(tmp_path, m)).message_id is None


def test_in_reply_to_and_references_lose_their_brackets(tmp_path):
    raw = (
        b"From: a@example.com\r\nTo: b@example.org\r\nSubject: s\r\n"
        b"In-Reply-To: <p2@x>\r\n"
        b"References: <p0@x> <p1@x>\r\n <p2@x>\r\n\r\nbody\r\n"
    )
    m = parse_eml(write(tmp_path, raw))
    assert m.in_reply_to == "p2@x" and m.references == ["p0@x", "p1@x", "p2@x"]
    bare = parse_eml(write(tmp_path, b"From: a@x.org\r\nSubject: s\r\n\r\nb\r\n", "bare.eml"))
    assert bare.in_reply_to is None and bare.references == []


def test_html_only_body_is_tag_stripped(tmp_path):
    m = base_message()
    html = (
        "<html><head><style>p { color: red }</style></head><body><p>Hello</p>"
        "<p>World &amp; co<br>line two</p><script>var x = 1;</script></body></html>"
    )
    m.set_content(html, subtype="html")
    parsed = parse_eml(write(tmp_path, m))
    assert parsed.body_html.startswith("<html>") and "<p>Hello</p>" in parsed.body_html
    assert parsed.body_text.splitlines() == ["Hello", "", "World & co", "line two"]


def test_plain_part_wins_and_html_is_kept_alongside(tmp_path):
    m = base_message()
    m.set_content("plain version")
    m.add_alternative("<p>html version</p>", subtype="html")
    parsed = parse_eml(write(tmp_path, m))
    assert parsed.body_text.strip() == "plain version"
    assert "html version" in parsed.body_html


def test_plain_only_message_has_no_html(tmp_path):
    m = base_message()
    m.set_content("just text")
    assert parse_eml(write(tmp_path, m)).body_html is None


def test_unknown_charset_does_not_break_parsing(tmp_path):
    raw = (
        b"From: a@example.com\r\nTo: b@example.org\r\nSubject: s\r\n"
        b"Content-Type: text/plain; charset=x-no-such-charset\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\ncaf\xc3\xa9 time\r\n"
    )
    assert "time" in parse_eml(write(tmp_path, raw)).body_text


def test_attachments_in_order_with_ordinals_and_data(tmp_path):
    m = base_message()
    m.set_content("see attached")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="a.PDF")
    m.add_attachment(b"binary", maintype="application", subtype="octet-stream")
    m.add_attachment(b"pngdata", maintype="image", subtype="png", filename="b.png")
    atts = parse_eml(write(tmp_path, m)).attachments
    assert [(a.ordinal, a.filename, a.mime, a.kind, a.size) for a in atts] == [
        (0, "a.PDF", "application/pdf", "pdf", 13),
        (1, "part-1", "application/octet-stream", "other", 6),
        (2, "b.png", "image/png", "image", 7),
    ]
    assert [a.data for a in atts] == [b"%PDF-1.4 fake", b"binary", b"pngdata"]
    assert all(a.nested is None and a.content_id is None for a in atts)


@pytest.mark.parametrize(
    ("filename", "maintype", "subtype", "kind"),
    [
        ("r.pdf", "application", "octet-stream", "pdf"),
        ("r.DOCX", "application", "octet-stream", "docx"),
        ("r.xlsx", "application", "octet-stream", "xlsx"),
        ("r.xlsm", "application", "octet-stream", "xlsx"),
        ("r.pptx", "application", "octet-stream", "pptx"),
        ("r.jpg", "application", "octet-stream", "image"),
        ("r.jpeg", "application", "octet-stream", "image"),
        ("r.tif", "application", "octet-stream", "image"),
        ("r.tiff", "application", "octet-stream", "image"),
        ("r.bmp", "application", "octet-stream", "image"),
        ("r.webp", "application", "octet-stream", "image"),
        ("r.msg", "application", "octet-stream", "msg"),
        ("r.eml", "application", "octet-stream", "eml"),
        ("r.zip", "application", "zip", "other"),
        ("r.txt", "text", "plain", "other"),
        ("scan", "image", "gif", "image"),
        (None, "application", "pdf", "pdf"),
    ],
)
def test_attachment_kind_from_suffix_then_mime(tmp_path, filename, maintype, subtype, kind):
    m = base_message()
    m.set_content("hi")
    m.add_attachment(b"x" * 4, maintype=maintype, subtype=subtype, filename=filename)
    assert parse_eml(write(tmp_path, m)).attachments[0].kind == kind


def test_text_attachment_data_is_bytes(tmp_path):
    m = base_message()
    m.set_content("hi")
    m.add_attachment("notes line", subtype="plain", filename="notes.txt")
    att = parse_eml(write(tmp_path, m)).attachments[0]
    assert isinstance(att.data, bytes) and att.data.startswith(b"notes line") and att.size == len(att.data)


def test_content_id_is_kept_without_brackets(tmp_path):
    m = base_message()
    m.set_content("<p>logo <img src='cid:logo@x'></p>", subtype="html")
    m.add_related(b"pngdata", "image", "png", cid="<logo@x>", filename="logo.png")
    atts = parse_eml(write(tmp_path, m)).attachments
    assert [(a.filename, a.kind, a.content_id) for a in atts] == [("logo.png", "image", "logo@x")]


def test_conversation_fields_are_none(tmp_path):
    m = base_message()
    m.set_content("hi")
    parsed = parse_eml(write(tmp_path, m))
    assert parsed.conversation_index is None and parsed.conversation_topic is None
