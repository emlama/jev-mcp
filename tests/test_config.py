import logging

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


def test_unknown_log_level_rejected():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**REQUIRED, "JEV_LOG_LEVEL": "chatty"})
    assert "JEV_LOG_LEVEL" in str(exc.value)


def test_known_log_levels_accepted():
    for level in ("critical", "error", "warning", "info", "debug", "trace"):
        assert Settings.from_env({**REQUIRED, "JEV_LOG_LEVEL": level.upper()}).log_level == level


def test_stdlib_log_level_maps_trace_to_debug():
    # uvicorn understands "trace"; logging.basicConfig raises ValueError on it.
    assert Settings.from_env({**REQUIRED, "JEV_LOG_LEVEL": "trace"}).stdlib_log_level == "DEBUG"
    assert Settings.from_env({**REQUIRED, "JEV_LOG_LEVEL": "warning"}).stdlib_log_level == "WARNING"
    for level in ("critical", "error", "warning", "info", "debug", "trace"):
        name = Settings.from_env({**REQUIRED, "JEV_LOG_LEVEL": level}).stdlib_log_level
        assert isinstance(logging.getLevelName(name), int)  # a name basicConfig accepts


def test_public_url_with_a_path_rejected():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**REQUIRED, "JEV_PUBLIC_URL": "https://jev.example.com/jev"})
    assert "JEV_PUBLIC_URL" in str(exc.value)


def test_non_positive_port_rejected():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**REQUIRED, "JEV_PORT": "0"})
    assert "JEV_PORT" in str(exc.value)


def test_non_positive_access_token_ttl_rejected():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**REQUIRED, "JEV_ACCESS_TOKEN_TTL": "0"})
    assert "JEV_ACCESS_TOKEN_TTL" in str(exc.value)


def test_non_positive_refresh_token_ttl_rejected():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**REQUIRED, "JEV_REFRESH_TOKEN_TTL": "-1"})
    assert "JEV_REFRESH_TOKEN_TTL" in str(exc.value)


def test_negative_run_retention_rejected():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({**REQUIRED, "JEV_RUN_RETENTION_DAYS": "-1"})
    assert "JEV_RUN_RETENTION_DAYS" in str(exc.value)


def test_zero_run_retention_allowed():
    assert Settings.from_env({**REQUIRED, "JEV_RUN_RETENTION_DAYS": "0"}).run_retention_days == 0
