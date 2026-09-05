"""One real call against a real Yandex account.

Skipped unless `YANDEX_MCP_LIVE_TESTS=1`, because everything else in this suite
is expected to run with no network and no credentials.

    YANDEX_MCP_LIVE_TESTS=1 uv run pytest tests/live -q

What these assert is the *contract*, never a claim about the operator's data.
"The account holds no unreadable event" is a fact about somebody's calendar, and
tolerating one odd invite is exactly what this server exists to do -- so a real
account with one would fail a test of the code that has nothing wrong with it.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import anyio
import pytest
from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient
from yandex_calendar_mcp.tools.calendars import build_calendar_list
from yandex_calendar_mcp.tools.events import (
    MORE_PAGES,
    RANGE_TRUNCATED,
    UNREADABLE_DATA,
    build_calendar_events_list,
)
from yandex_core.config import load_profile
from yandex_core.credentials import get_secret

pytestmark = pytest.mark.skipif(
    os.environ.get("YANDEX_MCP_LIVE_TESTS") != "1",
    reason="live tests need YANDEX_MCP_LIVE_TESTS=1 and a configured account",
)


def _provider():
    profile = load_profile()

    async def provider() -> CalDAVCalendarClient:
        return CalDAVCalendarClient(
            url=profile.caldav_url,
            username=profile.login,
            password=get_secret("calendar", profile.name),
        )

    return provider


def test_calendar_list_returns_real_calendars():
    page = anyio.run(build_calendar_list(_provider()))

    assert page.items, "the account reported no calendars at all"
    assert page.complete is True
    assert page.next_cursor is None
    for calendar in page.items:
        assert calendar.name
        assert calendar.url.startswith("http")


def test_calendar_events_list_returns_real_occurrences():
    """One real range query: a week from today, against the configured account."""
    start = datetime.now(timezone.utc).replace(microsecond=0)
    end = start + timedelta(days=7)

    tool = build_calendar_events_list(_provider())
    page = anyio.run(lambda: tool(start=start, end=end, limit=10))

    assert isinstance(page.items, list)

    for occurrence in page.items:
        assert occurrence.uid
        assert occurrence.calendar_url.startswith("http")
        assert occurrence.calendar_name
        # Start-inclusive, end-exclusive, on the occurrence's own start.
        assert start <= _as_moment(occurrence.start) < end
        if occurrence.all_day:
            # An all-day event must never have been coerced to midnight.
            assert isinstance(occurrence.start, date)
            assert not isinstance(occurrence.start, datetime)
            assert not isinstance(occurrence.end, datetime)
        else:
            assert occurrence.start.utcoffset() is not None
            assert occurrence.end.utcoffset() is not None

    # The contract, not the state of the operator's calendar: an account holding
    # one unreadable invite is the case this feature exists to tolerate.
    assert page.unreadable >= 0
    assert page.unreadable_calendars >= 0
    if page.complete:
        assert page.next_cursor is None
        assert page.incomplete_reason is None
        assert page.unreadable == 0
        assert page.unreadable_calendars == 0
    else:
        assert page.incomplete_reason in {MORE_PAGES, RANGE_TRUNCATED, UNREADABLE_DATA}
        if page.incomplete_reason == UNREADABLE_DATA:
            assert page.unreadable or page.unreadable_calendars
        else:
            assert page.next_cursor, "a resumable shortfall must carry a cursor"


def test_paging_a_real_range_terminates_and_never_dead_ends():
    """Follow the cursor for real: every page must be resumable or complete."""
    start = datetime.now(timezone.utc).replace(microsecond=0)
    end = start + timedelta(days=30)

    tool = build_calendar_events_list(_provider())
    seen: list[tuple] = []
    cursor = None
    pages = 0

    while True:
        page = anyio.run(
            lambda c=cursor: tool(start=start, end=end, limit=5, cursor=c)
        )
        pages += 1
        assert pages < 200, "paging a month of a real calendar did not terminate"
        seen.extend(
            (item.calendar_url, item.uid, item.recurrence_id, str(item.start))
            for item in page.items
        )
        if page.complete:
            assert page.next_cursor is None
            break
        if page.incomplete_reason == UNREADABLE_DATA:
            # Nothing further to fetch; the loss is in the data itself.
            assert page.next_cursor is None
            break
        assert page.next_cursor, "a page was cut short with no way to continue"
        cursor = page.next_cursor

    assert len(seen) == len(set(seen)), "an occurrence was returned on two pages"


def _as_moment(value):
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
