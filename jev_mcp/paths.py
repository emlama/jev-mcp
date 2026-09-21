"""Extraction of backticked state paths from question text."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

BACKTICK_RE = re.compile(r"`([^`\n]*)`")
ROOT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)")


def backtick_paths(value: Any) -> set[str]:
    """Every backticked span found in any string nested inside `value`."""
    found: set[str] = set()
    _walk(value, found)
    return found


def _walk(value: Any, found: set[str]) -> None:
    if isinstance(value, str):
        found.update(m.group(1) for m in BACKTICK_RE.finditer(value) if m.group(1))
    elif isinstance(value, Mapping):
        for item in value.values():
            _walk(item, found)
    elif isinstance(value, list | tuple):
        for item in value:
            _walk(item, found)


def path_root(path: str) -> str | None:
    """The leading identifier of a dotted/indexed path, or None if it is not path-like."""
    match = ROOT_RE.match(path.strip())
    return match.group(1) if match else None


def unresolved_roots(questions: Mapping[str, Any], known: set[str]) -> list[tuple[str, str]]:
    """(question_id, root) pairs for backticked paths whose root is not a known state key."""
    problems: set[tuple[str, str]] = set()
    for question_id, question in questions.items():
        for path in backtick_paths(question):
            root = path_root(path)
            if root is not None and root not in known:
                problems.add((question_id, root))
    return sorted(problems)
