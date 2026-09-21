"""SQLite persistence for jev tools and their runs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from pydantic import BaseModel

from jev_mcp.db import Database, utc_now
from jev_mcp.models import ToolRecord, ToolSpec, ToolSummary, questions_to_wire


class ToolNotFound(LookupError):
    pass


class ToolExists(ValueError):
    pass


def _record(row: sqlite3.Row) -> ToolRecord:
    return ToolRecord(
        name=row["name"],
        title=row["title"],
        docs=row["docs"],
        inputs=json.loads(row["inputs_json"]),
        context=json.loads(row["context_json"]),
        questions=json.loads(row["questions_json"]),
        model=row["model"],
        version=row["version"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _summary_line(docs: str) -> str:
    for line in docs.splitlines():
        if line.strip():
            return line.strip()
    return ""


class ToolRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, spec: ToolSpec, client_id: str) -> ToolRecord:
        now = utc_now()
        with self._db.tx() as conn:
            if conn.execute("SELECT 1 FROM tools WHERE name = ?", (spec.name,)).fetchone():
                raise ToolExists(f"a tool named {spec.name!r} already exists; use update_tool to change it")
            conn.execute(
                "INSERT INTO tools (name, title, docs, inputs_json, context_json, questions_json, model, "
                "version, created_by, updated_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    spec.name,
                    spec.title,
                    spec.docs,
                    json.dumps({k: v.model_dump() for k, v in spec.inputs.items()}),
                    json.dumps(spec.context),
                    json.dumps(questions_to_wire(spec.questions)),
                    spec.model,
                    1,
                    client_id,
                    client_id,
                    now,
                    now,
                ),
            )
        return self.get(spec.name)

    def update(self, spec: ToolSpec, client_id: str) -> ToolRecord:
        now = utc_now()
        with self._db.tx() as conn:
            cur = conn.execute(
                "UPDATE tools SET title = ?, docs = ?, inputs_json = ?, context_json = ?, "
                "questions_json = ?, model = ?, version = version + 1, updated_by = ?, "
                "updated_at = ? WHERE name = ?",
                (
                    spec.title,
                    spec.docs,
                    json.dumps({k: v.model_dump() for k, v in spec.inputs.items()}),
                    json.dumps(spec.context),
                    json.dumps(questions_to_wire(spec.questions)),
                    spec.model,
                    client_id,
                    now,
                    spec.name,
                ),
            )
            if cur.rowcount == 0:
                raise ToolNotFound(f"no tool named {spec.name!r}; call list_tools to see what exists")
        return self.get(spec.name)

    def get(self, name: str) -> ToolRecord:
        with self._db.tx() as conn:
            row = conn.execute("SELECT * FROM tools WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ToolNotFound(f"no tool named {name!r}; call list_tools to see what exists")
        return _record(row)

    def list(self, query: str | None = None) -> list[ToolSummary]:
        with self._db.tx() as conn:
            rows = conn.execute(
                "SELECT name, title, docs, version, updated_at FROM tools ORDER BY name"
            ).fetchall()
        needle = (query or "").strip().lower()
        results: list[ToolSummary] = []
        for row in rows:
            haystack = f"{row['name']}\n{row['title']}\n{row['docs']}".lower()
            if needle and needle not in haystack:
                continue
            results.append(
                ToolSummary(
                    name=row["name"],
                    title=row["title"],
                    version=row["version"],
                    updated_at=row["updated_at"],
                    summary=_summary_line(row["docs"]),
                )
            )
        return results

    def delete(self, name: str) -> None:
        with self._db.tx() as conn:
            cur = conn.execute("DELETE FROM tools WHERE name = ?", (name,))
            if cur.rowcount == 0:
                raise ToolNotFound(f"no tool named {name!r}; call list_tools to see what exists")


class RunRecord(BaseModel):
    run_id: str
    tool_name: str | None
    tool_version: int | None
    client_id: str
    inputs: Any
    answers: dict[str, Any] | None
    model: str
    usage: dict[str, int | None]
    latency_ms: int
    error: str | None
    created_at: str


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["id"],
        tool_name=row["tool_name"],
        tool_version=row["tool_version"],
        client_id=row["client_id"],
        inputs=json.loads(row["inputs_json"]),
        answers=json.loads(row["answers_json"]) if row["answers_json"] is not None else None,
        model=row["model"],
        usage={"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]},
        latency_ms=row["latency_ms"],
        error=row["error"],
        created_at=row["created_at"],
    )


class RunRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        tool_name: str | None,
        tool_version: int | None,
        client_id: str,
        inputs: Any,
        answers: dict[str, Any] | None,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
        error: str | None,
    ) -> RunRecord:
        run_id = uuid.uuid4().hex
        with self._db.tx() as conn:
            conn.execute(
                "INSERT INTO runs (id, tool_name, tool_version, client_id, inputs_json, "
                "answers_json, model, input_tokens, output_tokens, latency_ms, error, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    tool_name,
                    tool_version,
                    client_id,
                    json.dumps(inputs),
                    json.dumps(answers) if answers is not None else None,
                    model,
                    input_tokens,
                    output_tokens,
                    latency_ms,
                    error,
                    utc_now(),
                ),
            )
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _run(row)

    def recent(self, tool_name: str | None = None, limit: int = 20) -> list[RunRecord]:
        limit = max(1, min(int(limit), 200))
        sql = "SELECT * FROM runs"
        params: tuple[Any, ...] = ()
        if tool_name is not None:
            sql += " WHERE tool_name = ?"
            params = (tool_name,)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self._db.tx() as conn:
            rows = conn.execute(sql, (*params, limit)).fetchall()
        return [_run(row) for row in rows]
