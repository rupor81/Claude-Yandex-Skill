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
from yandex_core.paging import (
    checked_limit,
    decode_position_cursor,
    encode_position_cursor,
)
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
        after = _position_from(cursor)
        client = await client_provider()

        # Sorted so the cursor resumes against a stable order. The server's own
        # enumeration order is not promised to hold between two calls.
        calendars = sorted(await client.list_calendars(), key=_sort_key)
        if after is not None:
            calendars = [ref for ref in calendars if _sort_key(ref) > after]

        window = calendars[:limit_used]
        complete = len(window) == len(calendars)

        return Page[CalendarSummary](
            items=[CalendarSummary(name=ref.name, url=ref.url) for ref in window],
            complete=complete,
            next_cursor=(
                None
                if complete
                else encode_position_cursor(
                    {"name": window[-1].name, "url": window[-1].url}, tool=TOOL_NAME
                )
            ),
        )

    calendar_list.__name__ = TOOL_NAME
    return calendar_list


_CURSOR_FIELDS = ("name", "url")


def _sort_key(ref: object) -> tuple[str, str]:
    """Total order over calendars: name first, URL to break ties.

    The URL is unique, so no two calendars share a key. Without that tiebreak
    two calendars named alike would collide and the strict resume below would
    drop one of them.
    """
    return (getattr(ref, "name", "") or "", getattr(ref, "url", "") or "")


def _position_from(cursor: str | None) -> tuple[str, str] | None:
    """The calendar a previous page stopped at, or None to start at the top.

    Naming the last calendar rather than counting how many were skipped is what
    keeps a page correct when the account gains or loses one in between.
    """
    if cursor is None:
        return None
    after = decode_position_cursor(cursor, tool=TOOL_NAME, fields=_CURSOR_FIELDS)
    return (after["name"] or "", after["url"] or "")
