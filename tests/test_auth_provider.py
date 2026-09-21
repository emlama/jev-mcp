import pytest
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from jev_mcp.auth import provider as provider_module
from jev_mcp.auth.provider import (
    LOCKOUT_BASE_SECONDS,
    LOCKOUT_MAX_SECONDS,
    LOCKOUT_THRESHOLD,
    MAX_ATTEMPTS,
    SqliteOAuthProvider,
)
from jev_mcp.config import Settings
from jev_mcp.db import Database

PUBLIC = "https://jev.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def make_client(client_id="client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name="Claude Desktop",
        redirect_uris=[AnyUrl(CALLBACK)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
    )


def make_params(resource=PUBLIC + "/mcp") -> AuthorizationParams:
    return AuthorizationParams(
        state="state-123",
        scopes=["jev"],
        code_challenge="challenge",
        redirect_uri=AnyUrl(CALLBACK),
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        typesafe_api_key="k", owner_password="pw", public_url=PUBLIC, db_path=str(tmp_path / "jev.db"),
        access_token_ttl=3600, refresh_token_ttl=86400,
    )


@pytest.fixture
def provider(settings):
    db = Database(settings.db_path)
    db.migrate()
    yield SqliteOAuthProvider(db, settings)
    db.close()


def request_id_from(url: str) -> str:
    assert url.startswith(PUBLIC + "/consent?request=")
    return url.split("request=", 1)[1]


async def test_register_and_get_client(provider):
    await provider.register_client(make_client())
    fetched = await provider.get_client("client-1")
    assert fetched is not None and fetched.client_name == "Claude Desktop"
    assert provider.client_name("client-1") == "Claude Desktop"
    assert await provider.get_client("missing") is None


async def test_authorize_creates_pending_and_consent_url(provider):
    client = make_client()
    await provider.register_client(client)
    url = await provider.authorize(client, make_params())
    pending = provider.get_pending(request_id_from(url))
    assert pending is not None
    assert pending.client.client_id == "client-1"
    assert pending.params.state == "state-123"
    assert pending.attempts == 0


async def test_authorize_rejects_foreign_resource(provider):
    client = make_client()
    await provider.register_client(client)
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client, make_params(resource="https://other.example.com/mcp"))
    assert exc.value.error == "invalid_target"


async def test_authorize_accepts_missing_resource(provider):
    client = make_client()
    await provider.register_client(client)
    url = await provider.authorize(client, make_params(resource=None))
    assert provider.get_pending(request_id_from(url)) is not None


async def test_full_code_exchange_and_access_token(provider, settings):
    client = make_client()
    await provider.register_client(client)
    request_id = request_id_from(await provider.authorize(client, make_params()))

    redirect = provider.approve(request_id)
    assert redirect.startswith(CALLBACK + "?")
    assert "state=state-123" in redirect
    code = redirect.split("code=", 1)[1].split("&", 1)[0]
    assert provider.get_pending(request_id) is None

    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    assert auth_code.code_challenge == "challenge"
    assert auth_code.resource == settings.mcp_url
    assert await provider.load_authorization_code(make_client("other"), code) is None

    token = await provider.exchange_authorization_code(client, auth_code)
    assert token.token_type == "Bearer" and token.expires_in == 3600 and token.scope == "jev"
    assert token.refresh_token

    access = await provider.load_access_token(token.access_token)
    assert access is not None and access.client_id == "client-1" and access.resource == settings.mcp_url
    assert await provider.load_access_token("not-a-token") is None

    # a code is single use
    assert await provider.load_authorization_code(client, code) is None
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, auth_code)


async def issue(provider, client):
    request_id = request_id_from(await provider.authorize(client, make_params()))
    code = provider.approve(request_id).split("code=", 1)[1].split("&", 1)[0]
    auth_code = await provider.load_authorization_code(client, code)
    return await provider.exchange_authorization_code(client, auth_code)


async def test_refresh_rotates_and_revokes_old_refresh_token(provider):
    client = make_client()
    await provider.register_client(client)
    first = await issue(provider, client)

    refresh = await provider.load_refresh_token(client, first.refresh_token)
    assert refresh is not None and refresh.scopes == ["jev"]
    second = await provider.exchange_refresh_token(client, refresh, [])
    assert second.access_token != first.access_token
    assert second.refresh_token != first.refresh_token
    assert await provider.load_refresh_token(client, first.refresh_token) is None
    assert await provider.load_access_token(second.access_token) is not None

    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, [])


async def test_refresh_rejects_scope_escalation(provider):
    client = make_client()
    await provider.register_client(client)
    token = await issue(provider, client)
    refresh = await provider.load_refresh_token(client, token.refresh_token)
    with pytest.raises(TokenError) as exc:
        await provider.exchange_refresh_token(client, refresh, ["jev", "admin"])
    assert exc.value.error == "invalid_scope"


async def test_revoke_kills_whole_family(provider):
    client = make_client()
    await provider.register_client(client)
    token = await issue(provider, client)
    access = await provider.load_access_token(token.access_token)
    await provider.revoke_token(access)
    assert await provider.load_access_token(token.access_token) is None
    assert await provider.load_refresh_token(client, token.refresh_token) is None
    await provider.revoke_token(access)  # idempotent


async def test_failed_attempts_delete_pending(provider):
    client = make_client()
    await provider.register_client(client)
    request_id = request_id_from(await provider.authorize(client, make_params()))
    for attempt in range(1, MAX_ATTEMPTS):
        assert provider.record_failed_attempt(request_id) == attempt
        assert provider.get_pending(request_id) is not None
    assert provider.record_failed_attempt(request_id) == MAX_ATTEMPTS
    assert provider.get_pending(request_id) is None
    with pytest.raises(LookupError):
        provider.approve(request_id)


async def test_expiry_is_enforced(provider, monkeypatch):
    client = make_client()
    await provider.register_client(client)
    request_id = request_id_from(await provider.authorize(client, make_params()))
    token = await issue(provider, client)

    real_now = provider_module.now_s()
    monkeypatch.setattr(provider_module, "now_s", lambda: real_now + 10_000)
    assert provider.get_pending(request_id) is None
    assert await provider.load_access_token(token.access_token) is None
    assert await provider.load_refresh_token(client, token.refresh_token) is not None  # 86400 s TTL

    monkeypatch.setattr(provider_module, "now_s", lambda: real_now + 100_000)
    assert await provider.load_refresh_token(client, token.refresh_token) is None


async def test_register_client_rejects_oversized_metadata(provider):
    bloated = OAuthClientInformationFull(
        client_id="fat-client",
        client_name="x" * 9000,
        redirect_uris=[AnyUrl(CALLBACK)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code"],
    )
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(bloated)
    assert exc.value.error == "invalid_client_metadata"
    assert await provider.get_client("fat-client") is None


async def test_stale_clients_without_tokens_are_purged(provider):
    live, stale = make_client("live-client"), make_client("stale-client")
    await provider.register_client(live)
    await provider.register_client(stale)
    with provider._db.tx() as conn:  # both registered more than 24 hours ago
        conn.execute("UPDATE oauth_clients SET created_at = '2000-01-01T00:00:00Z'")
    fresh = make_client("fresh-client")
    await provider.register_client(fresh)

    await issue(provider, live)  # the code exchange purges

    assert await provider.get_client("live-client") is not None  # holds live tokens
    assert await provider.get_client("fresh-client") is not None  # registered just now
    assert await provider.get_client("stale-client") is None


async def test_global_failures_lock_consent_after_threshold(provider, monkeypatch):
    """Failures are counted globally, not per pending request."""
    monkeypatch.setattr(provider_module, "now_s", lambda: 1_000_000)
    assert provider.consent_locked_until() == 0
    for _ in range(LOCKOUT_THRESHOLD - 1):
        assert provider.record_global_failure() == 0
    assert provider.consent_locked_until() == 0

    locked_until = provider.record_global_failure()
    assert locked_until == 1_000_000 + LOCKOUT_BASE_SECONDS
    assert provider.consent_locked_until() == locked_until

    # the backoff doubles with each further failure and is capped
    assert provider.record_global_failure() == 1_000_000 + 2 * LOCKOUT_BASE_SECONDS
    for _ in range(20):
        provider.record_global_failure()
    assert provider.consent_locked_until() == 1_000_000 + LOCKOUT_MAX_SECONDS


async def test_reset_global_failures_clears_lockout(provider):
    for _ in range(LOCKOUT_THRESHOLD):
        provider.record_global_failure()
    assert provider.consent_locked_until() > 0
    provider.reset_global_failures()
    assert provider.consent_locked_until() == 0
    # the counter restarted: the next failure is not enough to lock again
    assert provider.record_global_failure() == 0


async def test_approve_is_atomic_rejects_deleted_pending(provider):
    """Regression test: approve must atomically load and consume the pending request.
    If the pending row is deleted (by record_failed_attempt reaching MAX_ATTEMPTS or TTL),
    approve must not issue a code."""
    client = make_client()
    await provider.register_client(client)
    request_id = request_id_from(await provider.authorize(client, make_params()))

    # Record failed attempts until the pending row is deleted
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        provider.record_failed_attempt(request_id)

    # Pending should now be gone
    assert provider.get_pending(request_id) is None

    # approve should raise LookupError and NOT insert a code
    with pytest.raises(LookupError):
        provider.approve(request_id)

    # Verify no code was inserted into oauth_codes
    with provider._db.tx() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM oauth_codes").fetchone()
        assert row["cnt"] == 0
