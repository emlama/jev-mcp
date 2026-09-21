from jev_mcp.guide import ADAPTER, GUIDE_URI, load_guide

TOOL_NAMES = [
    "ask_jev", "create_tool", "update_tool", "get_tool",
    "list_tools", "run_tool", "delete_tool", "tool_runs",
]


def test_guide_contains_adapter_then_upstream_skill_then_license():
    guide = load_guide()
    assert guide.startswith(ADAPTER)
    upstream_start = guide.index("name: typesafe-ai")
    assert upstream_start > len(ADAPTER) - 10
    assert "System One" in guide
    assert "MIT License" in guide and "Copyright (c) 2026 TypeSafe AI" in guide
    assert "Source: https://raw.githubusercontent.com/typesafe-ai/skills" in guide


def test_adapter_names_every_tool_and_all_three_shapes():
    for name in TOOL_NAMES:
        assert f"`{name}`" in ADAPTER, name
    for shape in ('"type": "noul"', '"type": "choice"', '"type": "score"', '"true"', '"false"'):
        assert shape in ADAPTER, shape


def test_get_guide_tool_and_resource_serve_the_same_document(mcp):
    via_tool = mcp.call_ok("get_guide", {})
    assert via_tool["uri"] == GUIDE_URI
    assert via_tool["guide"] == load_guide()

    listed = mcp.rpc("resources/list", {})["resources"]
    assert [r["uri"] for r in listed] == [GUIDE_URI]
    contents = mcp.rpc("resources/read", {"uri": GUIDE_URI})["contents"]
    assert contents[0]["mimeType"] == "text/markdown"
    assert contents[0]["text"] == load_guide()


def test_instructions_point_agents_at_the_guide(mcp):
    assert "get_guide" in mcp.initialize()["instructions"]
