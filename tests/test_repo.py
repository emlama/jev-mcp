import pytest

from jev_mcp.db import Database
from jev_mcp.models import ToolSpec
from jev_mcp.repo import RunRepo, ToolExists, ToolNotFound, ToolRepo

SPEC = {
    "name": "email_triage",
    "title": "Email triage",
    "docs": "First line summary.\nMore detail.",
    "inputs": {"email": {"type": "object", "description": "The email."}},
    "context": {"policy": "Be nice."},
    "questions": {"urgent": {"type": "noul", "instructions": "Is `email` urgent?"}},
}


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "jev.db"))
    database.migrate()
    yield database
    database.close()


def test_create_and_get_round_trip(db):
    repo = ToolRepo(db)
    record = repo.create(ToolSpec.model_validate(SPEC), "agent-a")
    assert record.version == 1
    assert record.created_by == "agent-a"
    assert record.updated_by == "agent-a"
    fetched = repo.get("email_triage")
    assert fetched == record
    assert fetched.questions["urgent"].type == "noul"
    assert fetched.context == {"policy": "Be nice."}


def test_create_duplicate_raises(db):
    repo = ToolRepo(db)
    repo.create(ToolSpec.model_validate(SPEC), "a")
    with pytest.raises(ToolExists):
        repo.create(ToolSpec.model_validate(SPEC), "a")


def test_update_bumps_version_and_keeps_creator(db):
    repo = ToolRepo(db)
    created = repo.create(ToolSpec.model_validate(SPEC), "agent-a")
    updated = repo.update(ToolSpec.model_validate({**SPEC, "title": "New title"}), "agent-b")
    assert updated.version == 2
    assert updated.title == "New title"
    assert updated.created_by == "agent-a"
    assert updated.updated_by == "agent-b"
    assert updated.created_at == created.created_at


def test_update_missing_raises(db):
    with pytest.raises(ToolNotFound):
        ToolRepo(db).update(ToolSpec.model_validate(SPEC), "a")


def test_get_missing_raises(db):
    with pytest.raises(ToolNotFound):
        ToolRepo(db).get("nope")


def test_list_sorted_with_summary_and_query(db):
    repo = ToolRepo(db)
    repo.create(ToolSpec.model_validate({**SPEC, "name": "zeta_tool"}), "a")
    repo.create(ToolSpec.model_validate(SPEC), "a")
    names = [t.name for t in repo.list()]
    assert names == ["email_triage", "zeta_tool"]
    assert repo.list()[0].summary == "First line summary."
    assert [t.name for t in repo.list("ZETA")] == ["zeta_tool"]
    assert [t.name for t in repo.list("more detail")] == ["email_triage", "zeta_tool"]
    assert repo.list("nothing-matches") == []


def test_delete(db):
    repo = ToolRepo(db)
    repo.create(ToolSpec.model_validate(SPEC), "a")
    repo.delete("email_triage")
    with pytest.raises(ToolNotFound):
        repo.get("email_triage")
    with pytest.raises(ToolNotFound):
        repo.delete("email_triage")


def test_runs_add_and_recent(db):
    runs = RunRepo(db)
    first = runs.add(
        tool_name="email_triage", tool_version=1, client_id="a", inputs={"email": {}},
        answers={"urgent": {"type": "noul", "noul": 0.9}}, model="jev-1.13.0",
        input_tokens=10, output_tokens=2, latency_ms=120, error=None,
    )
    runs.add(
        tool_name=None, tool_version=None, client_id="b", inputs="raw text",
        answers=None, model="jev-latest", input_tokens=None, output_tokens=None,
        latency_ms=5, error="TypeSafe request failed with HTTP 429",
    )
    assert first.run_id and first.usage == {"input_tokens": 10, "output_tokens": 2}
    recent = runs.recent()
    assert [r.client_id for r in recent] == ["b", "a"]
    assert recent[0].error.startswith("TypeSafe")
    assert recent[0].inputs == "raw text"
    assert [r.run_id for r in runs.recent(tool_name="email_triage")] == [first.run_id]
    assert len(runs.recent(limit=1)) == 1


def test_recent_clamps_limit(db):
    runs = RunRepo(db)
    for _ in range(3):
        runs.add(tool_name=None, tool_version=None, client_id="a", inputs={}, answers={}, model="m",
                 input_tokens=1, output_tokens=1, latency_ms=1, error=None)
    assert len(runs.recent(limit=0)) == 1
    assert len(runs.recent(limit=10_000)) == 3
