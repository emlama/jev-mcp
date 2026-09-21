"""SQLite connection handling and schema migrations."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS tools (
            name TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            docs TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            context_json TEXT NOT NULL,
            questions_json TEXT NOT NULL,
            model TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_by TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            tool_name TEXT,
            tool_version INTEGER,
            client_id TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            answers_json TEXT,
            model TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            latency_ms INTEGER NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS runs_tool_created ON runs(tool_name, created_at)",
        "CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at)",
        """CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_name TEXT,
            client_info_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS oauth_pending (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            params_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS oauth_pending_expires ON oauth_pending(expires_at)",
        """CREATE TABLE IF NOT EXISTS oauth_codes (
            code_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            params_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            client_id TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            expires_at INTEGER,
            revoked INTEGER NOT NULL DEFAULT 0,
            family_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family_id)",
    ],
    2: [
        # Single-row table: consent failures are counted for the whole server, not per
        # pending request, so minting fresh pending requests cannot reset the counter.
        """CREATE TABLE IF NOT EXISTS oauth_lockout (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            failures INTEGER NOT NULL DEFAULT 0,
            locked_until INTEGER NOT NULL DEFAULT 0
        )""",
        "INSERT OR IGNORE INTO oauth_lockout (id) VALUES (1)",
        # Written by /healthz so the check proves the database is writable, not just readable.
        """CREATE TABLE IF NOT EXISTS healthcheck (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            touched_at INTEGER NOT NULL
        )""",
    ],
}


def utc_now() -> str:
    """Current time as ISO 8601 UTC with second precision and a Z suffix."""
    return _iso(datetime.now(UTC))


def utc_cutoff(age: timedelta) -> str:
    """A timestamp `age` in the past, in exactly the format utc_now() writes.

    Stored timestamps are ISO strings, so callers compare against this
    lexicographically; sharing the formatter keeps that comparison sound.
    """
    return _iso(datetime.now(UTC) - age)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    """One shared SQLite connection guarded by a lock.

    The server runs as a single process with low write volume, so a single
    connection in WAL mode is simpler and safer than a pool.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Context manager for a transactional block within BEGIN IMMEDIATE.

        Not reentrant: never call tx() while inside another tx() block on the same Database;
        pass the yielded connection down instead. A nested call deadlocks.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def migrate(self) -> None:
        with self.tx() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0
            for version in sorted(MIGRATIONS):
                if version <= current:
                    continue
                for statement in MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

    def close(self) -> None:
        self._conn.close()
