from jev_mcp.paths import backtick_paths, path_root, unresolved_roots


def test_backtick_paths_walks_nested_structures():
    value = {
        "question": "Does `email.subject` match `policy.rules[0]`?",
        "extra": ["see `notes`", {"deep": "`a.b.c`"}],
        "number": 3,
    }
    assert backtick_paths(value) == {"email.subject", "policy.rules[0]", "notes", "a.b.c"}


def test_backtick_paths_ignores_empty_and_multiline():
    assert backtick_paths("`` and `multi\nline`") == set()


def test_path_root_extracts_identifier():
    assert path_root("email.subject") == "email"
    assert path_root("items[2].name") == "items"
    assert path_root(" spaced ") == "spaced"
    assert path_root('"quoted"') is None
    assert path_root("123abc") is None


def test_unresolved_roots_reports_question_and_root():
    questions = {
        "q1": {"type": "noul", "instructions": "Is `email.body` polite given `policy`?"},
        "q2": {"type": "choice", "instructions": "Pick", "criteria": {"a": "see `missing.field`", "b": None}},
        "q3": {"type": "score", "instructions": "Rate `email`", "criteria": ["low", "`other` high"]},
    }
    result = unresolved_roots(questions, {"email", "policy"})
    assert result == [("q2", "missing"), ("q3", "other")]


def test_backtick_paths_captures_non_identifier_characters():
    """Regression: backticked spans with hyphens and brackets should be captured."""
    result = backtick_paths('see `unknownfield.body-text` and `context["some-key"]`')
    assert result == {"unknownfield.body-text", 'context["some-key"]'}
    # path_root extracts leading identifier, ignoring characters that don't look like paths
    assert path_root("unknownfield.body-text") == "unknownfield"
    assert path_root('context["some-key"]') == "context"
    # unresolved_roots detects the leading identifier when not in known keys
    questions = {"q": {"type": "noul", "instructions": "Is `unknownfield.body-text` ok?"}}
    result = unresolved_roots(questions, {"email"})
    assert result == [("q", "unknownfield")]
