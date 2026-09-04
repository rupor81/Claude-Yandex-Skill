"""The Yandex calendar MCP server: transport and wiring, nothing else.

Every decision of substance lives a layer down. This module builds the
application, resolves the profile and credential once at start-up, registers the
tools through the risk registry, and hands the process to stdio.
"""

from __future__ import annotations

import logging
import sys

from yandex_core.app import build_server, configure_logging, register_tool
from yandex_core.config import Profile, load_profile
from yandex_core.credentials import get_secret
from yandex_core.errors import YandexError

from .client.caldav_client import CalDAVCalendarClient
from .tools.calendars import build_calendar_list

__all__ = ["SERVICE", "build_calendar_server", "main"]

SERVICE = "calendar"

INSTRUCTIONS = (
    "Read a Yandex calendar over CalDAV. This server is read-only: it can list "
    "the calendars on the configured account and nothing else -- it cannot "
    "create, change, or delete anything. Listing tools are bounded: a result "
    "with `complete: false` was cut short and its `next_cursor` must be passed "
    "back to see the rest."
)

logger = logging.getLogger(__name__)


def build_calendar_server(profile: Profile | None = None):
    """Build the calendar application with no transport chosen yet.

    Raises:
        ProtocolError: if a tool is missing from the risk registry, naming it.
    """
    resolved = profile or load_profile()

    async def client_provider() -> CalDAVCalendarClient:
        # Read lazily so a missing password surfaces as an actionable tool error
        # rather than a start-up crash with no context.
        password = get_secret(SERVICE, resolved.name)
        return CalDAVCalendarClient(
            url=resolved.caldav_url,
            username=resolved.login,
            password=password,
        )

    server = build_server(name="yandex-calendar-mcp", instructions=INSTRUCTIONS)
    register_tool(server, build_calendar_list(client_provider))
    return server


def main() -> int:
    """Entry point: stdio transport, logs on stderr only.

    A missing or malformed configuration is an operator problem with a fix, so it
    is reported as one line on stderr rather than as a traceback.
    """
    configure_logging()
    try:
        server = build_calendar_server()
        logger.info("yandex-calendar-mcp starting on stdio")
        server.run(transport="stdio")
    except YandexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.info("yandex-calendar-mcp stopped")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
