"""Orchestration: validate definitions, build state, call TypeSafe, record runs."""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from jev_mcp.models import (
    QuestionMap,
    ToolRecord,
    ToolSpec,
    ToolSummary,
    format_validation_error,
    questions_to_wire,
)
from jev_mcp.paths import unresolved_roots
from jev_mcp.repo import RunRecord, RunRepo, ToolExists, ToolNotFound, ToolRepo
from jev_mcp.typesafe_client import JevClient, JevError

SPEC_FIELDS = ("title", "docs", "inputs", "context", "questions", "model")
MAX_RUN_INPUT_BYTES = 262_144
_QUESTIONS = TypeAdapter(QuestionMap)
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {"string": str, "object": dict, "array": list}


class ServiceError(Exception):
    """A failure whose message is written for the calling agent."""


class RunResult(BaseModel):
    tool: str | None
    version: int | None
    model: str
    answers: dict[str, Any]
    usage: dict[str, int | None]
    run_id: str


def validate_inputs(record: ToolRecord, inputs: Any) -> None:
    if not isinstance(inputs, dict):
        raise ServiceError("inputs must be a JSON object keyed by input name")
    unknown = sorted(set(inputs) - set(record.inputs))
    if unknown:
        raise ServiceError(
            f"unknown inputs: {', '.join(unknown)}; this tool accepts: {', '.join(sorted(record.inputs))}"
        )
    missing = sorted(name for name, spec in record.inputs.items() if spec.required and name not in inputs)
    if missing:
        raise ServiceError(f"missing required inputs: {', '.join(missing)}")
    for name, value in inputs.items():
        expected = record.inputs[name].type
        if not isinstance(value, _JSON_TYPES[expected]):
            raise ServiceError(f"input {name!r} must be a JSON {expected}, got {type(value).__name__}")


def check_size(payload: Any) -> None:
    """Reject inputs too big to be worth sending to TypeSafe or storing in a run row."""
    size = len(json.dumps(payload))
    if size > MAX_RUN_INPUT_BYTES:
        raise ServiceError(f"inputs are {size} bytes; the limit is {MAX_RUN_INPUT_BYTES}")


class ToolService:
    def __init__(
        self, tools: ToolRepo, runs: RunRepo, jev: JevClient, default_model: str = "jev-latest"
    ) -> None:
        self._tools = tools
        self._runs = runs
        self._jev = jev
        self._default_model = default_model

    # ----- definitions

    def create_tool(self, payload: dict[str, Any], client_id: str) -> ToolRecord:
        data = dict(payload)
        if not data.get("model"):
            data["model"] = self._default_model
        if data.get("context") is None:
            data["context"] = {}
        spec = self._validate_spec(data)
        try:
            return self._tools.create(spec, client_id)
        except ToolExists as exc:
            raise ServiceError(str(exc)) from exc

    def update_tool(self, name: str, changes: dict[str, Any], client_id: str) -> ToolRecord:
        existing = self.get_tool(name)
        applied = {key: value for key, value in changes.items() if value is not None}
        if not applied:
            raise ServiceError("update_tool needs at least one field to change")
        unknown = sorted(set(applied) - set(SPEC_FIELDS))
        if unknown:
            raise ServiceError(
                f"cannot update {', '.join(unknown)}; changeable fields: {', '.join(SPEC_FIELDS)}"
            )
        merged = existing.model_dump(mode="json", include={"name", *SPEC_FIELDS})
        merged.update(applied)
        spec = self._validate_spec(merged)
        try:
            return self._tools.update(spec, client_id)
        except ToolNotFound as exc:
            raise ServiceError(str(exc)) from exc

    def get_tool(self, name: str) -> ToolRecord:
        try:
            return self._tools.get(name)
        except ToolNotFound as exc:
            raise ServiceError(str(exc)) from exc

    def list_tools(self, query: str | None = None) -> list[ToolSummary]:
        return self._tools.list(query)

    def delete_tool(self, name: str) -> None:
        try:
            self._tools.delete(name)
        except ToolNotFound as exc:
            raise ServiceError(str(exc)) from exc

    def recent_runs(self, name: str | None = None, limit: int = 20) -> list[RunRecord]:
        return self._runs.recent(name, limit)

    # ----- execution

    async def run_tool(self, name: str, inputs: Any, client_id: str, model: str | None = None) -> RunResult:
        record = self.get_tool(name)
        validate_inputs(record, inputs)
        check_size(inputs)
        state = {**record.context, **inputs}
        return await self._call(
            state,
            questions_to_wire(record.questions),
            model or record.model,
            client_id,
            tool_name=record.name,
            tool_version=record.version,
            logged_inputs=inputs,
        )

    async def ask(
        self, state: Any, questions: dict[str, Any], client_id: str, model: str | None = None
    ) -> RunResult:
        check_size(state)
        try:
            validated = _QUESTIONS.validate_python(questions)
        except ValidationError as exc:
            raise ServiceError("Invalid questions:\n" + format_validation_error(exc)) from exc
        wire = questions_to_wire(validated)
        if isinstance(state, dict):
            problems = unresolved_roots(wire, set(state))
            if problems:
                listed = "; ".join(f"question {qid!r} references `{root}`" for qid, root in problems)
                raise ServiceError(
                    f"{listed}. Backticked names must be top-level keys of the state object "
                    f"(keys: {', '.join(sorted(state))}). Remove the backticks for plain words."
                )
        return await self._call(
            state,
            wire,
            model or self._default_model,
            client_id,
            tool_name=None,
            tool_version=None,
            logged_inputs=state,
        )

    async def _call(
        self,
        state: Any,
        wire_questions: dict[str, Any],
        model: str,
        client_id: str,
        *,
        tool_name: str | None,
        tool_version: int | None,
        logged_inputs: Any,
    ) -> RunResult:
        started = time.monotonic()
        try:
            result = await self._jev.ask(state, wire_questions, model)
        except JevError as exc:
            message = exc.agent_message()
            self._runs.add(
                tool_name=tool_name,
                tool_version=tool_version,
                client_id=client_id,
                inputs=logged_inputs,
                answers=None,
                model=model,
                input_tokens=None,
                output_tokens=None,
                latency_ms=_elapsed_ms(started),
                error=message,
            )
            raise ServiceError(message) from exc
        run = self._runs.add(
            tool_name=tool_name,
            tool_version=tool_version,
            client_id=client_id,
            inputs=logged_inputs,
            answers=result.answers,
            model=result.model,
            input_tokens=result.usage.get("input_tokens"),
            output_tokens=result.usage.get("output_tokens"),
            latency_ms=_elapsed_ms(started),
            error=None,
        )
        return RunResult(
            tool=tool_name,
            version=tool_version,
            model=result.model,
            answers=result.answers,
            usage=result.usage,
            run_id=run.run_id,
        )

    @staticmethod
    def _validate_spec(data: dict[str, Any]) -> ToolSpec:
        try:
            return ToolSpec.model_validate(data)
        except ValidationError as exc:
            raise ServiceError("Invalid tool definition:\n" + format_validation_error(exc)) from exc


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
