"""`calendar_list` against a fake client: bounded, honest, never empty on failure."""

from __future__ import annotations

import anyio
import pytest
from yandex_calendar_mcp.client.caldav_client import CalendarRef
from yandex_calendar_mcp.tools.calendars import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    build_calendar_list,
)
from yandex_core.errors import AuthError, ProtocolError, TransportError
from yandex_core.results import Page


class FakeCalendarClient:
    def __init__(self, count=0, failure=None):
        self._refs = [
            CalendarRef(name=f"Calendar {i}", url=f"https://caldav.yandex.ru/c/{i}/")
            for i in range(count)
        ]
        self._failure = failure
        self.calls = 0

    async def list_calendars(self):
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return list(self._refs)


def tool_for(client):
    async def provider():
        return client

    return build_calendar_list(provider)


def call(tool, **kwargs) -> Page:
    return anyio.run(lambda: tool(**kwargs))


def test_happy_path_is_complete_with_no_cursor():
    page = call(tool_for(FakeCalendarClient(3)))
    assert [item.name for item in page.items] == ["Calendar 0", "Calendar 1", "Calendar 2"]
    assert page.complete is True
    assert page.next_cursor is None


def test_empty_account_is_a_complete_empty_page():
    page = call(tool_for(FakeCalendarClient(0)))
    assert page.items == []
    assert page.complete is True
    assert page.next_cursor is None


def test_truncation_is_declared_and_carries_a_cursor():
    page = call(tool_for(FakeCalendarClient(5)), limit=2)
    assert len(page.items) == 2
    assert page.complete is False
    assert page.next_cursor is not None


def test_cursor_resumes_exactly_where_the_page_stopped():
    client = FakeCalendarClient(5)
    tool = tool_for(client)

    first = call(tool, limit=2)
    second = call(tool, limit=2, cursor=first.next_cursor)
    third = call(tool, limit=2, cursor=second.next_cursor)

    names = [i.name for i in first.items + second.items + third.items]
    assert names == [f"Calendar {i}" for i in range(5)]
    assert third.complete is True
    assert third.next_cursor is None


def test_a_foreign_cursor_is_rejected_rather_than_ignored():
    with pytest.raises(ProtocolError):
        call(tool_for(FakeCalendarClient(3)), cursor="offset=1")


@pytest.mark.parametrize(
    "failure", [AuthError("wrong app password"), TransportError("network down")]
)
def test_failures_are_never_turned_into_an_empty_success(failure):
    with pytest.raises(type(failure)):
        call(tool_for(FakeCalendarClient(3, failure=failure)))


def test_default_limit_is_applied():
    page = call(tool_for(FakeCalendarClient(DEFAULT_LIMIT + 1)))
    assert len(page.items) == DEFAULT_LIMIT
    assert page.complete is False


def test_a_cursor_minted_by_another_tool_is_refused():
    """Cursors carry their issuer, so another listing's cursor is not an offset."""
    from yandex_core.paging import encode_cursor

    foreign = encode_cursor({"offset": 1}, tool="event_list")
    with pytest.raises(ProtocolError):
        call(tool_for(FakeCalendarClient(3)), cursor=foreign)


def test_a_cursor_naming_a_deleted_calendar_still_resumes():
    """The position it names need not still exist; it only has to order.

    This replaces an older rule that a cursor past the end was an error. That
    rule existed to compensate for an index cursor, which could not tell a
    shrunken list from a finished one. A position can: everything after it is
    still well defined once the calendar it names is gone.
    """
    client = FakeCalendarClient(count=4)
    first = call(tool_for(client), limit=2)
    assert [c.name for c in first.items] == ["Calendar 0", "Calendar 1"]

    # The calendar the cursor names is unsubscribed before the next page.
    client._refs = [ref for ref in client._refs if ref.name != "Calendar 1"]

    second = call(tool_for(client), limit=2, cursor=first.next_cursor)
    assert [c.name for c in second.items] == ["Calendar 2", "Calendar 3"]
    assert second.complete is True


def test_an_offset_shaped_cursor_is_refused_rather_than_reinterpreted():
    """Cursors from the previous index scheme name nothing this tool can resume."""
    from yandex_core.paging import encode_cursor

    stale = encode_cursor({"offset": 2}, tool="calendar_list")
    with pytest.raises(ProtocolError):
        call(tool_for(FakeCalendarClient(3)), cursor=stale)


def test_two_calendars_sharing_a_name_both_survive_paging():
    """The URL breaks the tie, so a strict resume cannot drop either of them."""
    client = FakeCalendarClient(count=0)
    client._refs = [
        CalendarRef(name="Shared", url="https://caldav.yandex.ru/c/a/"),
        CalendarRef(name="Shared", url="https://caldav.yandex.ru/c/b/"),
    ]
    first = call(tool_for(client), limit=1)
    second = call(tool_for(client), limit=1, cursor=first.next_cursor)
    seen = [c.url for c in first.items] + [c.url for c in second.items]
    assert seen == [
        "https://caldav.yandex.ru/c/a/",
        "https://caldav.yandex.ru/c/b/",
    ]


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1, 10_000])
def test_limits_outside_the_range_are_refused_by_the_function_itself(limit):
    """The JSON schema binds a protocol caller; the function must bind everyone."""
    with pytest.raises(ProtocolError) as caught:
        call(tool_for(FakeCalendarClient(3)), limit=limit)
    assert str(MAX_LIMIT) in str(caught.value)


@pytest.mark.parametrize("limit", [True, "5", 1.5, None])
def test_a_non_integer_limit_is_refused(limit):
    with pytest.raises(ProtocolError):
        call(tool_for(FakeCalendarClient(3)), limit=limit)


@pytest.mark.parametrize("limit", [MIN_LIMIT, 2, MAX_LIMIT])
def test_limits_at_the_edges_of_the_range_are_accepted(limit):
    page = call(tool_for(FakeCalendarClient(1)), limit=limit)
    assert page.complete is True


def test_an_out_of_range_limit_never_reaches_the_client():
    """Validation happens before the network, not after it."""
    client = FakeCalendarClient(3)
    with pytest.raises(ProtocolError):
        call(tool_for(client), limit=0)
    assert client.calls == 0


def test_a_calendar_removed_between_pages_does_not_hide_the_next_one():
    """Resuming must name where it stopped, not count how far it got.

    An index cursor means "skip N". If a calendar earlier in the list is
    unsubscribed between two pages, everything after it shifts up by one and
    "skip N" lands one calendar too far, silently omitting one from the answer.
    A cursor that names the last calendar returned resumes correctly instead.
    """
    client = FakeCalendarClient(count=4)
    first = call(tool_for(client), limit=2)
    assert [c.name for c in first.items] == ["Calendar 0", "Calendar 1"]
    assert first.complete is False and first.next_cursor

    # The account changes: the first calendar is unsubscribed.
    client._refs = client._refs[1:]

    second = call(tool_for(client), limit=2, cursor=first.next_cursor)
    assert [c.name for c in second.items] == ["Calendar 2", "Calendar 3"], (
        "resuming after 'Calendar 1' must return Calendar 2 next; an index "
        "cursor skips it because the list shifted"
    )
