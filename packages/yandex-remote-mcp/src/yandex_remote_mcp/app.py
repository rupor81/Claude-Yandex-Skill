"""The remote connector: Calendar and Mail over streamable HTTP, behind Yandex ID.

One server rather than two, because a remote connector is added to Claude once:
one address, one sign-in. Stateless HTTP, because a serverless host starts a
fresh process whenever it likes.
"""

from __future__ import annotations

import urllib.parse

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from yandex_calendar_mcp.server import INSTRUCTIONS as CALENDAR
from yandex_calendar_mcp.server import register_calendar_tools
from yandex_core.app import build_server
from yandex_core.config import Profile, load_profile
from yandex_mail_mcp.server import INSTRUCTIONS as MAIL
from yandex_mail_mcp.server import register_mail_tools

from .auth import RemoteSettings, YandexIdProvider

__all__ = ["build_remote_app", "build_remote_server"]

INSTRUCTIONS = (
    "Yandex Calendar and Yandex Mail for one signed-in account.\n\n"
    + CALENDAR
    + "\n\n"
    + MAIL
    + " On this remote connector `mail_attachment_download` is not offered: a file "
    "saved here would land on the server, not on your machine."
)


def build_remote_server(
    settings: RemoteSettings,
    profile: Profile | None = None,
    provider: YandexIdProvider | None = None,
) -> MCPServer:
    provider = provider or YandexIdProvider(settings)
    server = build_server(
        name="yandex",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.base_url,
            resource_server_url=f"{settings.base_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=False),
        ),
    )
    resolved = profile or load_profile()
    register_calendar_tools(server, resolved)
    register_mail_tools(server, resolved, local_files=False)

    @server.custom_route("/yandex/callback", methods=["GET"])
    async def yandex_callback(request: Request) -> Response:
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code or not state:
            return _page(
                "Вход не завершён: Яндекс не вернул код. Начните заново из Claude.", 400
            )
        try:
            target = await provider.finish_yandex_login(code, state)
        except PermissionError as exc:
            return _page(str(exc), 403)
        return RedirectResponse(target, status_code=302)

    return server


def build_remote_app(
    settings: RemoteSettings | None = None, **kwargs: object
) -> Starlette:
    settings = settings or RemoteSettings.from_env()
    host = urllib.parse.urlsplit(settings.base_url).netloc
    server = build_remote_server(settings, **kwargs)
    return server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host],
            allowed_origins=[
                settings.base_url,
                "https://claude.ai",
                "https://claude.com",
            ],
        ),
    )


def _page(message: str, status: int) -> HTMLResponse:
    import html

    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>Yandex MCP</title>"
        f"<p style='font:16px system-ui;margin:15vh auto;max-width:32em'>{html.escape(message)}</p>",
        status_code=status,
    )
