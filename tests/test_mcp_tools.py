from tests.conftest import EMAIL_TOOL, McpClient, obtain_grant

EXPECTED_TOOLS = {
    "ask_jev", "create_tool", "update_tool", "get_tool", "list_tools",
    "run_tool", "delete_tool", "tool_runs", "get_guide",
}


def test_lists_exactly_the_nine_tools(mcp):
    names = {tool["name"] for tool in mcp.list_tools()["tools"]}
    assert names == EXPECTED_TOOLS
    run_tool = next(t for t in mcp.list_tools()["tools"] if t["name"] == "run_tool")
    assert set(run_tool["inputSchema"]["required"]) == {"name", "inputs"}


def test_create_get_run_round_trip(mcp, fake_jev, grant):
    created = mcp.call_ok("create_tool", EMAIL_TOOL)
    assert created["name"] == "email_triage"
    assert created["version"] == 1
    assert created["created_by"] == grant.client_id
    assert created["created_by_name"] == "Claude Desktop"
    assert created["model"] == "jev-latest"

    fetched = mcp.call_ok("get_tool", {"name": "email_triage"})
    assert fetched["docs"].startswith("Classifies a support email.")
    assert fetched["inputs"]["email"]["type"] == "object"

    run = mcp.call_ok(
        "run_tool", {"name": "email_triage", "inputs": {"email": {"subject": "Help", "body": "Now!"}}}
    )
    assert run["tool"] == "email_triage" and run["version"] == 1
    assert run["answers"]["urgent"]["noul"] == 0.91
    assert run["run_id"]
    assert fake_jev.calls[-1]["state"] == {
        "policy": "Refunds within 30 days.",
        "email": {"subject": "Help", "body": "Now!"},
    }
    assert fake_jev.calls[-1]["questions"]["dept"]["criteria"] == {
        "billing": "Money", "support": "Everything else"
    }
    assert fake_jev.calls[-1]["model"] == "jev-latest"


def test_validation_errors_are_tool_errors_with_guidance(mcp):
    text = mcp.call_error("create_tool", {**EMAIL_TOOL, "name": "Bad Name"})
    assert "name" in text and "match" in text

    mcp.call_ok("create_tool", EMAIL_TOOL)
    text = mcp.call_error("run_tool", {"name": "email_triage", "inputs": {}})
    assert "missing required inputs: email" in text

    text = mcp.call_error("get_tool", {"name": "does_not_exist"})
    assert "list_tools" in text


def test_update_list_delete(mcp):
    mcp.call_ok("create_tool", EMAIL_TOOL)
    updated = mcp.call_ok("update_tool", {"name": "email_triage", "title": "Support email triage"})
    assert updated["version"] == 2 and updated["title"] == "Support email triage"

    listing = mcp.call_ok("list_tools", {})
    assert [t["name"] for t in listing["tools"]] == ["email_triage"]
    assert listing["tools"][0]["summary"] == "Classifies a support email."
    assert mcp.call_ok("list_tools", {"query": "nothing"})["tools"] == []

    assert mcp.call_ok("delete_tool", {"name": "email_triage"}) == {"deleted": True, "name": "email_triage"}
    assert mcp.call_ok("list_tools", {})["tools"] == []


def test_ask_jev_and_tool_runs_show_agent_names(http, settings, mcp, fake_jev):
    result = mcp.call_ok(
        "ask_jev",
        {
            "state": {"text": "Refund me now"},
            "questions": {"angry": {"type": "noul", "instructions": "Is `text` angry?"}},
        },
    )
    assert result["tool"] is None
    assert result["answers"]["urgent"]["noul"] == 0.91  # canned fake answer
    assert fake_jev.calls[-1]["state"] == {"text": "Refund me now"}

    other = obtain_grant(http, settings, client_name="Claude Code")
    McpClient(http, other.access_token).call_ok("create_tool", EMAIL_TOOL)
    McpClient(http, other.access_token).call_ok("run_tool", {"name": "email_triage", "inputs": {"email": {}}})

    runs = mcp.call_ok("tool_runs", {})["runs"]
    assert [r["client_name"] for r in runs] == ["Claude Code", "Claude Desktop"]
    assert runs[0]["tool_name"] == "email_triage"
    assert runs[1]["tool_name"] is None
    only_tool = mcp.call_ok("tool_runs", {"name": "email_triage", "limit": 5})["runs"]
    assert len(only_tool) == 1


def test_typesafe_failure_is_reported_and_logged(settings, db):
    from starlette.testclient import TestClient

    from jev_mcp.server import build_app
    from jev_mcp.typesafe_client import JevError
    from tests.fakes import FakeJevClient

    failing = FakeJevClient(error=JevError("slow down", status=429, retry_after_seconds=3))
    app = build_app(settings, jev=failing, db=db)
    with TestClient(app, base_url=settings.public_url) as client:
        grant = obtain_grant(client, settings)
        mcp = McpClient(client, grant.access_token)
        text = mcp.call_error(
            "ask_jev", {"state": "x", "questions": {"q": {"type": "noul", "instructions": "ok?"}}}
        )
        assert "HTTP 429" in text and "3 seconds" in text
        runs = mcp.call_ok("tool_runs", {})["runs"]
        assert runs[0]["error"] and "429" in runs[0]["error"]


def test_tool_runs_empty_name_means_unfiltered(mcp):
    mcp.call_ok(
        "ask_jev",
        {
            "state": {"text": "hello"},
            "questions": {"q": {"type": "noul", "instructions": "Is `text` polite?"}},
        },
    )
    mcp.call_ok("create_tool", EMAIL_TOOL)
    mcp.call_ok("run_tool", {"name": "email_triage", "inputs": {"email": {}}})

    everything = mcp.call_ok("tool_runs", {"name": ""})["runs"]
    assert [r["tool_name"] for r in everything] == ["email_triage", None]
    assert everything == mcp.call_ok("tool_runs", {})["runs"]

    only_tool = mcp.call_ok("tool_runs", {"name": "email_triage"})["runs"]
    assert [r["tool_name"] for r in only_tool] == ["email_triage"]


def test_descriptions_document_criteria_shapes(mcp):
    tools = {t["name"]: t["description"] for t in mcp.list_tools()["tools"]}
    for shape in ('"true"', '"false"', "option", "ordered levels"):
        assert shape in tools["ask_jev"], shape
    assert "same three shapes as ask_jev" in tools["create_tool"]
    assert "not recorded" in tools["tool_runs"]
