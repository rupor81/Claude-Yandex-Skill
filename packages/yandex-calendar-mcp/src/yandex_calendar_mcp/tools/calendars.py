"""The ``calendar_list`` tool contract.

This module imports no protocol library.  It owns argument validation, slicing,
and the completeness flag; the client below it owns CalDAV, and the server above
it owns transport and annotations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from pydantic import BaseModel, Field
from yandex_core.errors import ProtocolError
from yandex_core.paging import checked_limit, decode_cursor, encode_cursor
from yandex_core.results import Page

from ..client.caldav_client import CalDAVCalendarClient

__all__ = [
    "CalendarSummary",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "TOOL_NAME",
    "build_calendar_list",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MIN_LIMIT = 1

TOOL_NAME = "calendar_list"

ClientProvider = Callable[[], Awaitable[CalDAVCalendarClient]]


class CalendarSummary(BaseModel):
    """One calendar on the configured account."""

    name: str = Field(description="Display name of the calendar.")
    url: str = Field(description="CalDAV collection URL identifying the calendar.")


def build_calendar_list(client_provider: ClientProvider) -> Callable[..., Awaitable[Page]]:
    """Bind ``calendar_list`` to a source of clients.

    The provider is injected rather than imported so that tests, and later
    stories with a different credential path, do not have to reach into a global.
    """

    async def calendar_list(
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_LIMIT,
                ge=MIN_LIMIT,
                le=MAX_LIMIT,
                description=(
                    f"Maximum calendars to return (default {DEFAULT_LIMIT}, "
                    f"maximum {MAX_LIMIT})."
                ),
            ),
        ] = DEFAULT_LIMIT,
        cursor: Annotated[
            str | None,
            Field(
                default=None,
                description="Opaque cursor from a previous truncated call.",
            ),
        ] = None,
    ) -> Page[CalendarSummary]:
        """List the calendars on the configured Yandex account.

        Returns at most `limit` calendars. If more exist, `complete` is false and
        `next_cursor` carries the remainder.
        """
        limit_used = checked_limit(limit, minimum=MIN_LIMIT, maximum=MAX_LIMIT)
        offset = _offset_from(cursor)
        client = await client_provider()
        calendars = await client.list_calendars()

        if offset and offset >= len(calendars):
            # The account shrank, or the cursor is older than the collection.
            # An empty `complete: true` page here would read as "no calendars".
            raise ProtocolError(
                f"Cursor points past the end of the calendar list "
                f"({offset} of {len(calendars)}); the list changed since it was "
                "issued. Start again without a cursor."
            )

        window = calendars[offset : offset + limit_used]
        remaining = len(calendars) - (offset + len(window))
        complete = remaining <= 0

        return Page[CalendarSummary](
            items=[CalendarSummary(name=ref.name, url=ref.url) for ref in window],
            complete=complete,
            next_cursor=(
                None
                if complete
                else encode_cursor({"offset": offset + len(window)}, tool=TOOL_NAME)
            ),
        )

    calendar_list.__name__ = TOOL_NAME
    return calendar_list


def _offset_from(cursor: str | None) -> int:
    if cursor is None:
        return 0
    payload = decode_cursor(cursor, tool=TOOL_NAME)
    offset = payload.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ProtocolError("Cursor is not a cursor this server issued.")
    return offset
