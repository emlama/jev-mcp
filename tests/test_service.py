import pytest

from jev_mcp.db import Database
from jev_mcp.repo import RunRepo, ToolRepo
from jev_mcp.service import MAX_RUN_INPUT_BYTES, ServiceError, ToolService
from jev_mcp.typesafe_client import JevError
from tests.fakes import FakeJevClient

PAYLOAD = {
    "name": "email_triage",
    "title": "Email triage",
    "docs": "Classifies an email.",
    "inputs": {
        "email": {"type": "object", "description": "Subject and body."},
        "note": {"type": "string", "description": "Optional note.", "required": False},
    },
    "context": {"policy": "Refunds within 30 days."},
    "questions": {"urgent": {"type": "noul", "instructions": "Is `email.body` urgent given `policy`?"}},
}


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "jev.db"))
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def jev():
    return FakeJevClient()


@pytest.fixture
def service(db, jev):
    return ToolService(ToolRepo(db), RunRepo(db), jev, default_model="jev-latest")


def test_create_applies_default_model(service):
    record = service.create_tool(PAYLOAD, "agent-a")
    assert record.model == "jev-latest"
    assert record.version == 1


def test_create_with_none_model_uses_default(service):
    record = service.create_tool({**PAYLOAD, "model": None}, "agent-a")
    assert record.model == "jev-latest"


def test_create_duplicate_is_service_error(service):
    service.create_tool(PAYLOAD, "a")
    with pytest.raises(ServiceError, match="already exists"):
        service.create_tool(PAYLOAD, "a")


def test_create_invalid_reports_field_path(service):
    with pytest.raises(ServiceError) as exc:
        service.create_tool({**PAYLOAD, "inputs": {"email": {"type": "blob", "description": "x"}}}, "a")
    assert "inputs.email.type" in str(exc.value)


def test_update_merges_and_bumps_version(service):
    service.create_tool(PAYLOAD, "a")
    updated = service.update_tool("email_triage", {"title": "Better title", "docs": None}, "b")
    assert updated.version == 2
    assert updated.title == "Better title"
    assert updated.docs == "Classifies an email."
    assert updated.updated_by == "b"


def test_update_requires_a_change(service):
    service.create_tool(PAYLOAD, "a")
    with pytest.raises(ServiceError, match="at least one field"):
        service.update_tool("email_triage", {"title": None}, "a")


def test_update_validates_merged_result(service):
    service.create_tool(PAYLOAD, "a")
    with pytest.raises(ServiceError, match="policy"):
        service.update_tool("email_triage", {"context": {}}, "a")  # question still references `policy`


def test_get_missing_is_service_error(service):
    with pytest.raises(ServiceError, match="list_tools"):
        service.get_tool("nope")


async def test_run_tool_builds_state_and_logs_run(service, jev):
    service.create_tool(PAYLOAD, "a")
    result = await service.run_tool("email_triage", {"email": {"body": "help!"}}, "agent-b")
    assert jev.calls == [
        {
            "state": {"policy": "Refunds within 30 days.", "email": {"body": "help!"}},
            "questions": {
                "urgent": {"type": "noul", "instructions": "Is `email.body` urgent given `policy`?"}
            },
            "model": "jev-latest",
        }
    ]
    assert result.tool == "email_triage" and result.version == 1
    assert result.answers["urgent"]["noul"] == 0.91
    run = service.recent_runs("email_triage")[0]
    assert run.run_id == result.run_id
    assert run.client_id == "agent-b"
    assert run.inputs == {"email": {"body": "help!"}}
    assert run.usage == {"input_tokens": 120, "output_tokens": 4}
    assert run.error is None


async def test_run_tool_model_override(service, jev):
    service.create_tool(PAYLOAD, "a")
    await service.run_tool("email_triage", {"email": {}}, "a", model="jev-1.13.0")
    assert jev.calls[0]["model"] == "jev-1.13.0"


async def test_run_tool_rejects_missing_unknown_and_wrong_type_before_calling(service, jev):
    service.create_tool(PAYLOAD, "a")
    with pytest.raises(ServiceError, match="missing required inputs: email"):
        await service.run_tool("email_triage", {}, "a")
    with pytest.raises(ServiceError, match="unknown inputs: bogus"):
        await service.run_tool("email_triage", {"email": {}, "bogus": 1}, "a")
    with pytest.raises(ServiceError, match="'email' must be a JSON object"):
        await service.run_tool("email_triage", {"email": "text"}, "a")
    with pytest.raises(ServiceError, match="inputs must be a JSON object"):
        await service.run_tool("email_triage", ["not", "a", "dict"], "a")
    assert jev.calls == []


async def test_run_tool_surfaces_jev_error_and_logs_it(db):
    jev = FakeJevClient(error=JevError("slow down", status=429, retry_after_seconds=7))
    service = ToolService(ToolRepo(db), RunRepo(db), jev)
    service.create_tool(PAYLOAD, "a")
    with pytest.raises(ServiceError, match="HTTP 429"):
        await service.run_tool("email_triage", {"email": {}}, "a")
    run = service.recent_runs()[0]
    assert run.answers is None
    assert "429" in run.error


async def test_ask_validates_questions_checks_paths_and_logs(service, jev):
    with pytest.raises(ServiceError, match="type"):
        await service.ask({"x": 1}, {"q": {"type": "rank", "instructions": "x"}}, "a")
    with pytest.raises(ServiceError, match="`missing`"):
        await service.ask(
            {"x": 1}, {"q": {"type": "noul", "instructions": "Is `missing` ok?"}}, "a"
        )
    result = await service.ask(
        "plain text", {"q": {"type": "noul", "instructions": "Is `anything` ok?"}}, "a"
    )
    assert result.tool is None and result.version is None
    assert jev.calls[-1]["state"] == "plain text"
    run = service.recent_runs()[0]
    assert run.tool_name is None and run.run_id == result.run_id


def test_delete_and_list(service):
    service.create_tool(PAYLOAD, "a")
    assert [t.name for t in service.list_tools()] == ["email_triage"]
    service.delete_tool("email_triage")
    assert service.list_tools() == []
    with pytest.raises(ServiceError):
        service.delete_tool("email_triage")


async def test_run_tool_rejects_oversized_inputs(service, jev):
    service.create_tool(PAYLOAD, "a")
    oversized = {"email": {"body": "x" * (MAX_RUN_INPUT_BYTES + 1)}}
    with pytest.raises(ServiceError, match="the limit is 262144"):
        await service.run_tool("email_triage", oversized, "a")
    assert jev.calls == []


async def test_ask_rejects_oversized_state(service, jev):
    oversized = {"blob": "x" * (MAX_RUN_INPUT_BYTES + 1)}
    with pytest.raises(ServiceError, match="the limit is 262144"):
        await service.ask(oversized, {"q": {"type": "noul", "instructions": "Is `blob` big?"}}, "a")
    assert jev.calls == []
