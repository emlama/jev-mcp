import sqlite3

import pytest

from jev_mcp.db import Database, utc_now

EXPECTED_TABLES = {
    "schema_version",
    "tools",
    "runs",
    "oauth_clients",
    "oauth_pending",
    "oauth_codes",
    "oauth_tokens",
}


def test_migrate_creates_tables_and_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "jev.db"))
    db.migrate()
    db.migrate()
    with db.tx() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert EXPECTED_TABLES <= names
    assert version == 1
    db.close()


def test_migrate_creates_parent_directory(tmp_path):
    db = Database(str(tmp_path / "nested" / "dir" / "jev.db"))
    db.migrate()
    assert (tmp_path / "nested" / "dir" / "jev.db").exists()
    db.close()


def test_tx_commits_and_rolls_back(tmp_path):
    db = Database(str(tmp_path / "jev.db"))
    db.migrate()
    with db.tx() as conn:
        conn.execute("INSERT INTO oauth_clients VALUES ('c1', 'name', '{}', 't')")
    with pytest.raises(RuntimeError):
        with db.tx() as conn:
            conn.execute("INSERT INTO oauth_clients VALUES ('c2', 'name', '{}', 't')")
            raise RuntimeError("boom")
    with db.tx() as conn:
        ids = [r["client_id"] for r in conn.execute("SELECT client_id FROM oauth_clients ORDER BY client_id")]
    assert ids == ["c1"]
    db.close()


def test_rows_are_dict_like(tmp_path):
    db = Database(str(tmp_path / "jev.db"))
    db.migrate()
    with db.tx() as conn:
        row = conn.execute("SELECT 1 AS one").fetchone()
    assert isinstance(row, sqlite3.Row)
    assert row["one"] == 1
    db.close()


def test_utc_now_format():
    value = utc_now()
    assert value.endswith("Z")
    assert "T" in value
