import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from jev_mcp.config import Settings
from jev_mcp.db import Database
from jev_mcp.server import build_app
from tests.fakes import FakeJevClient

PUBLIC = "https://jev.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"

EMAIL_TOOL: dict[str, Any] = {
    "name": "email_triage",
    "title": "Email triage",
    "docs": (
        "Classifies a support email.\n\nUse when you have a raw email. `urgent` is P(urgent); "
        "treat > 0.7 as urgent."
    ),
    "inputs": {"email": {"type": "object", "description": "Object with subject and body."}},
    "context": {"policy": "Refunds within 30 days."},
    "questions": {
        "urgent": {"type": "noul", "instructions": "Does `email.body` express urgency?"},
        "dept": {
            "type": "choice",
            "instructions": "Which team should handle `email`, given `policy`?",
            "criteria": {"billing": "Money", "support": "Everything else"},
        },
    },
}


@dataclass
class Grant:
    client_id: str
    access_token: str
    refresh_token: str


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        typesafe_api_key="test-key",
        owner_password="hunter2",
        public_url=PUBLIC,
        db_path=str(tmp_path / "jev.db"),
    )


@pytest.fixture
def db(settings):
    database = Database(settings.db_path)
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def fake_jev() -> FakeJevClient:
    return FakeJevClient()


@pytest.fixture
def app(settings, db, fake_jev):
    return build_app(settings, jev=fake_jev, db=db)


@pytest.fixture
def http(app):
    with TestClient(app, base_url=PUBLIC) as client:
        yield client


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(40)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(http, client_name="Claude Desktop") -> str:
    response = http.post(
        "/register",
        json={
            "client_name": client_name,
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def obtain_grant(http, settings, client_name="Claude Desktop", resource: str | None = None) -> Grant:
    client_id = register(http, client_name)
    verifier, challenge = pkce_pair()
    params = {
        "client_id": client_id,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": CALLBACK,
        "state": "xyz",
        "scope": "jev",
        "resource": resource or settings.mcp_url,
    }
    authorize = http.get("/authorize", params=params, follow_redirects=False)
    assert authorize.status_code == 302, authorize.text
    consent_url = authorize.headers["location"]
    assert http.get(consent_url).status_code == 200
    request_id = parse_qs(urlparse(consent_url).query)["request"][0]
    approved = http.post(
        "/consent", data={"request": request_id, "password": settings.owner_password}, follow_redirects=False
    )
    assert approved.status_code == 302, approved.text
    query = parse_qs(urlparse(approved.headers["location"]).query)
    assert query["state"] == ["xyz"]
    token = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "resource": resource or settings.mcp_url,
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    return Grant(client_id=client_id, access_token=body["access_token"], refresh_token=body["refresh_token"])


@pytest.fixture
def grant(http, settings) -> Grant:
    return obtain_grant(http, settings)


class McpClient:
    def __init__(self, http, token: str) -> None:
        self.http = http
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._next_id = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        response = self.http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}},
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "error" not in body, body
        return body["result"]

    def initialize(self) -> dict[str, Any]:
        return self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "tests", "version": "0"},
            },
        )

    def list_tools(self) -> dict[str, Any]:
        return self.rpc("tools/list")

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def call_ok(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.call(name, arguments)
        assert result.get("isError") is False, result
        return result["structuredContent"]

    def call_error(self, name: str, arguments: dict[str, Any]) -> str:
        result = self.call(name, arguments)
        assert result.get("isError") is True, result
        return result["content"][0]["text"]


@pytest.fixture
def mcp(http, grant) -> McpClient:
    return McpClient(http, grant.access_token)
