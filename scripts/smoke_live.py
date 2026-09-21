"""Opt-in smoke test against the real TypeSafe API.

Usage: TYPESAFE_API_KEY=... uv run python scripts/smoke_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from jev_mcp.typesafe_client import JevError, TypeSafeJevClient


async def main() -> int:
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        print("TYPESAFE_API_KEY is not set; skipping live smoke test")
        return 0
    client = TypeSafeJevClient(api_key)
    try:
        result = await client.ask(
            {"message": "Help! My payouts have been failing for 3 days."},
            {"urgent": {"type": "noul", "instructions": "Does `message` convey urgency?"}},
            os.environ.get("JEV_DEFAULT_MODEL", "jev-latest"),
        )
    except JevError as exc:
        print(exc.agent_message())
        return 1
    finally:
        await client.aclose()
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0 if result.answers["urgent"]["noul"] > 0.5 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
