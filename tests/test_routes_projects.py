"""The project and status routes, driven through the real app with real supervisors."""

import os
from pathlib import Path

from emailsupersearch import __version__


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
