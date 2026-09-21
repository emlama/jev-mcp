from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from jev_mcp.auth import provider as provider_module
from jev_mcp.auth.consent import consent_handler
from jev_mcp.auth.provider import LOCKOUT_THRESHOLD, MAX_ATTEMPTS, SqliteOAuthProvider
from jev_mcp.config import Settings
from jev_mcp.db import Database

PUBLIC = "https://jev.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        typesafe_api_key="k",
        owner_password="correct-horse-battery",
        public_url=PUBLIC,
        db_path=str(tmp_path / "jev.db"),
    )


@pytest.fixture
def provider(settings):
    db = Database(settings.db_path)
    db.migrate()
    yield SqliteOAuthProvider(db, settings)
    db.close()


@pytest.fixture
def client(provider, settings):
    app = Starlette(routes=[Route("/consent", consent_handler(provider, settings), methods=["GET", "POST"])])
    return TestClient(app, base_url=PUBLIC)


async def pending_request(provider) -> str:
    client_info = OAuthClientInformationFull(
        client_id="client-1", client_name="Claude Desktop", redirect_uris=[AnyUrl(CALLBACK)],
        token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
    )
    await provider.register_client(client_info)
    params = AuthorizationParams(
        state="st", scopes=["jev"], code_challenge="ch", redirect_uri=AnyUrl(CALLBACK),
        redirect_uri_provided_explicitly=True, resource=PUBLIC + "/mcp",
    )
    url = await provider.authorize(client_info, params)
    return url.split("request=", 1)[1]


def test_get_unknown_request_is_400(client):
    response = client.get("/consent", params={"request": "nope"})
    assert response.status_code == 400
    assert "expired" in response.text.lower()


async def test_get_shows_client_and_form(client, provider):
    request_id = await pending_request(provider)
    response = client.get("/consent", params={"request": request_id})
    assert response.status_code == 200
    assert "Claude Desktop" in response.text
    assert "claude.ai" in response.text
    assert f'name="request" value="{request_id}"' in response.text
    assert 'type="password"' in response.text


async def test_wrong_password_shows_error_and_counts_down(client, provider):
    request_id = await pending_request(provider)
    response = client.post("/consent", data={"request": request_id, "password": "wrong"})
    assert response.status_code == 401
    assert "Wrong password" in response.text
    assert f"{MAX_ATTEMPTS - 1} attempt" in response.text


async def test_too_many_failures_locks_request(client, provider):
    request_id = await pending_request(provider)
    for _ in range(MAX_ATTEMPTS - 1):
        client.post("/consent", data={"request": request_id, "password": "wrong"})
    response = client.post("/consent", data={"request": request_id, "password": "wrong"})
    assert response.status_code == 403
    assert client.get("/consent", params={"request": request_id}).status_code == 400


async def test_correct_password_redirects_with_code_and_state(client, provider):
    request_id = await pending_request(provider)
    response = client.post(
        "/consent",
        data={"request": request_id, "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(CALLBACK + "?")
    query = parse_qs(urlparse(location).query)
    assert query["state"] == ["st"]
    assert query["code"][0]
    assert client.get("/consent", params={"request": request_id}).status_code == 400


def test_html_escapes_client_name(client, provider, settings):
    import asyncio

    async def register_evil():
        info = OAuthClientInformationFull(
            client_id="evil", client_name="<script>alert(1)</script>", redirect_uris=[AnyUrl(CALLBACK)],
            token_endpoint_auth_method="none", grant_types=["authorization_code"],
        )
        await provider.register_client(info)
        params = AuthorizationParams(
            state=None, scopes=["jev"], code_challenge="ch", redirect_uri=AnyUrl(CALLBACK),
            redirect_uri_provided_explicitly=False, resource=None,
        )
        return (await provider.authorize(info, params)).split("request=", 1)[1]

    request_id = asyncio.run(register_evil())
    response = client.get("/consent", params={"request": request_id})
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


async def test_global_lockout_spans_separate_pending_requests(client, provider):
    """Ten wrong passwords across ten different pending requests lock the consent page."""
    for _ in range(LOCKOUT_THRESHOLD):
        request_id = await pending_request(provider)
        response = client.post("/consent", data={"request": request_id, "password": "wrong"})
        assert response.status_code == 401, response.text

    fresh = await pending_request(provider)
    blocked = client.post("/consent", data={"request": fresh, "password": "wrong"})
    assert blocked.status_code == 429
    assert "Too many failed attempts" in blocked.text
    assert "Try again in 1 minute" in blocked.text

    # even the correct password is refused while the lockout is in force
    correct = client.post(
        "/consent",
        data={"request": fresh, "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert correct.status_code == 429


async def test_lockout_expiry_lets_the_owner_back_in(client, provider, monkeypatch):
    for _ in range(LOCKOUT_THRESHOLD):
        request_id = await pending_request(provider)
        client.post("/consent", data={"request": request_id, "password": "wrong"})
    locked_until = provider.consent_locked_until()
    assert locked_until > 0

    fresh = await pending_request(provider)
    monkeypatch.setattr(provider_module, "now_s", lambda: locked_until + 1)
    response = client.post(
        "/consent",
        data={"request": fresh, "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    # a successful consent clears the counter as well as the lock
    assert provider.consent_locked_until() == 0
    assert provider.record_global_failure() == 0


async def test_double_approval_returns_expired(client, provider):
    request_id = await pending_request(provider)
    # First approval succeeds
    response1 = client.post(
        "/consent",
        data={"request": request_id, "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response1.status_code == 302
    # Second submission of the same form should get 400 expired
    response2 = client.post(
        "/consent",
        data={"request": request_id, "password": "correct-horse-battery"},
        follow_redirects=False,
    )
    assert response2.status_code == 400
    assert "expired" in response2.text.lower()
