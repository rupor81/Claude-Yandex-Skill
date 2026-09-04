"""One real call against a real Yandex account.

Skipped unless `YANDEX_MCP_LIVE_TESTS=1`, because everything else in this suite
is expected to run with no network and no credentials.

    YANDEX_MCP_LIVE_TESTS=1 uv run pytest tests/live -q
"""

from __future__ import annotations

import os

import anyio
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("YANDEX_MCP_LIVE_TESTS") != "1",
    reason="live tests need YANDEX_MCP_LIVE_TESTS=1 and a configured account",
)


def test_calendar_list_returns_real_calendars():
    from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient
    from yandex_calendar_mcp.tools.calendars import build_calendar_list
    from yandex_core.config import load_profile
    from yandex_core.credentials import get_secret

    profile = load_profile()

    async def provider() -> CalDAVCalendarClient:
        return CalDAVCalendarClient(
            url=profile.caldav_url,
            username=profile.login,
            password=get_secret("calendar", profile.name),
        )

    page = anyio.run(build_calendar_list(provider))

    assert page.items, "the account reported no calendars at all"
    assert page.complete is True
    assert page.next_cursor is None
    for calendar in page.items:
        assert calendar.name
        assert calendar.url.startswith("http")
