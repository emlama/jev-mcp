"""OAuth 2.1 authorization-server state stored in SQLite.

The MCP SDK owns the HTTP endpoints and protocol checks (PKCE, redirect URI,
client authentication). This class owns persistence and the consent handshake.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from jev_mcp.auth import tokens as tk
from jev_mcp.config import Settings
from jev_mcp.db import Database, utc_now

PENDING_TTL = 600
CODE_TTL = 600
MAX_ATTEMPTS = 5
SCOPE = "jev"


def now_s() -> int:  # indirection so tests can freeze time
    return tk.now_s()


@dataclass
class PendingAuth:
    id: str
    client: OAuthClientInformationFull
    params: AuthorizationParams
    attempts: int


class SqliteOAuthProvider:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    # ----- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT client_info_json FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["client_info_json"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, client_name, client_info_json, created_at) "
                "VALUES (?,?,?,?)",
                (client_info.client_id, client_info.client_name, client_info.model_dump_json(), utc_now()),
            )

    def client_name(self, client_id: str) -> str | None:
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT client_name FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return row["client_name"] if row else None

    # ----- authorization and consent

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self._settings.mcp_url:
            msg = f"this server only issues tokens for {self._settings.mcp_url}"
            raise AuthorizeError("invalid_target", msg)
        request_id = tk.new_token(16)
        with self._db.tx() as conn:
            self._purge(conn)
            conn.execute(
                "INSERT INTO oauth_pending (id, client_id, params_json, expires_at, attempts) "
                "VALUES (?,?,?,?,0)",
                (request_id, client.client_id, params.model_dump_json(), now_s() + PENDING_TTL),
            )
        return f"{self._settings.public_url}/consent?request={request_id}"

    def get_pending(self, request_id: str) -> PendingAuth | None:
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT p.id, p.params_json, p.attempts, c.client_info_json FROM oauth_pending p "
                "JOIN oauth_clients c ON c.client_id = p.client_id WHERE p.id = ? AND p.expires_at > ?",
                (request_id, now_s()),
            ).fetchone()
        if row is None:
            return None
        return PendingAuth(
            id=row["id"],
            client=OAuthClientInformationFull.model_validate_json(row["client_info_json"]),
            params=AuthorizationParams.model_validate_json(row["params_json"]),
            attempts=row["attempts"],
        )

    def record_failed_attempt(self, request_id: str) -> int:
        with self._db.tx() as conn:
            conn.execute("UPDATE oauth_pending SET attempts = attempts + 1 WHERE id = ?", (request_id,))
            row = conn.execute("SELECT attempts FROM oauth_pending WHERE id = ?", (request_id,)).fetchone()
            attempts = row["attempts"] if row else MAX_ATTEMPTS
            if attempts >= MAX_ATTEMPTS:
                conn.execute("DELETE FROM oauth_pending WHERE id = ?", (request_id,))
        return attempts

    def approve(self, request_id: str) -> str:
        pending = self.get_pending(request_id)
        if pending is None:
            raise LookupError("pending authorization not found or expired")
        code = tk.new_token(32)
        with self._db.tx() as conn:
            conn.execute("DELETE FROM oauth_pending WHERE id = ?", (request_id,))
            conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, params_json, expires_at, used) "
                "VALUES (?,?,?,?,0)",
                (
                    tk.hash_token(code),
                    pending.client.client_id,
                    pending.params.model_dump_json(),
                    now_s() + CODE_TTL,
                ),
            )
        return construct_redirect_uri(
            str(pending.params.redirect_uri), code=code, state=pending.params.state
        )

    # ----- authorization codes

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT params_json, expires_at FROM oauth_codes WHERE code_hash = ? AND client_id = ? "
                "AND used = 0 AND expires_at > ?",
                (tk.hash_token(authorization_code), client.client_id, now_s()),
            ).fetchone()
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row["params_json"])
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [SCOPE],
            expires_at=float(row["expires_at"]),
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self._settings.mcp_url,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        result = None
        with self._db.tx() as conn:
            cur = conn.execute(
                "UPDATE oauth_codes SET used = 1 WHERE code_hash = ? AND used = 0",
                (tk.hash_token(authorization_code.code),),
            )
            if cur.rowcount == 0:
                result = TokenError(
                    "invalid_grant", "authorization code is unknown or already used"
                )
            else:
                self._purge(conn)
                result = self._issue(
                    conn, client.client_id, authorization_code.scopes, family_id=tk.new_token(16)
                )
        if isinstance(result, TokenError):
            raise result
        return result

    # ----- refresh tokens

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT scopes_json, expires_at FROM oauth_tokens WHERE token_hash = ? "
                "AND kind = 'refresh' AND client_id = ? AND revoked = 0 "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (tk.hash_token(refresh_token), client.client_id, now_s()),
            ).fetchone()
        if row is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            resource=self._settings.mcp_url,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        requested = scopes or refresh_token.scopes
        if not set(requested) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "requested scopes exceed the original grant")
        token_hash = tk.hash_token(refresh_token.token)
        result = None
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT family_id FROM oauth_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            cur = conn.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ? AND revoked = 0",
                (token_hash,),
            )
            if row is None or cur.rowcount == 0:
                result = TokenError(
                    "invalid_grant", "refresh token is unknown, revoked, or already used"
                )
            else:
                self._purge(conn)
                result = self._issue(conn, client.client_id, requested, family_id=row["family_id"])
        if isinstance(result, TokenError):
            raise result
        return result

    # ----- access tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self._db.tx() as conn:
            row = conn.execute(
                "SELECT client_id, scopes_json, expires_at FROM oauth_tokens "
                "WHERE token_hash = ? AND kind = 'access' AND revoked = 0 "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (tk.hash_token(token), now_s()),
            ).fetchone()
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            resource=self._settings.mcp_url,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE family_id = "
                "(SELECT family_id FROM oauth_tokens WHERE token_hash = ?)",
                (tk.hash_token(token.token),),
            )

    # ----- helpers

    def _issue(
        self, conn: sqlite3.Connection, client_id: str, scopes: list[str], *, family_id: str
    ) -> OAuthToken:
        now = now_s()
        access, refresh = tk.new_token(), tk.new_token()
        scopes_json = json.dumps(scopes)
        conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, scopes_json, expires_at, "
            "revoked, family_id, created_at) VALUES (?,?,?,?,?,0,?,?)",
            (
                tk.hash_token(access),
                "access",
                client_id,
                scopes_json,
                now + self._settings.access_token_ttl,
                family_id,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, scopes_json, expires_at, "
            "revoked, family_id, created_at) VALUES (?,?,?,?,?,0,?,?)",
            (
                tk.hash_token(refresh),
                "refresh",
                client_id,
                scopes_json,
                now + self._settings.refresh_token_ttl,
                family_id,
                now,
            ),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self._settings.access_token_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    @staticmethod
    def _purge(conn: sqlite3.Connection) -> None:
        now = now_s()
        conn.execute("DELETE FROM oauth_pending WHERE expires_at <= ?", (now,))
        conn.execute("DELETE FROM oauth_codes WHERE expires_at <= ?", (now,))
        conn.execute("DELETE FROM oauth_tokens WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
