"""`calendar_events_list`: validation, ordering, filtering, and honest paging.

The client is a fake, so nothing here opens a socket. What the fake records is
itself part of the contract: the tool must never hand the client a text filter.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import anyio
import pytest
from yandex_calendar_mcp.client.recurrence import (
    Expansion,
    Occurrence,
    occurrence_sort_key,
)
from yandex_calendar_mcp.tools.events import (
    MAX_LIMIT,
    MAX_RANGE_DAYS,
    MORE_PAGES,
    RANGE_TRUNCATED,
    TOOL_NAME,
    UNREADABLE_DATA,
    build_calendar_events_list,
)
from yandex_core.errors import ProtocolError
from yandex_core.paging import encode_cursor, encode_position_cursor

MOSCOW = timezone(timedelta(hours=3))
START = datetime(2026, 6, 1, tzinfo=MOSCOW)
END = datetime(2026, 6, 30, tzinfo=MOSCOW)
CALENDAR = "https://caldav.example/me/personal/"


OTHER_CALENDAR = "https://caldav.example/me/work/"


def occurrence(
    uid: str,
    start,
    *,
    recurrence_id: str | None = None,
    summary: str = "Meeting",
    end=None,
    all_day: bool = False,
    calendar_url: str = CALENDAR,
    calendar_name: str = "Personal",
) -> Occurrence:
    return Occurrence(
        uid=uid,
        recurrence_id=recurrence_id,
        summary=summary,
        start=start,
        end=end if end is not None else start,
        all_day=all_day,
        calendar_url=calendar_url,
        calendar_name=calendar_name,
    )


class FakeClient:
    """Records every call, and honours `after` and the ceiling as the real one does.

    A fake that ignored `after` would make every paging test pass against a tool
    that never resumed at all.
    """

    def __init__(
        self,
        occurrences,
        *,
        unreadable: int = 0,
        unreadable_calendars: int = 0,
        truncated: bool = False,
        ceiling: int | None = None,
    ) -> None:
        self.occurrences = sorted(occurrences, key=occurrence_sort_key)
        self.unreadable = unreadable
        self.unreadable_calendars = unreadable_calendars
        self.forced_truncated = truncated
        self.ceiling = ceiling
        self.calls: list[dict] = []

    async def list_occurrences(self, **kwargs):
        self.calls.append(kwargs)
        after = kwargs.get("after")
        remaining = [
            o for o in self.occurrences if after is None or occurrence_sort_key(o) > after
        ]
        truncated = self.forced_truncated
        if self.ceiling is not None:
            truncated = len(remaining) > self.ceiling
            remaining = remaining[: self.ceiling]
        return Expansion(
            occurrences=tuple(remaining),
            unreadable=self.unreadable,
            truncated=truncated,
            unreadable_calendars=self.unreadable_calendars,
        )


def build(occurrences=(), **kwargs):
    client = FakeClient(list(occurrences), **kwargs)

    async def provider():
        return client

    return build_calendar_events_list(provider), client


def call(tool, **kwargs):
    async def run():
        return await tool(**kwargs)

    return anyio.run(run)


def daily(count: int, *, uid: str = "series-1", summary: str = "Standup"):
    return [
        occurrence(
            uid,
            datetime(2026, 6, 1 + day, 9, tzinfo=MOSCOW),
            recurrence_id=datetime(2026, 6, 1 + day, 9, tzinfo=MOSCOW).isoformat(),
            summary=summary,
        )
        for day in range(count)
    ]


# -- the shape of one occurrence ------------------------------------------


def test_single_and_recurring_occurrences_carry_their_own_times():
    tool, _ = build(
        [
            occurrence(
                "single-1",
                datetime(2026, 6, 2, 10, tzinfo=MOSCOW),
                end=datetime(2026, 6, 2, 11, tzinfo=MOSCOW),
                summary="Dentist",
            ),
            *daily(2),
        ]
    )

    page = call(tool, start=START, end=END)

    by_uid = {item.uid: item for item in page.items}
    assert by_uid["single-1"].recurrence_id is None
    assert by_uid["single-1"].end == datetime(2026, 6, 2, 11, tzinfo=MOSCOW)
    series = [item for item in page.items if item.uid == "series-1"]
    assert len(series) == 2
    assert all(item.recurrence_id for item in series)
    assert len({item.recurrence_id for item in series}) == 2
    assert page.complete is True
    assert page.next_cursor is None
    assert page.unreadable == 0


def test_an_all_day_occurrence_stays_a_date_through_the_model():
    tool, _ = build(
        [occurrence("allday-1", date(2026, 6, 12), end=date(2026, 6, 13), all_day=True)]
    )

    (item,) = call(tool, start=START, end=END).items

    assert item.all_day is True
    assert item.start == date(2026, 6, 12)
    assert not isinstance(item.start, datetime)
    assert item.model_dump(mode="json")["start"] == "2026-06-12"


def test_results_come_back_ordered_by_start_then_uid_then_recurrence_id():
    later = occurrence("b", datetime(2026, 6, 5, 9, tzinfo=MOSCOW))
    earlier = occurrence("a", datetime(2026, 6, 2, 9, tzinfo=MOSCOW))
    tool, _ = build([earlier, later])

    page = call(tool, start=START, end=END)

    assert [item.uid for item in page.items] == ["a", "b"]


# -- validation, before any request ---------------------------------------


@pytest.mark.parametrize("field", ["start", "end"])
def test_a_naive_timestamp_is_refused_before_any_request(field):
    tool, client = build(daily(1))
    arguments = {"start": START, "end": END}
    arguments[field] = arguments[field].replace(tzinfo=None)

    with pytest.raises(ProtocolError) as raised:
        call(tool, **arguments)

    assert field in str(raised.value)
    assert "offset" in str(raised.value)
    assert client.calls == []


def test_an_inverted_range_is_refused_before_any_request():
    tool, client = build(daily(1))

    with pytest.raises(ProtocolError) as raised:
        call(tool, start=END, end=START)

    assert "after" in str(raised.value)
    assert client.calls == []


def test_an_empty_range_is_refused():
    tool, client = build(daily(1))

    with pytest.raises(ProtocolError):
        call(tool, start=START, end=START)

    assert client.calls == []


def test_a_range_wider_than_the_maximum_is_refused_by_name_not_narrowed():
    tool, client = build(daily(1))
    too_wide = START + timedelta(days=MAX_RANGE_DAYS + 1)

    with pytest.raises(ProtocolError) as raised:
        call(tool, start=START, end=too_wide)

    assert str(MAX_RANGE_DAYS) in str(raised.value)
    assert client.calls == []


def test_a_range_exactly_at_the_maximum_is_allowed():
    tool, client = build(daily(1))

    call(tool, start=START, end=START + timedelta(days=MAX_RANGE_DAYS))

    assert len(client.calls) == 1


def test_the_range_is_required():
    tool, _ = build(daily(1))

    with pytest.raises(TypeError):
        call(tool)


@pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1, "10", True])
def test_a_limit_outside_the_documented_range_is_refused(limit):
    tool, client = build(daily(1))

    with pytest.raises(ProtocolError):
        call(tool, start=START, end=END, limit=limit)

    assert client.calls == []


def test_iso_strings_are_accepted_and_naive_ones_still_refused():
    tool, client = build(daily(1))

    call(tool, start="2026-06-01T00:00:00+03:00", end="2026-06-30T00:00:00+03:00")
    assert client.calls[0]["start"] == START

    with pytest.raises(ProtocolError):
        call(tool, start="2026-06-01T00:00:00", end="2026-06-30T00:00:00+03:00")


# -- what the client is, and is not, asked ---------------------------------


def test_the_client_receives_the_range_and_never_a_text_parameter():
    tool, client = build(daily(1))

    call(tool, start=START, end=END, title_contains="stand", calendar_url=CALENDAR)

    (arguments,) = client.calls
    assert arguments == {
        "start": START,
        "end": END,
        "calendar_url": CALENDAR,
        "after": None,
    }


def test_the_client_exposes_no_text_search_parameter_at_all():
    import inspect

    from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient

    parameters = set(inspect.signature(CalDAVCalendarClient.list_occurrences).parameters)
    assert not parameters & {"title", "title_contains", "text", "summary", "query"}


# -- truncation and paging -------------------------------------------------


def test_more_occurrences_than_limit_yields_a_cursor_naming_the_last_returned():
    tool, _ = build(daily(5))

    page = call(tool, start=START, end=END, limit=2)

    assert len(page.items) == 2
    assert page.complete is False
    assert page.next_cursor is not None
    assert page.items[-1].start == datetime(2026, 6, 2, 9, tzinfo=MOSCOW)


def test_a_cursor_resumes_after_the_named_occurrence():
    tool, _ = build(daily(5))

    first = call(tool, start=START, end=END, limit=2)
    second = call(tool, start=START, end=END, limit=2, cursor=first.next_cursor)

    assert [item.start for item in second.items] == [
        datetime(2026, 6, 3, 9, tzinfo=MOSCOW),
        datetime(2026, 6, 4, 9, tzinfo=MOSCOW),
    ]
    third = call(tool, start=START, end=END, limit=2, cursor=second.next_cursor)
    assert [item.start for item in third.items] == [
        datetime(2026, 6, 5, 9, tzinfo=MOSCOW)
    ]
    assert third.complete is True
    assert third.next_cursor is None


def test_paging_visits_every_occurrence_exactly_once():
    tool, _ = build(daily(5))

    seen = []
    cursor = None
    while True:
        page = call(tool, start=START, end=END, limit=2, cursor=cursor)
        seen.extend((item.uid, item.recurrence_id) for item in page.items)
        if page.complete:
            break
        cursor = page.next_cursor

    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_an_occurrence_added_before_the_cursor_does_not_shift_the_next_page():
    """An index cursor would repeat one here; a position cursor cannot."""
    tool, _ = build(daily(5))
    first = call(tool, start=START, end=END, limit=2)

    inserted = occurrence(
        "new-1", datetime(2026, 6, 1, 8, tzinfo=MOSCOW), summary="Earlier"
    )
    grown, _ = build([inserted, *daily(5)])
    second = call(grown, start=START, end=END, limit=2, cursor=first.next_cursor)

    assert [item.start for item in second.items] == [
        datetime(2026, 6, 3, 9, tzinfo=MOSCOW),
        datetime(2026, 6, 4, 9, tzinfo=MOSCOW),
    ]


def test_an_occurrence_removed_before_the_cursor_does_not_skip_the_next_page():
    tool, _ = build(daily(5))
    first = call(tool, start=START, end=END, limit=2)

    shrunk, _ = build(daily(5)[1:])
    second = call(shrunk, start=START, end=END, limit=2, cursor=first.next_cursor)

    assert [item.start for item in second.items] == [
        datetime(2026, 6, 3, 9, tzinfo=MOSCOW),
        datetime(2026, 6, 4, 9, tzinfo=MOSCOW),
    ]


def test_a_cursor_from_another_tool_is_refused():
    tool, _ = build(daily(2))
    foreign = encode_cursor({"offset": 1}, tool="calendar_list")

    with pytest.raises(ProtocolError):
        call(tool, start=START, end=END, cursor=foreign)


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        encode_cursor({"offset": 1}, tool=TOOL_NAME),
        encode_position_cursor({"start": "nonsense", "uid": "u"}, tool=TOOL_NAME),
    ],
)
def test_a_malformed_cursor_is_refused(cursor):
    tool, _ = build(daily(2))

    with pytest.raises(ProtocolError):
        call(tool, start=START, end=END, cursor=cursor)


def test_a_cursor_past_the_end_yields_an_empty_but_honest_page():
    tool, _ = build(daily(2))

    exhausted = call(tool, start=START, end=END, limit=2)
    assert exhausted.complete is True

    # A cursor from a page that did have a successor, once its rows are gone.
    first = call(tool, start=START, end=END, limit=1)
    emptied, _ = build([])
    page = call(emptied, start=START, end=END, cursor=first.next_cursor)

    assert page.items == []
    assert page.complete is True
    assert page.next_cursor is None
    assert page.incomplete_reason is None


# -- the title filter ------------------------------------------------------


def test_the_title_filter_is_applied_in_the_tool_layer():
    tool, _ = build([*daily(2), *daily(2, uid="other-1", summary="Retro")])

    page = call(tool, start=START, end=END, title_contains="retro")

    assert {item.uid for item in page.items} == {"other-1"}
    assert page.complete is True


def test_the_title_filter_is_case_insensitive_and_a_substring():
    tool, _ = build(daily(1, summary="Weekly Standup"))

    assert call(tool, start=START, end=END, title_contains="STANDup").items


def test_a_filter_over_a_truncated_fetch_is_not_complete():
    tool, _ = build(daily(2, summary="Retro"), truncated=True)

    page = call(tool, start=START, end=END, title_contains="retro")

    assert len(page.items) == 2
    assert page.complete is False
    assert page.next_cursor is not None


def test_a_truncated_fetch_is_never_reported_complete_even_unfiltered():
    tool, _ = build(daily(2), truncated=True)

    page = call(tool, start=START, end=END)

    assert page.complete is False


# -- unreadable events -----------------------------------------------------


def test_an_unreadable_event_is_reported_and_the_good_ones_still_returned():
    tool, _ = build(daily(2), unreadable=1)

    page = call(tool, start=START, end=END)

    assert len(page.items) == 2
    assert page.unreadable == 1
    assert page.complete is False


def test_an_unreadable_event_alone_yields_no_cursor_to_chase():
    tool, _ = build(daily(2), unreadable=1)

    page = call(tool, start=START, end=END)

    assert page.next_cursor is None


# -- determinism -----------------------------------------------------------


def test_identical_arguments_produce_identical_results():
    tool, _ = build(daily(5))

    assert call(tool, start=START, end=END, limit=3) == call(
        tool, start=START, end=END, limit=3
    )


# -- paging through a truncated expansion ----------------------------------


def test_paging_through_a_truncated_expansion_terminates_and_loses_nothing():
    """The ceiling must never leave occurrences behind an unresumable page."""
    tool, _ = build(daily(7), ceiling=2)

    seen = []
    cursor = None
    pages = 0
    while True:
        page = call(tool, start=START, end=END, limit=2, cursor=cursor)
        pages += 1
        assert pages < 20, "paging did not terminate"
        seen.extend((item.uid, item.recurrence_id) for item in page.items)
        if page.complete:
            assert page.next_cursor is None
            assert page.incomplete_reason is None
            break
        assert page.next_cursor is not None, (
            "a page said it was cut short but offered no way to continue"
        )
        cursor = page.next_cursor

    assert len(seen) == 7
    assert len(set(seen)) == 7


def test_a_page_is_never_short_and_unresumable_at_once():
    """The state the contract does not have: empty, incomplete, no cursor."""
    # A filter that matches only the tail, over a fetch the ceiling cut short.
    occurrences = [*daily(4), *daily(2, uid="late-1", summary="Retro")]
    occurrences[-2:] = [
        occurrence(
            "late-1",
            datetime(2026, 6, 20 + day, 9, tzinfo=MOSCOW),
            recurrence_id=datetime(2026, 6, 20 + day, 9, tzinfo=MOSCOW).isoformat(),
            summary="Retro",
        )
        for day in range(2)
    ]
    tool, _ = build(occurrences, ceiling=2)

    page = call(tool, start=START, end=END, title_contains="retro")

    assert page.items == []
    assert page.complete is False
    assert page.next_cursor is not None
    assert page.incomplete_reason == RANGE_TRUNCATED

    # And following it actually reaches the matches.
    found = []
    cursor = page.next_cursor
    while cursor is not None:
        page = call(tool, start=START, end=END, title_contains="retro", cursor=cursor)
        found.extend(item.uid for item in page.items)
        cursor = page.next_cursor
    assert found == ["late-1", "late-1"]


def test_two_calendars_with_an_otherwise_equal_key_both_survive_paging():
    """Without the calendar in the key these share one, and `> after` drops one."""
    moment = datetime(2026, 6, 5, 9, tzinfo=MOSCOW)
    pair = [
        occurrence("shared-1", moment, calendar_url=CALENDAR, calendar_name="Personal"),
        occurrence(
            "shared-1", moment, calendar_url=OTHER_CALENDAR, calendar_name="Work"
        ),
    ]
    tool, _ = build(pair)

    first = call(tool, start=START, end=END, limit=1)
    assert first.complete is False
    second = call(tool, start=START, end=END, limit=1, cursor=first.next_cursor)

    assert second.items, "the duplicate across calendars was dropped by the cursor"
    assert {first.items[0].calendar_url, second.items[0].calendar_url} == {
        CALENDAR,
        OTHER_CALENDAR,
    }
    assert second.complete is True


def test_each_occurrence_reports_the_calendar_it_came_from():
    tool, _ = build(
        [
            occurrence("a", datetime(2026, 6, 2, 9, tzinfo=MOSCOW)),
            occurrence(
                "b",
                datetime(2026, 6, 3, 9, tzinfo=MOSCOW),
                calendar_url=OTHER_CALENDAR,
                calendar_name="Work",
            ),
        ]
    )

    page = call(tool, start=START, end=END)

    assert {(item.uid, item.calendar_url, item.calendar_name) for item in page.items} == {
        ("a", CALENDAR, "Personal"),
        ("b", OTHER_CALENDAR, "Work"),
    }


# -- the cursor is bound to its question -----------------------------------


@pytest.mark.parametrize(
    "changed",
    [
        {"end": END + timedelta(days=1)},
        {"start": START + timedelta(days=1)},
        {"calendar_url": OTHER_CALENDAR},
        {"title_contains": "standup"},
    ],
)
def test_a_cursor_replayed_against_a_different_question_is_refused(changed):
    tool, _ = build(daily(5))
    first = call(tool, start=START, end=END, limit=2)

    arguments = {"start": START, "end": END, "limit": 2, "cursor": first.next_cursor}
    arguments.update(changed)

    with pytest.raises(ProtocolError) as raised:
        call(tool, **arguments)

    assert "different question" in str(raised.value)


def test_a_cursor_replayed_with_the_same_question_still_works():
    tool, _ = build(daily(5))
    first = call(tool, start=START, end=END, limit=2)

    second = call(tool, start=START, end=END, limit=2, cursor=first.next_cursor)

    assert [item.start for item in second.items] == [
        datetime(2026, 6, 3, 9, tzinfo=MOSCOW),
        datetime(2026, 6, 4, 9, tzinfo=MOSCOW),
    ]


def test_the_same_range_written_in_another_offset_is_the_same_question():
    """The cursor binds the moment, not the way the caller spelled it."""
    tool, _ = build(daily(5))
    first = call(tool, start=START, end=END, limit=2)

    second = call(
        tool,
        start=START.astimezone(timezone.utc),
        end=END.astimezone(timezone.utc),
        limit=2,
        cursor=first.next_cursor,
    )

    assert [item.start for item in second.items] == [
        datetime(2026, 6, 3, 9, tzinfo=MOSCOW),
        datetime(2026, 6, 4, 9, tzinfo=MOSCOW),
    ]


def test_a_cursor_naming_a_moment_no_clock_can_hold_is_refused_not_raised():
    tool, _ = build(daily(2))
    absurd = encode_position_cursor(
        {
            "start": "9999-12-31T23:59:59+00:00",
            "calendar_url": CALENDAR,
            "uid": "u",
            "recurrence_id": None,
            "query": "0" * 16,
        },
        tool=TOOL_NAME,
    )

    with pytest.raises(ProtocolError):
        call(tool, start=START, end=END, cursor=absurd)


# -- blank arguments -------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_title_filter_is_refused_rather_than_silently_ignored(blank):
    tool, client = build(daily(2))

    with pytest.raises(ProtocolError) as raised:
        call(tool, start=START, end=END, title_contains=blank)

    assert "title_contains" in str(raised.value)
    assert client.calls == []


def test_the_title_filter_is_stripped_before_matching():
    tool, _ = build(daily(1, summary="Weekly Standup"))

    assert call(tool, start=START, end=END, title_contains="  standup  ").items


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_calendar_url_means_every_calendar(blank):
    tool, client = build(daily(1))

    call(tool, start=START, end=END, calendar_url=blank)

    assert client.calls[0]["calendar_url"] is None


def test_a_calendar_url_is_trimmed_before_it_is_used():
    tool, client = build(daily(1))

    call(tool, start=START, end=END, calendar_url=f"  {CALENDAR}  ")

    assert client.calls[0]["calendar_url"] == CALENDAR


# -- why a page is short ---------------------------------------------------


def test_more_pages_and_a_truncated_range_are_told_apart():
    more, _ = build(daily(5))
    assert call(more, start=START, end=END, limit=2).incomplete_reason == MORE_PAGES

    cut, _ = build(daily(5), ceiling=3)
    page = call(cut, start=START, end=END, limit=50)
    assert len(page.items) == 3
    assert page.incomplete_reason == RANGE_TRUNCATED


def test_unreadable_data_is_named_as_its_own_reason():
    tool, _ = build(daily(2), unreadable=1)
    page = call(tool, start=START, end=END)

    assert page.complete is False
    assert page.incomplete_reason == UNREADABLE_DATA
    assert page.unreadable == 1
    assert page.next_cursor is None


def test_an_unreadable_calendar_is_counted_and_named():
    tool, _ = build(daily(2), unreadable_calendars=1)
    page = call(tool, start=START, end=END)

    assert page.unreadable_calendars == 1
    assert page.complete is False
    assert page.incomplete_reason == UNREADABLE_DATA


def test_a_complete_page_names_no_reason():
    tool, _ = build(daily(2))
    page = call(tool, start=START, end=END)

    assert page.complete is True
    assert page.incomplete_reason is None
