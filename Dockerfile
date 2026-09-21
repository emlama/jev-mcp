FROM python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    JEV_DB_PATH=/data/jev.db \
    JEV_HOST=0.0.0.0 \
    JEV_PORT=8080

COPY pyproject.toml uv.lock README.md ./
COPY jev_mcp ./jev_mcp
RUN uv sync --frozen --no-dev

# The process runs as root so it can write to a freshly attached volume at /data
# (Fly.io and Docker mount volumes root-owned). Nothing else in the image needs it.
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8080

# Shell form so ${JEV_PORT} is expanded at run time: the app binds the configured
# port, and a hard-coded 8080 here would report a healthy container that nobody can reach.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${JEV_PORT:-8080}/healthz').status == 200 else 1)"

CMD ["python", "-m", "jev_mcp"]
