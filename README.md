# jev-mcp

jev-mcp is a self-hostable MCP server that lets AI agents save, document, and re-run [TypeSafe](https://typesafe.ai)
Jev queries. Instead of an agent re-deriving the same "is this urgent?" or "which category?" judgment call
from scratch in every session, it saves the judgment once as a named tool — with the questions, the
supporting context, and docs explaining when to use it — and any agent with access can look it up and run
it again with new inputs.

To run it you need a TypeSafe API key, somewhere to run one container with a small persistent disk (a
Fly.io app, a VPS, or any Docker host), and an MCP client that supports remote servers with OAuth, such as
Claude Desktop, claude.ai, or Claude Code.

## How it works

- Agents authenticate with OAuth 2.1; you approve each agent once by typing an owner password on a consent
  page, and it gets its own client id and tokens from then on.
- A saved tool is `inputs + context + questions + docs`: inputs are what callers supply at run time,
  context is constant background (policies, definitions, examples), questions are what to ask TypeSafe,
  and docs explain the tool for the next agent that finds it.
- Running a tool merges its context and the caller's inputs into a single TypeSafe `state` object and
  returns typed answers (`noul`, `choice`, or `score`).
- Every call — saved-tool runs and one-off `ask_jev` calls alike — is logged with its inputs, answers,
  token usage, latency, and the agent that made it.

## Deploy on Fly.io

```bash
git clone <repo> && cd jev-mcp
fly launch --no-deploy --copy-config --name <your-app>
fly volumes create jev_data --size 1 --region <region>
fly secrets set TYPESAFE_API_KEY=... JEV_OWNER_PASSWORD=... JEV_PUBLIC_URL=https://<your-app>.fly.dev
fly deploy
curl https://<your-app>.fly.dev/healthz
```

This is a single-instance design: the database is one SQLite file on one volume, so scaling past one
Fly machine gives each machine its own volume and forks the database. Keep the count at one.

## Deploy with Docker

```bash
cp .env.example .env
# edit .env: set TYPESAFE_API_KEY, JEV_OWNER_PASSWORD, JEV_PUBLIC_URL
docker compose up -d
```

`JEV_PUBLIC_URL` must be the HTTPS URL your MCP clients will actually use — OAuth issuer and redirect
checks depend on it matching exactly. `docker compose` does not terminate TLS for you, so put a
TLS-terminating proxy (Caddy, Traefik, or Cloudflare Tunnel) in front of the container and point it at
`localhost:8080`.

## Connect an agent

**Claude Desktop / claude.ai**: Settings → Connectors → Add custom connector → URL `https://<host>/mcp`.
A browser tab opens the consent page; enter the owner password to approve the agent.

**Claude Code**:

```bash
claude mcp add --transport http jev https://<host>/mcp
```

Then run `/mcp` inside Claude Code to complete authentication.

Every agent that connects gets its own client id and its own access/refresh tokens. To revoke one agent,
delete its rows from the `oauth_tokens` table in the SQLite database; to revoke every agent at once,
rotate `JEV_OWNER_PASSWORD` and restart (new consent approvals will require the new password, but existing
tokens keep working until they expire or are deleted — deleting rows is the immediate option).

## How agents use it

The server's `instructions` field tells connecting agents the workflow:

1. Call `list_tools` first. If a saved tool fits, call `get_tool` to read its docs, then `run_tool` with
   the declared inputs.
2. To design a new judgment, iterate with `ask_jev` (state + questions, nothing is saved) until the
   answers look right.
3. Persist it with `create_tool`: declare inputs (what callers pass at run time), optional context
   (policies, definitions, examples that never change), the questions, and docs that explain purpose,
   when to use it, and how to read the answers.

### Worked example: Gmail triage

An agent that has been iterating with `ask_jev` settles on a tool and saves it:

```json
{
  "name": "create_tool",
  "arguments": {
    "name": "gmail_triage",
    "title": "Gmail triage",
    "docs": "Triages one inbound Gmail message. Use this before deciding whether to reply, file, or escalate an email. `needs_reply` above 0.5 means draft a reply. `category` sorts the email into one of four buckets. `urgency` above 1.5 (top of a 0-2 scale) means handle it today; below 0.5 means it can wait.",
    "inputs": {
      "email": {
        "type": "object",
        "description": "Object with `subject`, `from`, and `body`.",
        "required": true
      }
    },
    "context": {
      "policy": "Reply within 24 hours to anything from a paying customer. Newsletters and automated receipts never need a reply."
    },
    "questions": {
      "needs_reply": {
        "type": "noul",
        "instructions": "Does `email` require a personal reply, per `policy`?"
      },
      "category": {
        "type": "choice",
        "instructions": "Which category best fits `email`?",
        "criteria": {
          "customer_support": "A question or complaint from someone outside the company",
          "internal": "From a colleague or an internal system",
          "newsletter": "A subscribed newsletter or marketing email",
          "receipt": "An automated receipt, invoice, or shipping notice"
        }
      },
      "urgency": {
        "type": "score",
        "instructions": "How urgent is `email`, given `policy`?",
        "criteria": ["low", "medium", "high"]
      }
    }
  }
}
```

Later — this session or a future one — any connected agent can run it against a real message:

```json
{
  "name": "run_tool",
  "arguments": {
    "name": "gmail_triage",
    "inputs": {
      "email": {
        "subject": "Payment failed again",
        "from": "customer@example.com",
        "body": "This is the third time my card has been declined even though it's valid. I need this fixed today."
      }
    }
  }
}
```

Which returns:

```json
{
  "tool": "gmail_triage",
  "version": 1,
  "model": "jev-latest",
  "answers": {
    "needs_reply": { "type": "noul", "noul": 0.97 },
    "category": {
      "type": "choice",
      "choice": "customer_support",
      "probabilities": { "customer_support": 0.91, "internal": 0.02, "newsletter": 0.01, "receipt": 0.06 },
      "confidence": 0.9
    },
    "urgency": {
      "type": "score",
      "score": 1.8,
      "legend": { "0": "low", "1": "medium", "2": "high" },
      "probabilities": { "0": 0.02, "1": 0.16, "2": 0.82 },
      "confidence": 0.85
    }
  },
  "usage": { "input_tokens": 210, "output_tokens": 34 },
  "run_id": "r_8f2a1c9b"
}
```

`needs_reply.noul` is 0.97, well above the 0.5 threshold, and `urgency.score` is 1.8, above the "handle it
today" threshold — the agent drafts a reply now.

## Tool reference

All eight tools require a valid bearer token.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `ask_jev` | `state` (string \| object \| array), `questions`, `model?` | `{model, answers, usage, run_id}` straight from TypeSafe. Nothing is saved except a run row with `tool_name = null`. Question shapes: noul `criteria: {"true": …, "false": …}` (optional); choice `criteria: {option: description \| null}` (2–255 options); score `criteria: [ordered levels]` (2–10). |
| `create_tool` | `name, title, docs, inputs, questions, context?, model?` | The saved tool. Error if name exists or validation fails. |
| `update_tool` | `name` plus any subset of `title, docs, inputs, questions, context, model` | The saved tool with bumped version. Full validation runs on the merged result. |
| `get_tool` | `name` | Full definition including docs. |
| `list_tools` | `query?` | `[{name, title, version, updated_at, summary}]` where summary is the first line of docs. `query` is a case-insensitive substring match over name, title, and docs. |
| `run_tool` | `name, inputs, model?` | `{tool: name, version, model, answers, usage, run_id}`. |
| `delete_tool` | `name` | `{deleted: true, name}`. Runs are retained with the name for history. |
| `tool_runs` | `name?, limit?` (default 20, max 200) | Recent TypeSafe calls newest first: `{run_id, tool_name, version, client_id, client_name, inputs, answers, model, usage, latency_ms, error, created_at}`. Omit `name` to include ad-hoc `ask_jev` calls (`tool_name = null`). Calls that failed at TypeSafe are recorded with `error`; calls rejected by validation before reaching TypeSafe are not recorded. |

Validation failures and not-found conditions come back as MCP tool errors with a single plain-English
message naming the offending field. TypeSafe HTTP errors are surfaced with the status code, the API's
message, and `retry_after_seconds` when the response carried a `retry-after` header.

## Configuration

All configuration is by environment variable; `.env.example` documents every key.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `TYPESAFE_API_KEY` | yes | | Server-held key used for every TypeSafe call. |
| `JEV_OWNER_PASSWORD` | yes | | Password entered on the consent page. At least 12 characters; generate one with `openssl rand -base64 24`. |
| `JEV_PUBLIC_URL` | yes | | Externally reachable HTTPS base URL, e.g. `https://jev.example.com`. Used as OAuth issuer and resource. |
| `JEV_DB_PATH` | no | `/data/jev.db` | SQLite file path. |
| `JEV_HOST` | no | `0.0.0.0` | Bind address. |
| `JEV_PORT` | no | `8080` | Bind port. |
| `JEV_DEFAULT_MODEL` | no | `jev-latest` | Default model for new tools and `ask_jev`. |
| `JEV_ACCESS_TOKEN_TTL` | no | `3600` | Seconds. |
| `JEV_REFRESH_TOKEN_TTL` | no | `2592000` | Seconds. |
| `JEV_LOG_LEVEL` | no | `info` | One of `critical`, `error`, `warning`, `info`, `debug`, `trace`. |
| `JEV_RUN_RETENTION_DAYS` | no | `90` | Run history older than this is deleted as new runs are recorded. `0` disables the sweep. |

Startup fails fast with a message naming the variable at fault: a required variable missing, an owner
password under 12 characters, a `JEV_PUBLIC_URL` that is not HTTPS (set `JEV_ALLOW_INSECURE_URL=1` to
allow `http://` for local development only) or that carries a path (the OAuth endpoints live at the
root), an unknown log level, a non-positive port or token TTL, or a negative retention.

## Security notes

- Self-contained OAuth 2.1 authorization server with PKCE and dynamic client registration; no external
  identity provider is required.
- Access and refresh tokens are hashed at rest in SQLite — the raw token is never stored.
- One owner password gates every agent's consent; anyone who knows it can authorize a new agent, so treat
  it like any other server credential. It must be at least 12 characters, and guessing is bounded twice
  over: 5 attempts per consent request, and 10 failures server-wide lock the consent page for everyone
  (right password included) with an exponential backoff from 1 minute to 1 hour. A successful consent
  clears the lock.
- Wrong passwords, rejected registrations, approvals, token issuance, and token revocations are logged
  with client and family identifiers. Token values, authorization codes, and the password never are.
- The TypeSafe API key never leaves the server — agents send state and questions, and the server makes the
  TypeSafe call on their behalf.
- Run only behind HTTPS in production; `JEV_PUBLIC_URL` doubles as the OAuth issuer, so it must be the
  exact externally reachable origin.
- `jev_mcp/server.py` carries a small guarded patch for a bug in the mcp SDK version this project was
  built against (2.2.0, locked in `uv.lock`), where a missing default on `RevocationRequest.client_secret`
  makes `/revoke` reject the public clients dynamic registration creates. It checks the field at import
  time and only applies the fix if the bug is still present, so it self-disables once upstream ships a
  corrected release. The patch rebinds an attribute on an SDK module, which is process-wide, and any
  failure to find what it patches is swallowed so a moved module degrades to upstream behaviour.

- The container runs as root. Fly.io and Docker mount volumes root-owned and the slim base image has no
  `gosu` or `su-exec` to drop privileges after fixing ownership, so a non-root default would fail to
  start on a freshly attached volume. Nothing but the app runs in the image and it listens on one port,
  so the exposure is small — but if your host lets you prepare the volume, run non-root by adding
  `user: "1000:1000"` to the `jev-mcp` service in `docker-compose.yml` and `chown -R 1000:1000` the
  volume's `/data` once before starting.

## Backups

The whole application state is one SQLite file. Copying `jev.db` on its own gives you a torn or stale
snapshot, because the database runs in WAL mode and recent commits live in the `-wal` sidecar. Let SQLite
write a consistent copy with `VACUUM INTO`, then fetch that:

```bash
# Fly.io
fly ssh console -C "python -c \"import sqlite3; sqlite3.connect('/data/jev.db').execute('VACUUM INTO \\\"/data/backup.db\\\"')\""
fly ssh sftp get /data/backup.db ./jev-backup.db

# Docker
docker compose exec jev-mcp python -c "import sqlite3; sqlite3.connect('/data/jev.db').execute('VACUUM INTO \"/data/backup.db\"')"
docker cp jev-mcp:/data/backup.db ./jev-backup.db
```

`VACUUM INTO` fails if the destination already exists, so delete the previous `/data/backup.db` between
runs. To restore, stop the server, copy the backup to `/data/jev.db` (the configured `JEV_DB_PATH`),
delete any leftover `jev.db-wal` and `jev.db-shm` files next to it, and start the server again.

Run history is self-limiting: each new run deletes rows older than `JEV_RUN_RETENTION_DAYS` (90 by
default), so back up before lowering it if you want to keep the older history.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

To run the server locally over plain HTTP:

```bash
TYPESAFE_API_KEY=... JEV_OWNER_PASSWORD=... JEV_PUBLIC_URL=http://localhost:8080 \
  JEV_ALLOW_INSECURE_URL=1 uv run python -m jev_mcp
```

`scripts/smoke_live.py` makes one real call against the TypeSafe API to catch integration drift that
mocked tests can't. It is opt-in and skipped in CI:

```bash
TYPESAFE_API_KEY=... uv run python scripts/smoke_live.py
```

Without a key it prints a skip message and exits 0.

## License

MIT — see [LICENSE](LICENSE).
