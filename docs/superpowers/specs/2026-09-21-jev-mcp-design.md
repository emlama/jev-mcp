# jev-mcp design

Date: 2026-09-21
Status: approved in discussion, pending spec review

## Purpose

`jev-mcp` is a small, self-hostable MCP server that lets AI agents define,
persist, document, and re-run TypeSafe "Jev" queries. An agent works out a
good set of typed questions once (for example a Gmail triage classifier, a
LinkedIn profile assessor, a service-incident severity rater, a messy-market-
data sanity checker), saves it as a named **jev tool** with documentation, and
any agent in any later session can list, read, and run that tool without
re-deriving it.

The project is open source (MIT) and designed to be deployed by anyone on
their own infrastructure: one container, one SQLite file, one TypeSafe API
key, one owner password.

## Non-goals

- Not a general TypeSafe playground or UI. Agents are the users.
- Not multi-tenant. One deployment serves one owner; every authorized agent
  sees every saved tool.
- No per-saved-tool dynamic MCP tool registration in v1 (see "Future work").
- No third-party identity provider integration in v1.

## Key concepts

### Jev tool

A jev tool is a named, versioned record:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | slug, `^[a-z][a-z0-9_]{2,63}$` | Primary key. Immutable. |
| `title` | string | Short human label. |
| `docs` | markdown string | Agent-facing documentation: purpose, when to use, how to interpret answers, suggested thresholds. Required, non-empty. |
| `inputs` | map of input name to `InputSpec` | Fields the caller supplies at run time. Input names must match the slug pattern above and must not collide with `context` keys. |
| `context` | JSON object | Constant state merged into every run: policies, definitions, worked examples. Optional, defaults to `{}`. |
| `questions` | map of question id to `Question` | TypeSafe question map stored verbatim. At least one question. |
| `model` | string | Defaults to `jev-latest`. May be pinned to a versioned id such as `jev-1.13.0`. |
| `version` | integer | Starts at 1, bumps on every update. |
| `created_by`, `updated_by` | string | OAuth client id of the agent. |
| `created_at`, `updated_at` | ISO 8601 UTC | |

`InputSpec`:

| Field | Type | Notes |
| --- | --- | --- |
| `type` | `"string"` \| `"object"` \| `"array"` | The JSON shape the caller must supply. Mirrors what TypeSafe accepts in state. |
| `description` | string | Required. Tells the calling agent what to pass. |
| `required` | bool | Defaults to true. |

`Question` is exactly the TypeSafe request shape:

- `{"type": "noul", "instructions": ..., "criteria"?: {"true": ..., "false": ...}}`
- `{"type": "choice", "instructions": ..., "criteria": {option: description|null, ...}}` with 2 to 255 options
- `{"type": "score", "instructions": ..., "criteria": [level, ...]}` with 2 to 10 levels

`instructions`, criteria descriptions, and levels may be strings, objects, or
arrays, matching the TypeSafe API.

### Run

Running a tool builds `state = {**context, **inputs}` (inputs win on a key
collision, but collisions are rejected at save time so this never happens in
practice), sends one `POST /v1/systemone` request with the tool's questions
and model, and returns the answers. Every run is recorded.

### Validation performed at save time

1. Name and input names match the slug pattern.
2. `docs` is non-empty.
3. Every question parses as one of the three types with the criteria limits
   above.
4. Every backticked path in `instructions` or criteria (for example
   `` `email.subject` `` or `` `policy.rules[0]` ``) has a root segment that
   is a declared input or a top-level context key. Unresolvable roots are an
   error; this is the most common authoring mistake and cheap to catch.
5. Input names do not collide with context keys.
6. The serialized `context` plus questions must be under 200 KB. This is a
   sanity bound well below the model's 64k-token budget; the API remains the
   final authority.

### Validation performed at run time

1. Every required input is present; unknown input names are rejected.
2. Each supplied input matches its declared JSON type.
3. Model override, if given, is a non-empty string; it is passed through.

## MCP surface

Transport: streamable HTTP at `/mcp`, stateless mode with JSON responses. No
session state lives in the server process, so restarts and single-instance
scaling on a micro VM are safe.

The server sends an `instructions` string on initialize that tells agents the
workflow:

> Use `list_tools` first to see saved jev tools. Read `get_tool` docs before
> running one. To design a new tool, iterate with `ask_jev` until the answers
> are right, then `create_tool` with clear docs so future agents can reuse it.

Eight MCP tools, all requiring a valid bearer token:

| Tool | Arguments | Returns |
| --- | --- | --- |
| `ask_jev` | `state` (string \| object \| array), `questions`, `model?` | `{model, answers, usage}` straight from TypeSafe. Not persisted, except a run row with `tool_name = null` for accounting. |
| `create_tool` | `name, title, docs, inputs, questions, context?, model?` | The saved tool. Error if name exists or validation fails. |
| `update_tool` | `name` plus any subset of `title, docs, inputs, questions, context, model` | The saved tool with bumped version. Full validation runs on the merged result. |
| `get_tool` | `name` | Full definition including docs. |
| `list_tools` | `query?` | `[{name, title, version, updated_at, summary}]` where summary is the first line of docs. `query` is a case-insensitive substring match over name, title, and docs. |
| `run_tool` | `name, inputs, model?` | `{tool: name, version, model, answers, usage, run_id}`. |
| `delete_tool` | `name` | `{deleted: true}`. Runs are retained with the name for history. |
| `tool_runs` | `name?, limit?` (default 20, max 200) | Recent runs newest first: `{run_id, tool_name, version, client_id, inputs, answers, model, usage, latency_ms, error, created_at}`. |

Error contract: validation failures and not-found conditions are returned as
MCP tool errors (`isError: true`) with a single plain-English message that
names the offending field, so an agent can self-correct in one step. TypeSafe
HTTP errors are surfaced with the status code, the API's message, and
`retry_after_seconds` when the response carried a `retry-after` header. The
run row records the error.

## Authentication and authorization

The server is its own OAuth 2.1 authorization server and resource server,
implemented on the Python MCP SDK's `OAuthAuthorizationServerProvider`
protocol. The SDK provides the routes; this project provides SQLite-backed
storage and a consent page.

Endpoints (all served by the SDK unless noted):

- `/.well-known/oauth-authorization-server` and
  `/.well-known/oauth-protected-resource` metadata
- `POST /register` dynamic client registration (enabled; this is how Claude
  Desktop and claude.ai onboard themselves)
- `GET /authorize` validates the request, stores a pending authorization, and
  redirects to `/consent?request=<id>` (this project)
- `GET|POST /consent` (this project) shows the requesting client's name and
  redirect host, asks for the owner password, and on success mints an
  authorization code and redirects to the client's `redirect_uri` with
  `code` and `state`
- `POST /token` authorization-code and refresh-token grants with PKCE
- `POST /revoke`

Policies:

- Single scope `jev`. Every MCP tool requires it.
- Access tokens are opaque 256-bit random strings, TTL 1 hour.
- Refresh tokens are opaque, TTL 30 days, rotated on every use; the previous
  refresh token is revoked.
- Only SHA-256 hashes of tokens and codes are stored.
- Authorization codes expire after 10 minutes and are single use.
- Pending consents expire after 10 minutes.
- Resource indicator validation is on: tokens are bound to the configured
  public URL.
- The owner password must be at least 12 characters (`Settings.from_env`
  refuses to start otherwise) and is compared with a constant-time function.
- Two limits guard it. Per pending consent: after 5 failed attempts the
  pending row is deleted and the agent must restart the connection (pending
  consents already expire after 10 minutes). Globally: failures are also
  counted in a single-row `oauth_lockout` table, and after 10 failures the
  consent page refuses every submission — right password included — with
  HTTP 429 until a deadline that backs off exponentially (60 s doubling per
  further failure, capped at 1 hour). A successful consent resets both the
  counter and the deadline. The global counter is what makes the password
  un-brute-forceable: anyone can mint unlimited pending consents through
  `/register` and `/authorize`, so a per-request counter alone bounds
  nothing.
- The `client_id` on the access token is the agent identity recorded on tools
  and runs. The client's registered `client_name` is stored so `tool_runs`
  and `get_tool` can show a readable agent name.

Why one owner password rather than user accounts: the deployment serves one
person's fleet of agents. The password gate stops strangers from registering
an agent against a public URL; per-client tokens give each agent its own
revocable credential.

## Storage

SQLite via the standard library, WAL mode, `busy_timeout` 5 s, one connection
per request from a small pool. Schema is created on startup by an idempotent
migration function keyed by a `schema_version` table.

```sql
tools(name TEXT PK, title, docs, inputs_json, context_json, questions_json,
      model, version INT, created_by, updated_by, created_at, updated_at)
runs(id TEXT PK, tool_name TEXT NULL, tool_version INT NULL, client_id,
     inputs_json, answers_json NULL, model, input_tokens INT NULL,
     output_tokens INT NULL, latency_ms INT, error TEXT NULL, created_at)
oauth_clients(client_id TEXT PK, client_name, client_info_json, created_at)
oauth_pending(id TEXT PK, client_id, params_json, expires_at, attempts INT)
oauth_codes(code_hash TEXT PK, client_id, params_json, expires_at, used INT)
oauth_tokens(token_hash TEXT PK, kind TEXT, client_id, scopes_json,
             expires_at INT NULL, revoked INT, family_id TEXT, created_at)
```

Indexes on `runs(tool_name, created_at)`, `runs(created_at)`,
`oauth_tokens(family_id)`, `oauth_pending(expires_at)`.

Expired codes, pending consents, and tokens are purged opportunistically on
each token request.

## Configuration

All configuration is by environment variable. A `.env.example` documents every
key.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `TYPESAFE_API_KEY` | yes | | Server-held key used for every TypeSafe call. |
| `JEV_OWNER_PASSWORD` | yes | | Password entered on the consent page. |
| `JEV_PUBLIC_URL` | yes | | Externally reachable HTTPS base URL, e.g. `https://jev.example.com`. Used as OAuth issuer and resource. |
| `JEV_DB_PATH` | no | `/data/jev.db` | SQLite file path. |
| `JEV_HOST` | no | `0.0.0.0` | Bind address. |
| `JEV_PORT` | no | `8080` | Bind port. |
| `JEV_DEFAULT_MODEL` | no | `jev-latest` | Default model for new tools and `ask_jev`. |
| `JEV_ACCESS_TOKEN_TTL` | no | `3600` | Seconds. |
| `JEV_REFRESH_TOKEN_TTL` | no | `2592000` | Seconds. |
| `JEV_LOG_LEVEL` | no | `info` | |

Startup fails fast with a clear message if a required variable is missing or
`JEV_PUBLIC_URL` is not HTTPS (a `JEV_ALLOW_INSECURE_URL=1` escape hatch
exists for local development).

## Code layout

```
jev_mcp/
  __init__.py
  __main__.py        # `python -m jev_mcp` runs uvicorn
  config.py          # Settings from env, validation
  db.py              # connection pool, migrations
  models.py          # pydantic models: InputSpec, Question variants, ToolDefinition, validators
  paths.py           # backtick path extraction and root resolution
  typesafe_client.py # thin wrapper over typesafe-sdk: system_one(state, questions, model) -> dict, error mapping
  repo.py            # ToolRepo and RunRepo (SQLite CRUD)
  service.py         # ToolService: create/update/run orchestration, validation, run logging
  auth/
    __init__.py
    provider.py      # SqliteOAuthProvider implementing OAuthAuthorizationServerProvider
    consent.py       # Starlette routes and HTML for /consent
    tokens.py        # random token generation and hashing
  server.py          # build_app(settings) -> Starlette app; MCP tool registration
tests/
  conftest.py        # temp DB, settings, fake TypeSafe client, ASGI test client
  test_models.py
  test_paths.py
  test_repo.py
  test_service.py
  test_auth_flow.py  # register -> authorize -> consent -> token -> refresh -> revoke
  test_mcp_tools.py  # end to end over /mcp with a bearer token
scripts/
  smoke_live.py      # optional: hits real TypeSafe if TYPESAFE_API_KEY is set
Dockerfile
docker-compose.yml
fly.toml
pyproject.toml
.env.example
LICENSE (MIT)
README.md
```

Dependency boundaries:

- `models.py` and `paths.py` depend on nothing internal.
- `repo.py` depends on `db.py` and `models.py`.
- `service.py` depends on `repo.py`, `models.py`, `typesafe_client.py`.
- `auth/` depends on `db.py` and `config.py` only.
- `server.py` wires everything and is the only module that imports the MCP
  SDK's server classes.

`typesafe_client.py` exposes a small protocol so tests inject a fake without
network access.

## Deployment

- `Dockerfile`: `python:3.12-slim`, install with `uv`, non-root user, `/data`
  volume mount point, `CMD ["python", "-m", "jev_mcp"]`, `HEALTHCHECK` on
  `GET /healthz`.
- `docker-compose.yml`: one service, env from `.env`, named volume at
  `/data`, port 8080. Suitable for any VPS behind a TLS-terminating proxy.
- `fly.toml`: single `shared-cpu-1x` machine, `[mounts]` volume `jev_data` at
  `/data`, HTTP service on 8080 with forced HTTPS, health check on
  `/healthz`, `auto_stop_machines = "stop"` and `min_machines_running = 0`
  (stateless HTTP makes cold starts harmless).
- README covers: what it is, five-minute Fly deploy (`fly launch`, `fly
  volumes create`, `fly secrets set`, `fly deploy`), generic Docker deploy,
  adding the connector in Claude Desktop and claude.ai, the agent workflow,
  the tool reference, and how to back up the SQLite file.

`GET /healthz` returns `{"ok": true}` without auth and checks the database is
writable.

## Testing strategy

- Unit: model validation edge cases (bad slug, 1 score level, 256 choice
  options, unresolvable backtick path, input and context key collision), path
  extraction, repo round trips, version bumping, run logging on success and
  error.
- Integration (in-process ASGI, no network): full OAuth flow including PKCE
  verification failure, wrong owner password, expired code, refresh rotation,
  revocation, then a `run_tool` call over `/mcp` with the fake TypeSafe
  client asserting the exact state and questions sent.
- Live smoke (opt in): `ask_jev` against the real API with one noul.
- CI: GitHub Actions running `uv sync`, `ruff`, `pytest` on push.

## Future work (explicitly out of v1)

- Register each saved jev tool as its own MCP tool with a typed input schema.
- Optional GitHub or Google login on the consent page.
- Tool version history and rollback.
- Export and import of tool definitions as JSON files for sharing.
