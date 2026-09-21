"""`python -m jev_mcp` starts the HTTP server."""

from __future__ import annotations

import logging

import uvicorn

from jev_mcp.config import ConfigError, Settings
from jev_mcp.server import build_app


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"jev-mcp: {exc}") from exc
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=settings.stdlib_log_level, format=log_format)
    app = build_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level)


if __name__ == "__main__":
    main()
