"""The host's security layer: loopback Host, foreign Origin, launch-token exchange, session and CSRF checks."""

import pytest

NEW_PROJECT = {"name": "x", "folders": [], "language": "en"}


def test_bad_host_rejected(raw_client):
    assert raw_client.get("/health", headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:#", "[::1]evil.com", "127.0.0.1.evil.com", "localhost:8765x", ""])
def test_malformed_host_rejected(client, host):
    r = client.get("/api/status", headers={"Host": host})
    assert r.status_code == 400 and r.json() == {"error": "bad host"}


@pytest.mark.parametrize("host", ["localhost:8765", "[::1]:8765", "LocalHost", "127.0.0.1:8765"])
def test_loopback_hosts_accepted(client, host):
    assert client.get("/api/status", headers={"Host": host}).status_code == 200


def test_api_requires_session(raw_client):
    assert raw_client.get("/api/status").status_code == 401


def test_auth_sets_cookies_and_redirects(raw_client, token):
    r = raw_client.get(f"/auth?token={token}", follow_redirects=False)
    sc = "\n".join(r.headers.get_list("set-cookie"))
    assert r.status_code in (302, 303, 307)
    assert "ess_session=" in sc and "HttpOnly" in sc and "ess_csrf=" in sc and "SameSite=Strict" in sc


def test_wrong_launch_token_rejected(raw_client):
    assert raw_client.get("/auth?token=nope", follow_redirects=False).status_code == 403


def test_post_requires_csrf_header_too(client_no_header):
    assert client_no_header.post("/api/projects", json=NEW_PROJECT).status_code == 401


def test_wrong_csrf_value_rejected(client_no_header):
    r = client_no_header.post("/api/projects", json=NEW_PROJECT, headers={"X-ESS-Token": "wrong"})
    assert r.status_code == 401 and r.json() == {"error": "bad csrf"}


def test_foreign_origin_rejected(client):
    assert client.post("/api/projects", json=NEW_PROJECT, headers={"Origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize(
    "origin", ["null", "http://[::1", "http://127.0.0.1:3000", "https://127.0.0.1", "http://127.0.0.1/x", "127.0.0.1"]
)
def test_origin_must_match_host(client, origin):
    r = client.get("/api/status", headers={"Origin": origin})
    assert r.status_code == 403 and r.json() == {"error": "foreign origin"}


def test_same_origin_accepted(client):
    assert client.get("/api/status", headers={"Origin": "http://127.0.0.1"}).status_code == 200
    headers = {"Host": "localhost:8765", "Origin": "http://localhost:8765"}
    assert client.get("/api/status", headers=headers).status_code == 200


def test_shutdown_accepts_launch_header(raw_client, token):
    assert raw_client.post("/api/shutdown", headers={"X-ESS-Launch": token}).status_code == 204


def test_shutdown_rejects_wrong_launch_header(raw_client):
    assert raw_client.post("/api/shutdown", headers={"X-ESS-Launch": "wrong"}).status_code == 401


def test_security_headers_present(client):
    headers = client.get("/health").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY" and headers["referrer-policy"] == "no-referrer"


def test_refusals_carry_security_headers(raw_client):
    refusals = [
        raw_client.get("/health", headers={"Host": "evil.example"}),
        raw_client.get("/health", headers={"Origin": "https://evil.example"}),
        raw_client.get("/api/status"),
    ]
    assert [r.status_code for r in refusals] == [400, 403, 401]
    for r in refusals:
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"


def test_endpoint_exception_is_json_500_with_headers(raw_client, token, host_services, monkeypatch):
    def boom():
        raise RuntimeError("secret detail")

    monkeypatch.setattr(host_services, "request_shutdown", boom)
    r = raw_client.post("/api/shutdown", headers={"X-ESS-Launch": token})
    assert r.status_code == 500 and r.json() == {"error": "internal error"}
    assert r.headers["x-content-type-options"] == "nosniff"
