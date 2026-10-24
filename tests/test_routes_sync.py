"""The sync routes, the progress stream and the folder picker, driven through the real app with real supervisors."""

import logging
import subprocess
import time
from dataclasses import fields
from pathlib import Path

from helpers import make_eml_bytes, wait_until

from emailsupersearch.host import folder_picker
from emailsupersearch.host.folder_picker import pick_folder
from emailsupersearch.host.routes_sync import public_event
from emailsupersearch.jobs.supervisor import SyncStatus

_STREAM_DEADLINE_S = 60  # a keepalive ping arrives every 15 s, so the loop re-checks this at least that often


def test_sync_trigger_returns_202_and_status_changes(client, indexed_project, host_services):
    url = f"/api/projects/{indexed_project}/sync"
    host_services.supervisors[indexed_project].store.set_setting("last_synced_at", 1)  # an old scan time to replace
    assert client.get(f"{url}/status").json()["last_scan_at"] == 1
    r = client.post(url)
    assert r.status_code == 202 and r.json() == {"accepted": True}
    wait_until(lambda: client.get(f"{url}/status").json()["last_scan_at"] > 1, 60)


def test_sync_status_route(client, project, host_services):
    r = client.get(f"/api/projects/{project}/sync/status")
    assert r.status_code == 200
    status = r.json()
    assert set(status) == {f.name for f in fields(SyncStatus)}
    assert status["project_id"] == project and status["state"] in {"idle", "scanning", "parsing", "ocr", "reindexing"}
    assert client.get("/api/projects/nope/sync/status").status_code == 404
    assert client.get("/api/projects/nope/sync/events").status_code == 404
    blocked = host_services.catalog.create_project("Old", [])
    host_services.blocked_projects[blocked.id] = "schema too new"
    assert client.get(f"/api/projects/{blocked.id}/sync/status").status_code == 409
    assert client.get(f"/api/projects/{blocked.id}/sync/events").status_code == 409
    assert client.post(f"/api/projects/{blocked.id}/sync").status_code == 409


def test_sync_events_stream_yields_scan_finished(client, indexed_project, host_services, corpus_dir):
    seen = False
    lines = []
    deadline = time.monotonic() + _STREAM_DEADLINE_S
    with client.stream("GET", f"/api/projects/{indexed_project}/sync/events") as r:
        (corpus_dir / "sse-new.eml").write_bytes(make_eml_bytes("sse"))  # a file to index: its event has a path
        client.post(f"/api/projects/{indexed_project}/sync")   # subscribe first, then trigger
        for line in r.iter_lines():
            lines.append(line)
            # The scan that indexed the new file; a watcher-triggered scan may come first, also after the write.
            if '"scan_finished"' in line and any('"file_indexed"' in earlier for earlier in lines):
                seen = True
                break
            if time.monotonic() > deadline:
                break
    assert seen
    assert lines[0] == "event: status" and lines[1].startswith("data: {")
    for line in lines:  # the file event went out without its path
        assert corpus_dir.parent.name not in line and "sse-new" not in line, line
    # Closing the stream ends the response, whose generator closes its subscription.
    wait_until(lambda: f"sync:{indexed_project}" not in host_services.bus._subscribers, 5)


def test_public_event_drops_paths():
    indexed = {"type": "file_indexed", "path": "C:\\Mail\\a.msg", "status": "indexed", "message_key": "k@x"}
    assert public_event(indexed) == {"type": "file_indexed", "status": "indexed", "message_key": "k@x"}
    renamed = {"type": "renamed", "old": "C:\\Mail\\a.msg", "new": "C:\\Mail\\b.msg"}
    assert public_event(renamed) == {"type": "renamed"}
    finished = {"type": "scan_finished", "scan_id": 3, "to_parse": 2, "errors": 0, "paths": ["C:\\Mail"]}
    assert public_event(finished) == {"type": "scan_finished", "scan_id": 3, "to_parse": 2, "errors": 0}
    failed = {"type": "error", "step": "scan", "error": "PermissionError", "reason": "C:\\Mail", "flag": True}
    assert public_event(failed) == {"type": "error"}


def test_pick_folder_route_uses_picker(client, monkeypatch, tmp_path):
    monkeypatch.setattr("emailsupersearch.host.routes_sync.pick_folder", lambda: tmp_path)
    r = client.post("/api/pick-folder")
    assert r.status_code == 200 and r.json() == {"path": str(tmp_path)}
    monkeypatch.setattr("emailsupersearch.host.routes_sync.pick_folder", lambda: None)
    assert client.post("/api/pick-folder").json() == {"path": None}


def test_pick_folder_returns_none_on_failure(monkeypatch, caplog):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(folder_picker.subprocess, "run", timeout)
    with caplog.at_level(logging.WARNING, logger="emailsupersearch.host.folder_picker"):
        assert pick_folder(Path("C:/Mail")) is None
    assert [r.getMessage() for r in caplog.records] == ["the folder picker failed: TimeoutExpired"]

    calls = []

    def answer(stdout):
        def run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="")

        return run

    monkeypatch.setattr(folder_picker.subprocess, "run", answer(""))  # the dialog was cancelled
    assert pick_folder() is None
    monkeypatch.setattr(folder_picker.subprocess, "run", answer("C:/Mail/Archive\n"))
    assert pick_folder(Path("C:/Mail")) == Path("C:/Mail/Archive")
    (command,), options = calls[-1]
    assert command[1] == "-c" and options["timeout"] == 600 and '"initial": "C:\\\\Mail"' in options["input"]
    compile(folder_picker._SCRIPT, "<folder picker>", "exec")  # the child's script at least parses
