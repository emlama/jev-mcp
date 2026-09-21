"""Application factory: MCP tools, OAuth wiring, consent page, health check."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from jev_mcp import __version__
from jev_mcp.auth.consent import consent_handler
from jev_mcp.auth.provider import SCOPE, SqliteOAuthProvider
from jev_mcp.config import Settings
from jev_mcp.db import Database
from jev_mcp.models import ToolRecord
from jev_mcp.repo import RunRepo, ToolRepo
from jev_mcp.service import ServiceError, ToolService
from jev_mcp.typesafe_client import JevClient, TypeSafeJevClient

INSTRUCTIONS = """jev-mcp stores reusable TypeSafe Jev queries ("jev tools") so agents can rerun them
across sessions.

Workflow:
1. Call list_tools first. If a saved tool fits, call get_tool to read its docs, then run_tool with the
   declared inputs.
2. To design a new judgment, iterate with ask_jev (state + questions, nothing is saved) until the
   answers look right.
3. Persist it with create_tool: declare inputs (what callers pass at run time), optional context
   (policies, definitions, examples that never change), the questions, and docs that explain purpose,
   when to use it, and how to read the answers.

Question design: ask narrow, atomic questions; put reference material in context rather than in the
question; refer to state fields with backticked paths such as `email.subject`. Answer types: noul
returns P(yes) in 0..1; choice returns the top option, a probability per option, and confidence; score
returns a probability-weighted position across your ordered levels plus a legend. Thresholds are your
decision; record the ones you settle on in the tool's docs.
"""

_TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _patch_mcp_revocation_client_secret_bug() -> None:
    """Work around a bug in mcp==2.2.0's /revoke handler.

    ``RevocationRequest.client_secret`` (mcp/server/auth/handlers/revoke.py) is typed
    ``str | None`` but, unlike every equivalent field in handlers/token.py, has no
    ``= None`` default. Pydantic therefore treats it as a *required* key, so any POST
    to /revoke that omits client_secret - which is exactly what a public client
    (token_endpoint_auth_method="none", the kind dynamic registration mints for us)
    correctly does per RFC 7009 section 2.1 - fails with 400 "client_secret: Field
    required" before the provider is ever consulted. Give the field the same default
    used elsewhere in the SDK by swapping in a subclass; remove this once upstream
    ships a fixed release.

    This rebinds an attribute on an SDK module, so it takes effect process-wide for
    every MCP server in this interpreter, not just the app built here. If the SDK
    moves or renames what it reaches for, the patch quietly does nothing and upstream
    behaviour stands - a 400 on /revoke is a far better failure than a crash at import.
    """
    try:
        import mcp.server.auth.handlers.revoke as revoke_module

        if revoke_module.RevocationRequest.model_fields["client_secret"].is_required():

            class _FixedRevocationRequest(revoke_module.RevocationRequest):
                client_secret: str | None = None

            revoke_module.RevocationRequest = _FixedRevocationRequest
    except (ImportError, AttributeError, KeyError):
        return


_patch_mcp_revocation_client_secret_bug()


def build_app(settings: Settings, *, jev: JevClient | None = None, db: Database | None = None) -> Starlette:
    database = db or Database(settings.db_path)
    database.migrate()
    jev_client = jev or TypeSafeJevClient(settings.typesafe_api_key)
    runs = RunRepo(database, retention_days=settings.run_retention_days)
    service = ToolService(ToolRepo(database), runs, jev_client, settings.default_model)
    provider = SqliteOAuthProvider(database, settings)

    srv = MCPServer(
        "jev-mcp",
        title="jev-mcp",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.public_url),
            resource_server_url=AnyHttpUrl(settings.mcp_url),
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        ),
    )
    _register_tools(srv, service, provider)
    srv.custom_route("/consent", methods=["GET", "POST"])(consent_handler(provider, settings))

    @srv.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        try:
            # A write, not a read: a full or read-only volume is the failure this
            # check exists to catch, and SELECT 1 would sail straight past it.
            with database.tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO healthcheck (id, touched_at) VALUES (1, ?)",
                    (int(time.time()),),
                )
        except Exception:  # noqa: BLE001 - health check must never raise
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True, "version": __version__})

    app = srv.streamable_http_app(
        json_response=True, stateless_http=True, transport_security=_TRANSPORT_SECURITY
    )
    app.state.db = database
    app.state.jev = jev_client
    return app


def _client_id() -> str:
    token = get_access_token()
    if token is None:
        raise ToolError("no authenticated agent on this request")
    return token.client_id


@contextmanager
def _agent_errors() -> Iterator[None]:
    try:
        yield
    except ServiceError as exc:
        raise ToolError(str(exc)) from exc


def _tool_view(record: ToolRecord, provider: SqliteOAuthProvider) -> dict[str, Any]:
    view = record.model_dump(mode="json")
    view["created_by_name"] = provider.client_name(record.created_by)
    view["updated_by_name"] = provider.client_name(record.updated_by)
    return view


def _register_tools(srv: MCPServer, service: ToolService, provider: SqliteOAuthProvider) -> None:
    @srv.tool(
        description=(
            "Ask TypeSafe Jev one-off questions about a state without saving anything. Use this to "
            "iterate on question wording before create_tool. `state` is a string, object, or array; "
            "`questions` maps ids to {type: noul|choice|score, instructions, criteria}. Returns answers "
            "keyed by question id."
        )
    )
    async def ask_jev(
        state: str | dict[str, Any] | list[Any], questions: dict[str, Any], model: str | None = None
    ) -> dict[str, Any]:
        with _agent_errors():
            result = await service.ask(state, questions, _client_id(), model)
        return result.model_dump(mode="json")

    @srv.tool(
        description=(
            "Save a reusable jev tool. `name` is a slug (^[a-z][a-z0-9_]{2,63}$). `inputs` maps input "
            "name to {type: string|object|array, description, required?}; callers supply these at run "
            "time and they become top-level state fields. `context` holds constant state (policies, "
            "definitions, examples). `questions` is the TypeSafe question map; refer to state with "
            "backticked paths like `email.subject`. `docs` must explain purpose, when to use it, and "
            "how to interpret answers (thresholds), for future agents."
        )
    )
    async def create_tool(
        name: str,
        title: str,
        docs: str,
        inputs: dict[str, Any],
        questions: dict[str, Any],
        context: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "name": name, "title": title, "docs": docs, "inputs": inputs, "questions": questions,
            "context": context, "model": model,
        }
        with _agent_errors():
            record = service.create_tool(payload, _client_id())
        return _tool_view(record, provider)

    @srv.tool(
        description=(
            "Change an existing jev tool. Pass only the fields to replace (title, docs, inputs, context, "
            "questions, model); omitted fields keep their current value. The whole merged definition is "
            "re-validated and the version number increases."
        )
    )
    async def update_tool(
        name: str,
        title: str | None = None,
        docs: str | None = None,
        inputs: dict[str, Any] | None = None,
        questions: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        changes = {
            "title": title,
            "docs": docs,
            "inputs": inputs,
            "questions": questions,
            "context": context,
            "model": model,
        }
        with _agent_errors():
            record = service.update_tool(name, changes, _client_id())
        return _tool_view(record, provider)

    @srv.tool(description="Fetch a saved jev tool's full definition and docs. Read this before run_tool.")
    async def get_tool(name: str) -> dict[str, Any]:
        with _agent_errors():
            record = service.get_tool(name)
        return _tool_view(record, provider)

    @srv.tool(
        description=(
            "List saved jev tools with a one-line summary each. Optional `query` filters by case-insensitive "
            "substring over name, title, and docs. Call this first."
        )
    )
    async def list_tools(query: str | None = None) -> dict[str, Any]:
        return {"tools": [summary.model_dump(mode="json") for summary in service.list_tools(query)]}

    @srv.tool(
        description=(
            "Run a saved jev tool. `inputs` maps each declared input name to its value; the server "
            "merges them with the tool's context and asks TypeSafe the saved questions. Returns "
            "answers, the model used, token usage, and a run_id. `model` optionally overrides the "
            "tool's model for this run."
        )
    )
    async def run_tool(name: str, inputs: dict[str, Any], model: str | None = None) -> dict[str, Any]:
        with _agent_errors():
            result = await service.run_tool(name, inputs, _client_id(), model)
        return result.model_dump(mode="json")

    @srv.tool(description="Delete a saved jev tool by name. Its run history is kept.")
    async def delete_tool(name: str) -> dict[str, Any]:
        with _agent_errors():
            service.delete_tool(name)
        return {"deleted": True, "name": name}

    @srv.tool(
        description=(
            "Recent runs, newest first, including ad-hoc ask_jev calls (tool_name null). Optional "
            "`name` filters to one tool; `limit` defaults to 20 (max 200). Shows inputs, answers, "
            "usage, latency, errors, and which agent made the call."
        )
    )
    async def tool_runs(name: str | None = None, limit: int = 20) -> dict[str, Any]:
        runs = []
        for run in service.recent_runs(name, limit):
            view = run.model_dump(mode="json")
            view["client_name"] = provider.client_name(run.client_id)
            runs.append(view)
        return {"runs": runs}
