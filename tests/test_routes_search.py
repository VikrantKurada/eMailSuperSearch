"""The search, message viewer, attachment, open/reveal and export routes, through the real app over a real project.

Every project here is indexed from the module's ``corpus_dir``: the synthetic corpus plus one HTML reply (see the
fixture), so the viewer has a body to sanitise, a Message-ID holding ``/`` and an attachment with an awkward name.
"""

import os
import re
import subprocess
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

import openpyxl
import pytest

from emailsupersearch.host import routes_export
from emailsupersearch.host.routes_messages import attachment_disposition

HTML_KEY = "html/1@corpus.example"
HTML_FILE = "zz-html.eml"
ODD_FILENAME = 'résumé "final".txt'
ODD_BYTES = b"odd attachment bytes"
_LINK = 'rel="noopener noreferrer" target="_blank"'


@pytest.fixture
def corpus_dir(corpus_dir):
    """The synthetic corpus plus a reply to thread 0 with an HTML body (a script, an inline handler, a ``cid:``
    image and a link), Message-ID ``<html/1@corpus.example>`` and one attachment named ``ODD_FILENAME``."""
    msg = EmailMessage()
    msg["Subject"] = "Re: Printer lease renewal"
    msg["From"] = "Hana Html <hana@corpus.example>"
    msg["To"] = "Alice Morgan <alice.morgan@example.com>"
    msg["Cc"] = "Ben Carter <ben.carter@example.com>"
    msg["Date"] = "Tue, 06 Jan 2026 09:00:00 +0000"
    msg["Message-ID"] = f"<{HTML_KEY}>"
    msg["In-Reply-To"] = "<t000-m00@corpus.example>"
    msg["References"] = "<t000-m00@corpus.example>"
    msg.set_content("Plain part of the html reply.")
    msg.add_alternative(
        '<p onclick="steal()">Hello <img src="cid:logo"><script>evil()</script>'
        '<a href="https://example.com/terms" style="color:red">terms</a></p>',
        subtype="html",
    )
    msg.add_attachment(ODD_BYTES, maintype="text", subtype="plain", filename=ODD_FILENAME)
    (corpus_dir / HTML_FILE).write_bytes(msg.as_bytes())
    return corpus_dir


def _conn(host_services, project_id):
    return host_services.supervisors[project_id].store.read_conn()


def _message_url(project_id, key, suffix=""):
    return f"/api/projects/{project_id}/messages/{quote(key, safe='')}{suffix}"


def _html_path(corpus_dir):
    return os.path.join(os.path.normpath(str(corpus_dir)), HTML_FILE)


def _sheet_rows(path):
    wb = openpyxl.load_workbook(path)
    try:
        rows = [list(row) for row in wb.active.iter_rows(values_only=True)]
    finally:
        wb.close()
    return rows[0], rows[1:]


# ---- search ----


def test_search_route_returns_cards(client, indexed_project):
    r = client.post(f"/api/projects/{indexed_project}/search", json={"query": "INV-2024-00017", "group_by_chain": False})
    assert r.status_code == 200 and r.json()["messages"]
    body = r.json()
    assert body["chains"] == [] and body["mode"] == "keyword_only" and body["page"] == 0
    assert "Printer lease renewal" in body["messages"][0]["subject"]


def test_search_route_accepts_sort(client, indexed_project):
    url = f"/api/projects/{indexed_project}/search"
    assert client.post(url, json={"query": "meeting", "sort": "date"}).status_code == 200
    r = client.post(url, json={"query": "", "sort": "date", "group_by_chain": False, "page_size": 5, "extra": 1})
    assert r.status_code == 200
    body = r.json()
    dates = [card["sent_ts"] for card in body["messages"]]
    assert body["page_size"] == 5 and len(dates) == 5 and dates == sorted(dates, reverse=True)


def test_search_and_export_reject_bad_fields(client, project):
    search = f"/api/projects/{project}/search"
    bad_bodies = [
        [], {"sort": "newest"}, {"page": -1}, {"page": True}, {"page_size": 0}, {"page_size": 101},
        {"query": 5}, {"from_addrs": "a@b.c"}, {"recipient_addrs": [1]}, {"attachment_kinds": None},
        {"date_from": "2026"}, {"date_to": True}, {"has_attachments": "yes"}, {"group_by_chain": None},
        {"only_with_conclusion": 1},
    ]
    for body in bad_bodies:
        r = client.post(search, json=body)
        assert r.status_code == 400 and "error" in r.json(), body
    assert client.post(search, content=b"{nope").status_code == 400
    assert client.post(search, json={"date_from": None, "has_attachments": None}).status_code == 200
    export = f"/api/projects/{project}/export/excel"
    for body in [{"filters": []}, {"filters": {"sort": "x"}}, {"filters": {}, "pinned_keys": "k"},
                 {"filters": {}, "pinned_chain_ids": [3]}]:
        assert client.post(export, json=body).status_code == 400, body
    assert client.post("/api/projects/nope/search", json={}).status_code == 404


# ---- message viewer ----


def test_message_route_returns_sanitized_body_and_chain(client, indexed_project, host_services, corpus_dir):
    r = client.get(_message_url(indexed_project, HTML_KEY))
    assert r.status_code == 200
    body = r.json()
    html = body["message"]["body_html_sanitized"]
    assert "<script" not in body["message"]["body_html_sanitized"] and body["chain"]["messages"]
    assert "onclick" not in html and "cid:" not in html and "style" not in html
    assert html.strip() == f'<p>Hello <a href="https://example.com/terms" {_LINK}>terms</a></p>'

    message = body["message"]
    assert message["headers"]["subject"] == "Re: Printer lease renewal"
    assert message["headers"]["message_id"] == HTML_KEY and message["headers"]["date_source"] == "header_date"
    assert message["sent_ts"] == message["headers"]["sent_ts"] == int(datetime(2026, 1, 6, 9, tzinfo=UTC).timestamp())
    assert message["sender"] == {"name": "Hana Html", "addr": "hana@corpus.example"}
    assert message["to"] == [{"name": "Alice Morgan", "addr": "alice.morgan@example.com"}]
    assert message["cc"] == [{"name": "Ben Carter", "addr": "ben.carter@example.com"}]
    assert "Plain part of the html reply." in message["body_text"]
    assert [(a["ordinal"], a["filename"]) for a in body["attachments"]] == [(0, ODD_FILENAME)]
    assert body["paths"] == [_html_path(corpus_dir)]

    chain_id = _conn(host_services, indexed_project).execute(
        "SELECT chain_id FROM messages WHERE message_key = ?", (HTML_KEY,)
    ).fetchone()[0]
    members = body["chain"]["messages"]
    assert body["chain"]["chain_id"] == chain_id and all(m["chain_id"] == chain_id for m in members)
    keys = [m["message_key"] for m in members]
    assert HTML_KEY in keys and "t000-m00@corpus.example" in keys
    assert [m["sent_ts"] for m in members] == sorted(m["sent_ts"] for m in members)


def test_message_route_404_for_unknown_key(client, project):
    for method, suffix in [("GET", ""), ("GET", "/attachments/0"), ("POST", "/open"), ("POST", "/reveal")]:
        r = client.request(method, _message_url(project, "no-such@key", suffix))
        assert r.status_code == 404 and r.json() == {"error": "no such message"}, suffix
    assert client.get(_message_url("no-such-project", "x")).status_code == 404


# ---- attachments ----


def test_attachment_download(client, indexed_project, host_services):
    key, ordinal = _conn(host_services, indexed_project).execute(
        "SELECT message_key, ordinal FROM attachments WHERE kind = 'pdf' ORDER BY message_key LIMIT 1"
    ).fetchone()
    r = client.get(_message_url(indexed_project, key, f"/attachments/{ordinal}"))
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert client.get(_message_url(indexed_project, key, f"/attachments/{ordinal + 1}")).status_code == 404


def test_attachment_download_filename_is_safe(client, indexed_project):
    r = client.get(_message_url(indexed_project, HTML_KEY, "/attachments/0"))
    assert r.status_code == 200 and r.content == ODD_BYTES
    assert r.headers["content-disposition"] == (
        "attachment; filename=\"r_sum_ _final_.txt\"; filename*=UTF-8''r%C3%A9sum%C3%A9%20%22final%22.txt"
    )
    assert attachment_disposition("a;b\\c\r\n.txt", 3) == (
        "attachment; filename=\"a_b_c__.txt\"; filename*=UTF-8''a%3Bb%5Cc%0D%0A.txt"
    )
    assert attachment_disposition("", 3) == "attachment; filename=\"attachment-3\"; filename*=UTF-8''attachment-3"


# ---- open and reveal ----


def test_open_route_calls_startfile(client, indexed_project, monkeypatch, corpus_dir):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p), raising=False)
    r = client.post(_message_url(indexed_project, HTML_KEY, "/open"))
    assert r.status_code == 204 and calls
    assert calls == [_html_path(corpus_dir)]


def test_reveal_route_spawns_explorer(client, indexed_project, monkeypatch, corpus_dir):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: spawned.append((args, kwargs)))
    r = client.post(_message_url(indexed_project, HTML_KEY, "/reveal"))
    assert r.status_code == 204
    assert [args for args, _ in spawned] == [["explorer", "/select,", _html_path(corpus_dir)]]
    assert spawned[0][1].get("shell") is not True


# ---- export ----


def test_export_route_writes_xlsx(client, indexed_project, host_services, monkeypatch):
    url = f"/api/projects/{indexed_project}/export/excel"
    r = client.post(url, json={"filters": {"query": ""}})
    assert r.status_code == 200
    assert r.json()["rows"] > 0 and Path(r.json()["path"]).exists()
    info = r.json()
    path = Path(info["path"])
    assert path.parent == host_services.catalog.get(indexed_project).dir / "exports" and path.name == info["name"]
    assert re.fullmatch(r"export-\d{8}-\d{6}\.xlsx", info["name"])
    assert info["rows"] == host_services.supervisors[indexed_project].store.counts()["messages"]
    assert info["truncated"] is False
    _, rows = _sheet_rows(path)
    assert len(rows) == info["rows"]

    def locked(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(routes_export, "export_results", locked)
    r = client.post(url, json={"filters": {"query": ""}})
    assert r.status_code == 409
    assert r.json() == {"error": "the export file is open in another program; close it and retry"}


def test_export_route_uses_pinned_keys_when_present(client, indexed_project, host_services):
    url = f"/api/projects/{indexed_project}/export/excel"
    r = client.post(url, json={"filters": {"query": ""}, "pinned_keys": [HTML_KEY]})
    assert r.status_code == 200 and r.json()["rows"] == 1

    conn = _conn(host_services, indexed_project)
    chain_id = conn.execute("SELECT chain_id FROM messages WHERE message_key = ?", (HTML_KEY,)).fetchone()[0]
    members = conn.execute("SELECT COUNT(*) FROM messages WHERE chain_id = ?", (chain_id,)).fetchone()[0]
    chain_subject = conn.execute("SELECT subject FROM chains WHERE chain_id = ?", (chain_id,)).fetchone()[0]
    r = client.post(
        url, json={"filters": {"query": "nothing matches this"}, "pinned_keys": [HTML_KEY], "pinned_chain_ids": [chain_id]}
    )
    assert r.status_code == 200 and r.json()["rows"] == members > 1
    header, rows = _sheet_rows(r.json()["path"])
    dates = [row[header.index("Date")] for row in rows]
    assert dates == sorted(dates, reverse=True)
    assert {row[header.index("Chain")] for row in rows} == {chain_subject or chain_id}


def test_export_name_traversal_rejected(client, project, host_services):
    exports = host_services.catalog.get(project).dir / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    (exports / "evil.xlsx").write_bytes(b"not an export")
    for name in ["..%5Cproject.sqlite", "evil.xlsx", "export-20260101-000000.xlsx.bak", "export-2026010-000000.xlsx",
                 "export-20260101-000000.xlsx"]:
        r = client.get(f"/api/projects/{project}/exports/{name}")
        assert r.status_code == 404, name


def test_export_download_roundtrip(client, indexed_project, host_services, monkeypatch, tmp_path):
    monkeypatch.setattr(routes_export, "_utcnow", lambda: datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC))
    url = f"/api/projects/{indexed_project}/export/excel"
    first = client.post(url, json={"filters": {"query": "", "group_by_chain": True}}).json()
    second = client.post(url, json={"filters": {"query": ""}}).json()
    assert (first["name"], second["name"]) == ("export-20261024-120000.xlsx", "export-20261024-120000-1.xlsx")
    for info in (first, second):
        r = client.get(f"/api/projects/{indexed_project}/exports/{info['name']}")
        assert r.status_code == 200 and r.content == Path(info["path"]).read_bytes()
        assert info["name"] in r.headers["content-disposition"]

    store = host_services.supervisors[indexed_project].store
    store.set_setting("export_dir", str(tmp_path / "missing"))
    assert Path(client.post(url, json={"filters": {}}).json()["path"]).parent.name == "exports"
    out = tmp_path / "out"
    out.mkdir()
    store.set_setting("export_dir", str(out))
    third = client.post(url, json={"filters": {}}).json()
    assert Path(third["path"]) == out / "export-20261024-120000.xlsx"
    assert client.get(f"/api/projects/{indexed_project}/exports/{third['name']}").content == Path(third["path"]).read_bytes()
