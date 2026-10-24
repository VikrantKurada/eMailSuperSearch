"""Excel export: columns, links, row cap, text safety and atomic save."""

from pathlib import Path

from openpyxl import load_workbook

from emailsupersearch.export.excel import EXPORT_MAX_ROWS, export_results
from emailsupersearch.search.results import AttachmentInfo, MessageCard
from emailsupersearch.types import Person

SENT = 1_773_705_600  # 2026-03-17 00:00 UTC

HEADERS = [
    "Date",
    "From",
    "To",
    "CC",
    "Subject",
    "Chain",
    "Attachments",
    "Preview",
    "Open",
    "Path",
]


def _card(key="k1", subject="Quarterly plan", paths=(), **overrides):
    values = {
        "message_key": key,
        "subject": subject,
        "sent_ts": SENT,
        "sender": Person("Ada Lovelace", "ada@example.com"),
        "to": [Person("Bob", "bob@example.com")],
        "cc": [Person("", "cc@example.com")],
        "snippet": "the plan is attached",
        "attachments": [],
        "paths": list(paths),
        "chain_id": None,
        "score": 1.0,
    }
    values.update(overrides)
    return MessageCard(**values)


def _attachment(filename):
    return AttachmentInfo(1, filename, "pdf", 10, "ok", None, False, 1)


def test_export_writes_expected_columns_and_hyperlink(tmp_path):
    msg_path = tmp_path / "mail" / "plan.msg"
    card = _card(
        paths=[str(msg_path)],
        attachments=[_attachment("plan.pdf"), _attachment("notes.docx")],
        chain_id="chain-1",
    )

    info = export_results([card], tmp_path / "out" / "results.xlsx")

    assert info.path == tmp_path / "out" / "results.xlsx"
    assert info.rows == 1
    assert info.truncated is False
    ws = load_workbook(info.path).active
    assert [c.value for c in ws[1]] == HEADERS
    assert all(c.font.bold for c in ws[1])
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == ws.dimensions
    assert ws["A2"].value == "2026-03-17 00:00"
    assert ws["B2"].value == "Ada Lovelace <ada@example.com>"
    assert ws["C2"].value == "Bob <bob@example.com>"
    assert ws["D2"].value == "cc@example.com"
    assert ws["E2"].value == "Quarterly plan"
    assert ws["F2"].value == "chain-1"
    assert ws["G2"].value == "plan.pdf; notes.docx"
    assert ws["H2"].value == "the plan is attached"
    assert ws["H2"].alignment.wrap_text
    assert ws.column_dimensions["H"].width == 60
    assert ws["I2"].value == "Open"
    assert ws["I2"].hyperlink.target.startswith("file:///")
    assert ws["I2"].style == "Hyperlink"
    assert ws["J2"].value == str(msg_path)
    assert ws.column_dimensions["E"].width == len("Quarterly plan") + 2


def test_export_caps_rows_and_reports_truncation(tmp_path):
    cards = [_card(key=f"k{i}", subject=f"s{i}", snippet="", sent_ts=None) for i in range(6000)]

    info = export_results(cards, tmp_path / "big.xlsx")

    assert EXPORT_MAX_ROWS == 5000
    assert info.rows == 5000
    assert info.truncated is True
    ws = load_workbook(info.path).active
    assert ws.max_row == 5001
    assert ws["A2"].value in (None, "")


def test_export_percent_encodes_path(tmp_path):
    odd = tmp_path / "my mail" / "a#b.msg"

    info = export_results([_card(paths=[str(odd)])], tmp_path / "out.xlsx")

    target = load_workbook(info.path).active["I2"].hyperlink.target
    assert "%20" in target
    assert "%23" in target


def test_formula_like_text_is_stored_as_text(tmp_path):
    evil = '=HYPERLINK("http://evil")'
    card = _card(subject=evil, snippet="@SUM(A1)", attachments=[_attachment("-cmd.exe")])

    info = export_results([card], tmp_path / "out.xlsx")

    ws = load_workbook(info.path).active
    assert ws["E2"].data_type == "s"
    assert ws["E2"].value == evil
    assert ws["H2"].data_type == "s"
    assert ws["H2"].value == "@SUM(A1)"
    assert ws["G2"].data_type == "s"
    assert ws["G2"].value == "-cmd.exe"


def test_chain_subjects_mapping_used(tmp_path):
    cards = [
        _card(key="a", chain_id="c1"),
        _card(key="b", chain_id="c2"),
        _card(key="c", chain_id=None),
    ]

    info = export_results(cards, tmp_path / "out.xlsx", chain_subjects={"c1": "Budget thread"})

    ws = load_workbook(info.path).active
    assert ws["F2"].value == "Budget thread"
    assert ws["F3"].value == "c2"
    assert ws["F4"].value in (None, "")


def test_relative_path_is_resolved_for_the_link(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    info = export_results([_card(paths=["mail/rel.msg"])], tmp_path / "out.xlsx")

    target = load_workbook(info.path).active["I2"].hyperlink.target
    assert target.startswith("file:///")
    assert target.endswith("/mail/rel.msg")


def test_card_without_path_has_empty_open_cell(tmp_path):
    info = export_results([_card(paths=[])], tmp_path / "out.xlsx")

    ws = load_workbook(info.path).active
    assert ws["I2"].value in (None, "")
    assert ws["I2"].hyperlink is None
    assert ws["J2"].value in (None, "")


def test_export_leaves_no_temp_file_behind(tmp_path):
    export_results([_card()], tmp_path / "out.xlsx")

    assert sorted(p.name for p in Path(tmp_path).iterdir()) == ["out.xlsx"]


def test_control_characters_are_dropped_not_fatal(tmp_path):
    card = _card(subject="bad\x00subject\x1f", snippet="line1\nline2\ttab")

    info = export_results([card], tmp_path / "out.xlsx")

    ws = load_workbook(info.path).active
    assert ws["E2"].value == "badsubject"
    assert ws["H2"].value == "line1\nline2\ttab"
