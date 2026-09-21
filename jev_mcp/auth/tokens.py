"""Opaque token generation and hashing."""

from __future__ import annotations

import hashlib
import secrets
import time


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def now_s() -> int:
    return int(time.time())
