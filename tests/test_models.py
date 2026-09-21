import pytest
from pydantic import ValidationError

from jev_mcp.models import (
    MAX_DEFINITION_BYTES,
    ToolSpec,
    format_validation_error,
    questions_to_wire,
)

BASE = {
    "name": "email_triage",
    "title": "Email triage",
    "docs": "Classifies an email.\n\nUse when you have a raw email.",
    "inputs": {"email": {"type": "object", "description": "The email with subject and body."}},
    "context": {"policy": "Refunds within 30 days."},
    "questions": {
        "urgent": {"type": "noul", "instructions": "Does `email.body` express urgency?"},
        "dept": {
            "type": "choice",
            "instructions": "Which team handles `email`?",
            "criteria": {"billing": "Money", "support": None},
        },
        "anger": {
            "type": "score",
            "instructions": "How angry is `email.body`?",
            "criteria": ["calm", "angry"],
        },
    },
}


def spec(**overrides):
    return ToolSpec.model_validate({**BASE, **overrides})


def test_valid_spec_round_trips_and_defaults_model():
    s = spec()
    assert s.name == "email_triage"
    assert s.model == "jev-latest"
    assert s.inputs["email"].required is True
    assert s.questions["dept"].type == "choice"


def test_questions_to_wire_matches_typesafe_shape():
    wire = questions_to_wire(spec().questions)
    assert wire["urgent"] == {"type": "noul", "instructions": "Does `email.body` express urgency?"}
    assert wire["dept"]["criteria"] == {"billing": "Money", "support": None}
    assert wire["anger"]["criteria"] == ["calm", "angry"]


@pytest.mark.parametrize("bad_name", ["Email", "1abc", "ab", "a-b", "a" * 65, "email.triage"])
def test_bad_tool_name_rejected(bad_name):
    with pytest.raises(ValidationError):
        spec(name=bad_name)


def test_bad_input_name_rejected():
    with pytest.raises(ValidationError):
        spec(inputs={"Bad Name": {"type": "string", "description": "x"}})


def test_at_least_one_input_and_question():
    with pytest.raises(ValidationError):
        spec(inputs={})
    with pytest.raises(ValidationError):
        spec(questions={})


def test_docs_must_not_be_blank():
    with pytest.raises(ValidationError):
        spec(docs="   ")


def test_score_level_bounds():
    with pytest.raises(ValidationError):
        spec(questions={"s": {"type": "score", "instructions": "x", "criteria": ["only"]}})
    with pytest.raises(ValidationError):
        spec(questions={"s": {"type": "score", "instructions": "x", "criteria": [str(i) for i in range(11)]}})


def test_choice_option_bounds():
    with pytest.raises(ValidationError):
        spec(questions={"c": {"type": "choice", "instructions": "x", "criteria": {"one": None}}})
    too_many = {f"o{i}": None for i in range(256)}
    with pytest.raises(ValidationError):
        spec(questions={"c": {"type": "choice", "instructions": "x", "criteria": too_many}})


def test_unknown_question_type_rejected():
    with pytest.raises(ValidationError):
        spec(questions={"q": {"type": "rank", "instructions": "x"}})


def test_unresolved_backtick_path_rejected_with_helpful_message():
    with pytest.raises(ValidationError) as exc:
        spec(questions={"q": {"type": "noul", "instructions": "Is `subject` polite?"}})
    message = format_validation_error(exc.value)
    assert "subject" in message
    assert "email" in message and "policy" in message


def test_input_and_context_key_collision_rejected():
    with pytest.raises(ValidationError) as exc:
        spec(context={"email": "constant"})
    assert "email" in format_validation_error(exc.value)


def test_oversized_definition_rejected():
    with pytest.raises(ValidationError):
        spec(context={"blob": "x" * (MAX_DEFINITION_BYTES + 1)})


def test_format_validation_error_names_field_paths():
    with pytest.raises(ValidationError) as exc:
        spec(inputs={"email": {"type": "blob", "description": "x"}})
    message = format_validation_error(exc.value)
    assert "inputs.email.type" in message
