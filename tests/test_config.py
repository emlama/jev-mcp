import pytest

from jev_mcp.config import ConfigError, Settings

REQUIRED = {
    "TYPESAFE_API_KEY": "ts_key",
    "JEV_OWNER_PASSWORD": "correct-horse-battery",
    "JEV_PUBLIC_URL": "https://jev.example.com/",
}


def test_from_env_reads_required_and_defaults():
    s = Settings.from_env(REQUIRED)
    assert s.typesafe_api_key == "ts_key"
    assert s.owner_password == "correct-horse-battery"
    assert s.public_url == "https://jev.example.com"  # trailing slash stripped
    assert s.mcp_url == "https://jev.example.com/mcp"
    assert s.db_path == "/data/jev.db"
    assert s.host == "0.0.0.0"
    assert s.port == 8080
    assert s.default_model == "jev-latest"
    assert s.access_token_ttl == 3600
    assert s.refresh_token_ttl == 2592000
    assert s.log_level == "info"


def test_from_env_reads_overrides():
    env = {**REQUIRED, "JEV_DB_PATH": "/tmp/x.db", "JEV_PORT": "9000", "JEV_ACCESS_TOKEN_TTL": "60"}
    s = Settings.from_env(env)
    assert s.db_path == "/tmp/x.db"
    assert s.port == 9000
    assert s.access_token_ttl == 60


def test_missing_required_lists_every_missing_name():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({"TYPESAFE_API_KEY": "k"})
    assert "JEV_OWNER_PASSWORD" in str(exc.value)
    assert "JEV_PUBLIC_URL" in str(exc.value)


def test_blank_value_counts_as_missing():
    with pytest.raises(ConfigError):
        Settings.from_env({**REQUIRED, "JEV_OWNER_PASSWORD": "   "})


def test_http_public_url_rejected_unless_escape_hatch():
    with pytest.raises(ConfigError):
        Settings.from_env({**REQUIRED, "JEV_PUBLIC_URL": "http://localhost:8080"})
    env = {**REQUIRED, "JEV_PUBLIC_URL": "http://localhost:8080", "JEV_ALLOW_INSECURE_URL": "1"}
    s = Settings.from_env(env)
    assert s.public_url == "http://localhost:8080"


def test_non_integer_port_is_config_error():
    with pytest.raises(ConfigError):
        Settings.from_env({**REQUIRED, "JEV_PORT": "eighty"})


def test_short_owner_password_rejected():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**REQUIRED, "JEV_OWNER_PASSWORD": "12345678901"})  # 11 characters
    message = str(exc.value)
    assert "JEV_OWNER_PASSWORD" in message
    assert "openssl rand -base64 24" in message


def test_twelve_character_owner_password_accepted():
    s = Settings.from_env({**REQUIRED, "JEV_OWNER_PASSWORD": "123456789012"})
    assert s.owner_password == "123456789012"
