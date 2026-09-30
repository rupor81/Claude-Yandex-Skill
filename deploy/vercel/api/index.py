"""Vercel entry point: the remote connector as an ASGI app.

Configuration comes from the project's environment variables -- see README,
"Remote connector". Until they are set, every request gets a 503 naming what is
missing, rather than a crash with nothing to read.
"""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route


def _make_app() -> Starlette:
    try:
        from yandex_remote_mcp.app import build_remote_app

        return build_remote_app()
    except Exception as exc:  # noqa: BLE001 -- shown to the operator, not swallowed
        reason = str(exc)

        async def unconfigured(request: Request) -> PlainTextResponse:
            return PlainTextResponse(
                f"Yandex MCP is not configured yet: {reason}", status_code=503
            )

        return Starlette(routes=[Route("/{path:path}", unconfigured)])


# Assigned at top level: Vercel finds the ASGI app by reading this file, not running it.
app = _make_app()
