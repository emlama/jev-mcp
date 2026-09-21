"""Thin wrapper around the TypeSafe SDK with an injectable protocol for tests."""

from __future__ import annotations

import re
from typing import Any, Protocol

import httpx2
from pydantic import BaseModel
from typesafe_sdk import (
    AsyncTypeSafeClient,
    RetryPolicy,
    TypeSafeAPIError,
    TypeSafeError,
    TypeSafeRateLimitError,
)

_ENDPOINT_PREFIX = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE) \S+: ")


class JevResult(BaseModel):
    model: str
    answers: dict[str, Any]
    usage: dict[str, int | None]


class JevError(Exception):
    """A TypeSafe call failed. `agent_message()` is safe to show to the calling agent."""

    def __init__(self, message: str, *, status: int | None = None, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds

    def agent_message(self) -> str:
        head = "TypeSafe request failed"
        if self.status is not None:
            head += f" with HTTP {self.status}"
        text = f"{head}: {self}"
        if self.retry_after_seconds is not None:
            text += f". Retry after {self.retry_after_seconds:.0f} seconds"
        return text


class JevClient(Protocol):
    async def ask(self, state: Any, questions: dict[str, Any], model: str) -> JevResult: ...

    async def aclose(self) -> None: ...


class TypeSafeJevClient:
    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx2.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._client = AsyncTypeSafeClient(api_key=api_key, http_client=http_client, retry=retry)

    async def ask(self, state: Any, questions: dict[str, Any], model: str) -> JevResult:
        try:
            response = await self._client.system_one(state=state, questions=questions, model=model)
        except TypeSafeRateLimitError as exc:
            retry_after = exc.retry_after_ms / 1000 if exc.retry_after_ms is not None else None
            raise JevError(
                _strip_prefix(str(exc)),
                status=exc.status,
                retry_after_seconds=retry_after,
            ) from exc
        except TypeSafeAPIError as exc:
            raise JevError(_strip_prefix(str(exc)), status=exc.status) from exc
        except TypeSafeError as exc:
            raise JevError(str(exc)) from exc
        return JevResult.model_validate(response.model_dump(mode="json"))

    async def aclose(self) -> None:
        await self._client.aclose()


def _strip_prefix(message: str) -> str:
    """The SDK formats API errors as 'POST <url>: <status> <message>'; keep the human part."""
    return _ENDPOINT_PREFIX.sub("", message, count=1)
