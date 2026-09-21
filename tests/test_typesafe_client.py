import json

import httpx2
import pytest
from typesafe_sdk import RetryPolicy

from jev_mcp.typesafe_client import JevError, TypeSafeJevClient, _strip_prefix

SUCCESS_BODY = {
    "model": "jev-1.13.0",
    "answers": {
        "urgent": {"type": "noul", "noul": 0.9},
        "dept": {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.8, "support": 0.2},
            "confidence": 0.7,
        },
        "anger": {
            "type": "score",
            "score": 1.4,
            "legend": {"0": "calm", "1": "angry"},
            "probabilities": {"0": 0.6, "1": 0.4},
            "confidence": 0.5,
        },
    },
    "usage": {"input_tokens": 300, "output_tokens": 12},
}


def make_client(handler) -> TypeSafeJevClient:
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return TypeSafeJevClient("test-key", http_client=http_client, retry=RetryPolicy(max_retries=0))


async def test_ask_sends_request_and_maps_response():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=SUCCESS_BODY)

    client = make_client(handler)
    result = await client.ask(
        {"email": "hi"},
        {"urgent": {"type": "noul", "instructions": "x"}},
        "jev-latest",
    )
    await client.aclose()

    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "jev-latest"
    assert seen["body"]["state"] == {"email": "hi"}
    assert seen["body"]["questions"]["urgent"]["type"] == "noul"
    assert result.model == "jev-1.13.0"
    assert result.usage == {"input_tokens": 300, "output_tokens": 12}
    assert result.answers["urgent"]["noul"] == 0.9
    assert result.answers["anger"]["legend"] == {"0": "calm", "1": "angry"}


async def test_rate_limit_maps_to_jev_error_with_retry_after():
    def handler(request):
        return httpx2.Response(429, json={"error": "slow down"}, headers={"retry-after": "7"})

    client = make_client(handler)
    with pytest.raises(JevError) as exc:
        await client.ask("x", {"q": {"type": "noul", "instructions": "x"}}, "jev-latest")
    await client.aclose()
    assert exc.value.status == 429
    assert exc.value.retry_after_seconds == 7.0
    message = exc.value.agent_message()
    assert "429" in message and "slow down" in message and "7 seconds" in message


async def test_server_error_maps_to_jev_error():
    def handler(request):
        return httpx2.Response(500, json={"message": "kaboom"})

    client = make_client(handler)
    with pytest.raises(JevError) as exc:
        await client.ask("x", {"q": {"type": "noul", "instructions": "x"}}, "jev-latest")
    await client.aclose()
    assert exc.value.status == 500
    assert exc.value.retry_after_seconds is None
    assert "kaboom" in exc.value.agent_message()


def test_agent_message_without_status():
    err = JevError("connection refused")
    assert err.agent_message() == "TypeSafe request failed: connection refused"


def test_strip_prefix_removes_endpoint_prefix():
    assert (
        _strip_prefix("POST https://api.typesafe.ai/v1/systemone: 429 slow down")
        == "429 slow down"
    )


def test_strip_prefix_leaves_other_colons_untouched():
    assert _strip_prefix("429 Model not found: jev-x") == "429 Model not found: jev-x"
