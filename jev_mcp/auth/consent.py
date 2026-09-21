"""Owner-password consent page that completes the OAuth authorization step."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from html import escape
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from jev_mcp.auth.provider import MAX_ATTEMPTS, PendingAuth, SqliteOAuthProvider
from jev_mcp.config import Settings

_STYLE = (
    "body{font-family:system-ui,sans-serif;background:#f6f7f9;color:#1c1e21;display:flex;justify-content:center;"
    "padding:4rem 1rem}main{background:#fff;border:1px solid #d9dce1;border-radius:12px;padding:2rem;"
    "max-width:26rem;width:100%}h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:.5rem 0;line-height:1.4}"
    "code{background:#eef0f3;padding:.1rem .3rem;border-radius:4px}label{display:block;margin-top:1rem;"
    "font-weight:600}input[type=password]{width:100%;padding:.6rem;margin-top:.4rem;border:1px solid #c5c9d0;"
    "border-radius:8px;font-size:1rem}button{margin-top:1rem;width:100%;padding:.7rem;border:0;border-radius:8px;"
    "background:#1f6feb;color:#fff;font-size:1rem;cursor:pointer}.error{color:#b42318;font-weight:600}"
)


def _html(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body><main>{body}</main></body></html>"
    )
    return HTMLResponse(page, status_code=status)


def _expired() -> HTMLResponse:
    return _html(
        "Link expired",
        "<h1>This sign-in link has expired</h1><p>Start the connection again from your MCP client.</p>",
        status=400,
    )


def _form(pending: PendingAuth, error: str | None, status: int = 200) -> HTMLResponse:
    name = escape(pending.client.client_name or pending.client.client_id)
    host = escape(urlparse(str(pending.params.redirect_uri)).netloc)
    error_html = f"<p class='error'>{escape(error)}</p>" if error else ""
    password_input = (
        '<input id="password" type="password" name="password" '
        'autocomplete="current-password" autofocus required>'
    )
    body = (
        "<h1>Authorize an agent to use jev-mcp</h1>"
        f"<p><strong>{name}</strong> wants access. After approval it will be sent back to "
        f"<code>{host}</code>.</p>"
        f"{error_html}"
        "<form method='post' action='/consent'>"
        f'<input type="hidden" name="request" value="{escape(pending.id)}">'
        "<label for='password'>Owner password</label>"
        f"{password_input}"
        "<button type='submit'>Approve</button></form>"
    )
    return _html("Authorize jev-mcp", body, status=status)


def consent_handler(
    provider: SqliteOAuthProvider, settings: Settings
) -> Callable[[Request], Awaitable[Response]]:
    async def handle(request: Request) -> Response:
        if request.method == "GET":
            pending = provider.get_pending(request.query_params.get("request", ""))
            return _expired() if pending is None else _form(pending, error=None)

        form = await request.form()
        request_id = str(form.get("request", ""))
        password = str(form.get("password", ""))
        pending = provider.get_pending(request_id)
        if pending is None:
            return _expired()
        pwd_bytes = password.encode("utf-8")
        pwd_setting_bytes = settings.owner_password.encode("utf-8")
        if not hmac.compare_digest(pwd_bytes, pwd_setting_bytes):
            attempts = provider.record_failed_attempt(request_id)
            if attempts >= MAX_ATTEMPTS:
                locked_msg = (
                    "<h1>Too many failed attempts</h1>"
                    "<p>Start the connection again from your MCP client.</p>"
                )
                return _html("Locked", locked_msg, status=403)
            remaining = MAX_ATTEMPTS - attempts
            plural = "s" if remaining != 1 else ""
            error_msg = f"Wrong password ({remaining} attempt{plural} left)"
            return _form(pending, error=error_msg, status=401)
        return RedirectResponse(provider.approve(request_id), status_code=302)

    return handle
