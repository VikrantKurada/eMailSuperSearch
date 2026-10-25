"""The project and status routes, driven through the real app with real supervisors."""

import asyncio
import os
from pathlib import Path

import pytest
from starlette.requests import Request

from emailsupersearch import __version__
from emailsupersearch.host.routes_projects import read_json, read_json_async


def test_project_crud_and_status(client, corpus_dir, host_services):
    r = client.post("/api/projects", json={"name": "Mail", "folders": [str(corpus_dir)], "language": "en"})
    assert r.status_code == 201
    created = r.json()
    pid = created["id"]
    assert created["name"] == "Mail" and Path(created["dir"]).is_dir()

    status = client.get("/api/status").json()
    assert status["version"] == __version__ and "embedder" in status["degraded"]
    entry = next(p for p in status["projects"] if p["id"] == pid)
    assert entry["name"] == "Mail" and entry["blocked"] is None
    assert entry["sync"]["project_id"] == pid and "state" in entry["sync"]

    assert client.get("/api/projects").json() == [created]
    assert client.get(f"/api/projects/{pid}").json()["folders"] == [os.path.normpath(str(corpus_dir))]

    r = client.patch(f"/api/projects/{pid}", json={"name": "Renamed"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    assert client.get(f"/api/projects/{pid}").json()["name"] == "Renamed"

    sup = host_services.supervisors[pid]
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert pid not in host_services.supervisors and sup.store.closed
    assert not Path(created["dir"]).exists()
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert client.get("/api/projects").json() == []


def test_saved_searches_put_get(client, project):
    url = f"/api/projects/{project}/saved-searches"
    assert client.get(url).json() == []
    searches = [{"name": "Invoices", "filters": {"query": "invoice", "attachment_kinds": ["pdf"]}}]
    r = client.put(url, json=searches)
    assert r.status_code == 200 and r.json() == searches
    assert client.get(url).json() == searches
    assert client.put(url, json=[{"name": 3, "filters": {}}]).status_code == 400
    assert client.put(url, json={"name": "x", "filters": {}}).status_code == 400
    assert client.get(url).json() == searches


def test_status_lists_blocked_project(client, host_services):
    blocked = host_services.catalog.create_project("Old", [])
    host_services.blocked_projects[blocked.id] = "schema too new"
    entry = next(p for p in client.get("/api/status").json()["projects"] if p["id"] == blocked.id)
    assert entry["blocked"] == "schema too new" and entry["sync"] is None
    assert client.get(f"/api/projects/{blocked.id}").json()["folders"] == []
    assert client.post(f"/api/projects/{blocked.id}/folders", json={"path": str(blocked.dir)}).status_code == 409


def test_folders_add_and_remove(client, project, corpus_dir, tmp_path):
    url = f"/api/projects/{project}/folders"
    extra = tmp_path / "more mail"
    extra.mkdir()
    r = client.post(url, json={"path": str(extra)})
    assert r.status_code == 200
    assert r.json()["folders"] == sorted([os.path.normpath(str(corpus_dir)), os.path.normpath(str(extra))])
    r = client.request("DELETE", url, json={"path": str(extra)})
    assert r.status_code == 200 and r.json()["folders"] == [os.path.normpath(str(corpus_dir))]
    assert client.post(url, json={"path": str(tmp_path / "missing")}).status_code == 400


def test_create_project_rejects_missing_folder(client, tmp_path):
    r = client.post("/api/projects", json={"name": "x", "folders": [str(tmp_path / "missing")], "language": "en"})
    assert r.status_code == 400 and "folders[0]" in r.json()["error"]
    assert client.get("/api/projects").json() == []


def test_create_with_failing_supervisor_reports_blocked(client, host_services, corpus_dir, monkeypatch):
    def fail(project_id):
        raise RuntimeError("cannot start")

    monkeypatch.setattr(host_services, "start_supervisor", fail)
    r = client.post("/api/projects", json={"name": "Mail", "folders": [str(corpus_dir)]})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert pid not in host_services.supervisors
    entry = next(p for p in client.get("/api/status").json()["projects"] if p["id"] == pid)
    assert entry["blocked"] == "RuntimeError" and entry["sync"] is None


def test_delete_failure_reports_blocked_until_retry(client, host_services, project, monkeypatch):
    def locked(project_id):
        raise PermissionError("in use")

    monkeypatch.setattr(host_services.catalog, "delete_project", locked)
    r = client.delete(f"/api/projects/{project}")
    assert r.status_code == 409 and r.json() == {"error": "project files are still in use; retry"}
    assert project not in host_services.supervisors
    entry = next(p for p in client.get("/api/status").json()["projects"] if p["id"] == project)
    assert entry["blocked"] == "delete failed; retry" and entry["sync"] is None

    monkeypatch.undo()
    assert client.delete(f"/api/projects/{project}").status_code == 204
    assert project not in host_services.blocked_projects
    assert client.get("/api/projects").json() == []


def test_read_json_needs_a_sync_endpoint_and_has_an_async_twin():
    async def receive():
        return {"type": "http.request", "body": b'{"a": 1}', "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    with pytest.raises(RuntimeError, match="sync endpoint"):
        read_json(Request(scope, receive))
    assert asyncio.run(read_json_async(Request(scope, receive))) == {"a": 1}


def test_create_project_takes_the_ocr_choice_before_its_supervisor_starts(client, corpus_dir, host_services):
    body = {"name": "No OCR", "folders": [str(corpus_dir)], "ocr_enabled": False}
    assert client.post("/api/projects", json={**body, "ocr_enabled": "no"}).status_code == 400
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201
    pid = r.json()["id"]
    sup = host_services.supervisors[pid]
    assert sup.store.get_setting("ocr_enabled") is False
    assert sup.ocr_runner is None and sup.pipeline.enqueue_ocr is None  # built without OCR from the start
    assert client.get(f"/api/projects/{pid}/settings").json()["ocr_enabled"] is False
    default = client.post("/api/projects", json={"name": "Default", "folders": [str(corpus_dir)]}).json()["id"]
    assert host_services.supervisors[default].store.get_setting("ocr_enabled") is True


def test_status_reports_a_missing_msg_handler_from_the_latest_doctor_run(client, host_services):
    import json

    def degraded():
        return client.get("/api/status").json()["degraded"]

    def record(checks):
        host_services.catalog.record_doctor_run(json.dumps({"checks": checks, "degraded": [], "generated_at": 1}))

    assert "msg_handler" not in degraded()  # no doctor run recorded
    record([{"name": "msg_handler", "status": "warn", "detail": "no .msg handler registered", "remedy": ""}])
    assert "msg_handler" in degraded()
    record([{"name": "msg_handler", "status": "ok", "detail": "ProgId Outlook.File.msg.15", "remedy": ""}])
    assert "msg_handler" not in degraded()  # the latest run wins
    record([{"name": "msg_handler", "status": "fail", "detail": "", "remedy": ""}])
    assert "msg_handler" in degraded()
    host_services.catalog.record_doctor_run("not json")
    assert "msg_handler" not in degraded()
    record([{"name": "python_build", "status": "ok", "detail": "", "remedy": ""}])  # no such check: off Windows
    assert "msg_handler" not in degraded()
