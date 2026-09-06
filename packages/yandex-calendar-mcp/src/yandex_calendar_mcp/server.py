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
from .tools.events import (
    build_calendar_event_create,
    build_calendar_event_get,
    build_calendar_events_list,
)
from .tools.freebusy import build_calendar_freebusy_query

__all__ = ["SERVICE", "build_calendar_server", "main"]

SERVICE = "calendar"

INSTRUCTIONS = (
    "Read a Yandex calendar over CalDAV, and create one-off events in it. Four "
    "of its five tools are read-only: they list the calendars on the configured "
    "account, the event occurrences in a date range, one event in full by its "
    "UID, and the merged intervals of time the account is busy. The fifth, "
    "`calendar_event_create`, writes: it adds one non-recurring event to a "
    "calendar you name -- it invites nobody, and this server still cannot "
    "change or delete anything that exists. Creating requires `calendar_url`: "
    "no calendar is chosen for you, because the account has several and the "
    "server marks none of them as the default. What a create returns is what "
    "the server stored, read back afterwards, not what was asked for. Busy "
    "time is answered by `calendar_freebusy_query`, which "
    "returns time only and no meeting titles, and which reports a tentative or "
    "unanswered invitation as its own kind of busy rather than deciding for "
    "you whether it counts. Recurring series are returned already "
    "expanded into concrete occurrences, never as recurrence rules. An event "
    "is read by addressing its UID, so a UID that is not on the account is an "
    "error naming it rather than an empty result. Listing tools are bounded: a "
    "result with `complete: false` was cut short and its `next_cursor` must be "
    "passed back to see the rest. A page can also be irrecoverably short: when "
    "events or whole calendars could not be read, they are counted rather than "
    "silently dropped and no cursor can retrieve them, so the answer stays "
    "incomplete however many times it is asked for again."
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
    register_tool(server, build_calendar_events_list(client_provider))
    register_tool(server, build_calendar_event_get(client_provider))
    register_tool(server, build_calendar_freebusy_query(client_provider))
    register_tool(server, build_calendar_event_create(client_provider))
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
