from typing import Any

from jev_mcp.typesafe_client import JevError, JevResult

DEFAULT_RESULT = JevResult(
    model="jev-1.13.0",
    answers={"urgent": {"type": "noul", "noul": 0.91}},
    usage={"input_tokens": 120, "output_tokens": 4},
)


class FakeJevClient:
    """Records every call and returns a canned result, or raises a canned error."""

    def __init__(self, result: JevResult | None = None, error: JevError | None = None) -> None:
        self.result = result or DEFAULT_RESULT
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def ask(self, state: Any, questions: dict[str, Any], model: str) -> JevResult:
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        return None
