"""Pydantic models for jev tool definitions, mirroring the TypeSafe request shape."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jev_mcp.paths import unresolved_roots

SLUG_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
SLUG_RE = re.compile(SLUG_PATTERN)
MAX_DEFINITION_BYTES = 200_000

JsonContent = str | dict[str, Any] | list[Any]


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "object", "array"]
    description: str = Field(min_length=1)
    required: bool = True


class NoulCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    true: JsonContent | None = None
    false: JsonContent | None = None


class NoulQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["noul"]
    instructions: JsonContent
    criteria: NoulCriteria | None = None


class ChoiceQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["choice"]
    instructions: JsonContent
    criteria: dict[str, JsonContent | None]

    @field_validator("criteria")
    @classmethod
    def _option_count(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not 2 <= len(value) <= 255:
            raise ValueError(f"a choice needs between 2 and 255 options, got {len(value)}")
        return value


class ScoreQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["score"]
    instructions: JsonContent
    criteria: list[JsonContent]

    @field_validator("criteria")
    @classmethod
    def _level_count(cls, value: list[Any]) -> list[Any]:
        if not 2 <= len(value) <= 10:
            raise ValueError(f"a score needs between 2 and 10 levels, got {len(value)}")
        return value


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]
QuestionMap = dict[str, Question]


def questions_to_wire(questions: QuestionMap) -> dict[str, Any]:
    """Serialize validated questions to the exact JSON TypeSafe expects."""
    return {qid: q.model_dump(mode="json", exclude_none=True) for qid, q in questions.items()}


class ToolSpec(BaseModel):
    """What an agent submits to create or fully replace a jev tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(min_length=1)
    docs: str
    inputs: dict[str, InputSpec] = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    questions: QuestionMap = Field(min_length=1)
    model: str = Field(default="jev-latest", min_length=1)

    @field_validator("docs")
    @classmethod
    def _docs_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("docs must describe the tool: purpose, when to use it, how to read the answers")
        return value.strip()

    @field_validator("inputs")
    @classmethod
    def _input_names(cls, value: dict[str, InputSpec]) -> dict[str, InputSpec]:
        for name in value:
            if not SLUG_RE.match(name):
                raise ValueError(f"input name {name!r} must match {SLUG_PATTERN}")
        return value

    @field_validator("questions")
    @classmethod
    def _question_ids(cls, value: QuestionMap) -> QuestionMap:
        for qid in value:
            if not qid.strip():
                raise ValueError("question ids must not be blank")
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> ToolSpec:
        collisions = sorted(set(self.inputs) & set(self.context))
        if collisions:
            raise ValueError(
                f"these names are both inputs and context keys: {', '.join(collisions)}; rename one side"
            )

        known = set(self.inputs) | set(self.context)
        problems = unresolved_roots(questions_to_wire(self.questions), known)
        if problems:
            listed = "; ".join(f"question {qid!r} references `{root}`" for qid, root in problems)
            raise ValueError(
                f"{listed}. Backticked names must start with a declared input or context key "
                f"(declared: {', '.join(sorted(known))}). Remove the backticks for plain words."
            )

        size = len(json.dumps(self.context)) + len(json.dumps(questions_to_wire(self.questions)))
        if size > MAX_DEFINITION_BYTES:
            raise ValueError(f"context plus questions is {size} bytes; the limit is {MAX_DEFINITION_BYTES}")
        return self


class ToolRecord(ToolSpec):
    """A persisted jev tool."""

    version: int
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str


class ToolSummary(BaseModel):
    name: str
    title: str
    version: int
    updated_at: str
    summary: str


def format_validation_error(exc: ValidationError) -> str:
    """One line per problem, prefixed with the dotted field path, readable by an agent."""
    lines = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err["loc"] if part not in ("function-after",))
        msg = err["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        lines.append(f"{loc}: {msg}" if loc else msg)
    return "\n".join(lines)
