"""The global and project settings routes and the purge request, driven through the real app."""

import pytest

from emailsupersearch import __version__
from emailsupersearch.catalog import Catalog
from emailsupersearch.paths import is_marked

_DEFAULT_PROJECT_SETTINGS = {
    "reconcile_interval_s": 900,
    "ocr_enabled": True,
    "ocr_engine_order": ["windows", "rapid"],
    "export_dir": None,
    "group_by_chain_default": True,
}


@pytest.fixture(autouse=True)
def _default_root(monkeypatch, tmp_path):
    """A ``data_root`` change is written to the default root's catalog too: keep that root inside the test folder."""
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(tmp_path / "default-root"))
    return tmp_path / "default-root"


def test_settings_roundtrip(client, host_services, tmp_path):
    catalog = host_services.catalog
    expected = {
        "data_root": str(catalog.root),
        "port": 8765,
        "log_level": "INFO",
        "autostart": {"enabled": False, "available": False},
        "version": __version__,
    }
    assert client.get("/api/settings").json() == expected

    r = client.put("/api/settings", json={"port": 9000, "log_level": "DEBUG"})
    assert r.status_code == 200
    expected.update(port=9000, log_level="DEBUG")
    assert r.json() == {**expected, "restart_required": True}
    assert client.get("/api/settings").json() == expected

    r = client.put("/api/settings", json={"log_level": "WARNING", "port": 9000})  # the port is unchanged
    expected.update(log_level="WARNING")
    assert r.status_code == 200 and r.json() == {**expected, "restart_required": False}

    new_root = str(tmp_path / "elsewhere")
    r = client.put("/api/settings", json={"data_root": new_root})
    # Stored for the next start; this host keeps serving from its current root.
    assert r.status_code == 200 and r.json() == {**expected, "restart_required": True}
    assert catalog.get_setting("data_root") == new_root

    for body in ({"port": "9000"}, {"port": True}, {"port": 80}, {"port": 65536}, {"log_level": "TRACE"},
                 {"data_root": ""}, {"data_root": 3}, [1]):
        r = client.put("/api/settings", json=body)
        assert r.status_code == 400 and r.json()["error"], body
    assert client.get("/api/settings").json() == expected


def test_data_root_change_reaches_the_default_catalog(client, host_services, tmp_path, _default_root):
    new_root = str(tmp_path / "elsewhere")
    assert client.put("/api/settings", json={"data_root": new_root}).status_code == 200
    assert host_services.catalog.get_setting("data_root") == new_root
    default = Catalog(_default_root)  # where the launcher looks for it at the next start
    try:
        assert default.get_setting("data_root") == new_root
    finally:
        default.close()
    assert is_marked(_default_root)


def test_settings_refuses_onedrive_data_root(client, host_services):
    r = client.put("/api/settings", json={"data_root": "C:\\Users\\x\\OneDrive\\ess"})
    assert r.status_code == 400
    error = r.json()["error"]
    assert error and "Users\\x" not in error
    assert host_services.catalog.get_setting("data_root") is None


def test_project_settings_roundtrip(client, project, host_services, tmp_path):
    url = f"/api/projects/{project}/settings"
    assert client.get(url).json() == _DEFAULT_PROJECT_SETTINGS
    changed = {
        "reconcile_interval_s": 600,
        "ocr_enabled": False,
        "ocr_engine_order": ["rapid", "windows"],
        "export_dir": str(tmp_path),
        "group_by_chain_default": False,
    }
    r = client.put(url, json=changed)
    assert r.status_code == 200 and r.json() == changed
    assert client.get(url).json() == changed
    assert host_services.supervisors[project].reconcile_interval_s == 600  # the next timer uses it
    r = client.put(url, json={"export_dir": None})
    assert r.status_code == 200 and r.json() == {**changed, "export_dir": None}


def test_project_settings_validation_400(client, project, host_services, tmp_path):
    url = f"/api/projects/{project}/settings"
    for body in (
        {"reconcile_interval_s": 59},
        {"reconcile_interval_s": 86401},
        {"reconcile_interval_s": "600"},
        {"reconcile_interval_s": True},
        {"ocr_enabled": "yes"},
        {"ocr_engine_order": ["windows", "windows"]},
        {"ocr_engine_order": ["tesseract"]},
        {"ocr_engine_order": "windows"},
        {"group_by_chain_default": 1},
        {"export_dir": 3},
        {"colour": "blue"},
        {"ocr_enabled": False, "colour": "blue"},
        [],
    ):
        r = client.put(url, json=body)
        assert r.status_code == 400 and r.json()["error"], body
    missing = str(tmp_path / "no such folder")
    r = client.put(url, json={"export_dir": missing})
    assert r.status_code == 400 and r.json() == {"error": "export_dir is not an existing folder"}
    assert client.get(url).json() == _DEFAULT_PROJECT_SETTINGS  # nothing was written
    assert host_services.supervisors[project].reconcile_interval_s == 900

    assert client.get("/api/projects/nope/settings").status_code == 404
    blocked = host_services.catalog.create_project("Old", [])
    host_services.blocked_projects[blocked.id] = "schema too new"
    assert client.get(f"/api/projects/{blocked.id}/settings").status_code == 409
    assert client.put(f"/api/projects/{blocked.id}/settings", json={"ocr_enabled": False}).status_code == 409


def test_purge_sets_flag_and_requests_shutdown(client, host_services):
    calls = []
    host_services.request_shutdown = lambda: calls.append("shutdown")
    r = client.post("/api/purge")
    assert r.status_code == 202 and r.json() == {"accepted": True}
    assert host_services.purge_requested is True and calls == ["shutdown"]
