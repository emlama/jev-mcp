from tests.conftest import CALLBACK, McpClient, obtain_grant, pkce_pair, register


def test_metadata_endpoints(http, settings):
    auth_meta = http.get("/.well-known/oauth-authorization-server").json()
    assert auth_meta["issuer"].rstrip("/") == settings.public_url
    assert auth_meta["registration_endpoint"] == settings.public_url + "/register"
    assert auth_meta["code_challenge_methods_supported"] == ["S256"]
    assert "jev" in auth_meta["scopes_supported"]

    resource_meta = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource_meta["resource"] == settings.mcp_url
    assert resource_meta["scopes_supported"] == ["jev"]


def test_mcp_requires_bearer_token(http):
    response = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401
    assert "resource_metadata" in response.headers.get("www-authenticate", "")


def test_full_flow_yields_working_token(http, settings):
    grant = obtain_grant(http, settings)
    result = McpClient(http, grant.access_token).initialize()
    assert result["serverInfo"]["name"] == "jev-mcp"
    assert "list_tools" in result["instructions"]


def test_wrong_pkce_verifier_rejected(http, settings):
    client_id = register(http)
    _verifier, challenge = pkce_pair()
    authorize = http.get(
        "/authorize",
        params={
            "client_id": client_id, "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "redirect_uri": CALLBACK, "scope": "jev",
            "resource": settings.mcp_url,
        },
        follow_redirects=False,
    )
    request_id = authorize.headers["location"].split("request=", 1)[1]
    approved = http.post(
        "/consent", data={"request": request_id, "password": "correct-horse-battery"}, follow_redirects=False
    )
    code = approved.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    token = http.post(
        "/token",
        data={
            "grant_type": "authorization_code", "code": code, "code_verifier": "not-the-verifier",
            "client_id": client_id, "redirect_uri": CALLBACK, "resource": settings.mcp_url,
        },
    )
    assert token.status_code == 400
    assert token.json()["error"] == "invalid_grant"


def test_foreign_resource_rejected_at_authorize(http, settings):
    client_id = register(http)
    _verifier, challenge = pkce_pair()
    authorize = http.get(
        "/authorize",
        params={
            "client_id": client_id, "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "redirect_uri": CALLBACK, "scope": "jev",
            "resource": "https://evil.example.com/mcp",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 302
    assert "error=invalid_target" in authorize.headers["location"]


def test_refresh_rotation_and_revocation(http, settings):
    grant = obtain_grant(http, settings)
    refresh_data = {
        "grant_type": "refresh_token", "refresh_token": grant.refresh_token, "client_id": grant.client_id
    }
    refreshed = http.post("/token", data=refresh_data)
    assert refreshed.status_code == 200, refreshed.text
    new_tokens = refreshed.json()
    assert new_tokens["access_token"] != grant.access_token

    replay = http.post("/token", data=refresh_data)
    assert replay.status_code == 400

    assert McpClient(http, new_tokens["access_token"]).list_tools()["tools"]

    revoke = http.post("/revoke", data={"token": new_tokens["access_token"], "client_id": grant.client_id})
    assert revoke.status_code == 200
    denied = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Authorization": f"Bearer {new_tokens['access_token']}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert denied.status_code == 401


def test_healthz_is_open(http):
    response = http.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_healthz_proves_the_database_is_writable(http, db):
    assert http.get("/healthz").status_code == 200
    with db.tx() as conn:
        touched = conn.execute("SELECT touched_at FROM healthcheck WHERE id = 1").fetchone()
    assert touched["touched_at"] > 0

    db.close()  # a database it cannot write is not healthy
    response = http.get("/healthz")
    assert response.status_code == 503
    assert response.json()["ok"] is False
