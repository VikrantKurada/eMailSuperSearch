"""The host's security layer: loopback Host, foreign Origin, launch-token exchange, session and CSRF checks."""

NEW_PROJECT = {"name": "x", "folders": [], "language": "en"}


def test_bad_host_rejected(raw_client):
    assert raw_client.get("/health", headers={"Host": "evil.example"}).status_code == 400


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


def test_foreign_origin_rejected(client):
    assert client.post("/api/projects", json=NEW_PROJECT, headers={"Origin": "https://evil.example"}).status_code == 403


def test_shutdown_accepts_launch_header(raw_client, token):
    assert raw_client.post("/api/shutdown", headers={"X-ESS-Launch": token}).status_code == 204


def test_security_headers_present(client):
    assert client.get("/health").headers["x-content-type-options"] == "nosniff"
