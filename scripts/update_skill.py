"""Refresh the vendored TypeSafe agent skill from its upstream repository.

Usage: uv run python scripts/update_skill.py

The skill is MIT licensed by TypeSafe AI and shipped verbatim so that hosted
agents (which cannot install plugins) can read it through the `get_guide`
tool. Run this after upstream publishes changes, review the diff, and commit.
"""

from __future__ import annotations

import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://raw.githubusercontent.com/typesafe-ai/skills/main/skills/typesafe-ai/"
FILES = ("SKILL.md", "LICENSE")
DEST = Path(__file__).resolve().parent.parent / "jev_mcp" / "vendor" / "typesafe-ai"


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        with urllib.request.urlopen(BASE + name, timeout=30) as response:  # noqa: S310 - fixed https URL
            body = response.read()
        if not body.strip():
            print(f"upstream {name} was empty; aborting", file=sys.stderr)
            return 1
        (DEST / name).write_bytes(body)
        print(f"wrote {DEST / name} ({len(body)} bytes)")
    (DEST / "SOURCE.txt").write_text(
        f"Source: {BASE}\nFetched: {datetime.now(UTC).replace(microsecond=0).isoformat()}\n"
        "Refresh with: uv run python scripts/update_skill.py\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
