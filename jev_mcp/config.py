"""Environment-driven settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

REQUIRED_VARS = ("TYPESAFE_API_KEY", "JEV_OWNER_PASSWORD", "JEV_PUBLIC_URL")
MIN_PASSWORD_LENGTH = 12


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    typesafe_api_key: str
    owner_password: str
    public_url: str
    db_path: str = "/data/jev.db"
    host: str = "0.0.0.0"
    port: int = 8080
    default_model: str = "jev-latest"
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 2592000
    log_level: str = "info"

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        missing = [name for name in REQUIRED_VARS if not env.get(name, "").strip()]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

        owner_password = env["JEV_OWNER_PASSWORD"].strip()
        if len(owner_password) < MIN_PASSWORD_LENGTH:
            raise ConfigError(
                f"JEV_OWNER_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters; "
                "it is the only gate on authorizing an agent. "
                "Generate one with: openssl rand -base64 24"
            )

        public_url = env["JEV_PUBLIC_URL"].strip().rstrip("/")
        if not public_url.startswith("https://") and env.get("JEV_ALLOW_INSECURE_URL", "") != "1":
            raise ConfigError(
                "JEV_PUBLIC_URL must start with https:// "
                "(set JEV_ALLOW_INSECURE_URL=1 to allow http:// for local development)"
            )

        return cls(
            typesafe_api_key=env["TYPESAFE_API_KEY"].strip(),
            owner_password=owner_password,
            public_url=public_url,
            db_path=env.get("JEV_DB_PATH", "/data/jev.db").strip() or "/data/jev.db",
            host=env.get("JEV_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_int(env, "JEV_PORT", 8080),
            default_model=env.get("JEV_DEFAULT_MODEL", "jev-latest").strip() or "jev-latest",
            access_token_ttl=_int(env, "JEV_ACCESS_TOKEN_TTL", 3600),
            refresh_token_ttl=_int(env, "JEV_REFRESH_TOKEN_TTL", 2592000),
            log_level=env.get("JEV_LOG_LEVEL", "info").strip().lower() or "info",
        )


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
