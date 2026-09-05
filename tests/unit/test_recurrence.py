"""Expansion is where the hard part of this story lives, so it is tested alone.

No network and no CalDAV: these run against iCalendar text directly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from yandex_calendar_mcp.client.recurrence import (
    CalendarSource,
    expand,
    format_instant,
    occurrence_sort_key,
    parse_instant,
    position_sort_key,
)
from yandex_core.errors import ProtocolError

MOSCOW = timezone(timedelta(hours=3))
RANGE_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 6, 30, tzinfo=timezone.utc)


def document(body: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + body + "END:VCALENDAR\r\n"


def source(body: str, *, url: str = "https://caldav.example/me/personal/") -> CalendarSource:
    return CalendarSource(ics=document(body), calendar_url=url, calendar_name="Personal")


SINGLE = """BEGIN:VEVENT
UID:single-1
SUMMARY:Dentist
DTSTART;TZID=Europe/Moscow:20260610T100000
DTEND;TZID=Europe/Moscow:20260610T110000
END:VEVENT
"""

SERIES = """BEGIN:VEVENT
UID:series-1
SUMMARY:Standup
DTSTART;TZID=Europe/Moscow:20260608T090000
DTEND;TZID=Europe/Moscow:20260608T091500
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
"""

SERIES_WITH_EXDATE = """BEGIN:VEVENT
UID:series-1
SUMMARY:Standup
DTSTART;TZID=Europe/Moscow:20260608T090000
DTEND;TZID=Europe/Moscow:20260608T091500
RRULE:FREQ=DAILY;COUNT=5
EXDATE;TZID=Europe/Moscow:20260610T090000
END:VEVENT
"""

SERIES_WITH_OVERRIDE = SERIES + """BEGIN:VEVENT
UID:series-1
SUMMARY:Standup (moved)
RECURRENCE-ID;TZID=Europe/Moscow:20260611T090000
DTSTART;TZID=Europe/Moscow:20260611T113000
DTEND;TZID=Europe/Moscow:20260611T120000
END:VEVENT
"""

ALL_DAY = """BEGIN:VEVENT
UID:allday-1
SUMMARY:Public holiday
DTSTART;VALUE=DATE:20260612
DTEND;VALUE=DATE:20260613
END:VEVENT
"""


def run(*bodies: str, **kwargs):
    return expand(
        [source(body) for body in bodies],
        start=RANGE_START,
        end=RANGE_END,
        **kwargs,
    )


def test_single_event_has_no_recurrence_id():
    result = run(SINGLE)

    (occurrence,) = result.occurrences
    assert occurrence.uid == "single-1"
    assert occurrence.recurrence_id is None
    assert occurrence.start == datetime(2026, 6, 10, 10, tzinfo=MOSCOW)
    assert occurrence.end == datetime(2026, 6, 10, 11, tzinfo=MOSCOW)
    assert occurrence.all_day is False
    assert result.unreadable == 0


def test_series_expands_into_one_occurrence_per_instance():
    result = run(SERIES)

    assert len(result.occurrences) == 5
    assert {o.uid for o in result.occurrences} == {"series-1"}
    starts = [o.start for o in result.occurrences]
    assert starts == sorted(starts)
    # Each instance carries its own start and its own place in the series.
    assert [o.recurrence_id for o in result.occurrences] == [
        format_instant(start) for start in starts
    ]
    assert len(set(o.recurrence_id for o in result.occurrences)) == 5


def test_every_returned_timestamp_carries_an_offset():
    for occurrence in run(SERIES, SINGLE).occurrences:
        assert occurrence.start.utcoffset() is not None
        assert occurrence.end.utcoffset() is not None


def test_exdate_cancels_only_that_instance():
    result = run(SERIES_WITH_EXDATE)

    cancelled = datetime(2026, 6, 10, 9, tzinfo=MOSCOW)
    assert len(result.occurrences) == 4
    assert cancelled not in [o.start for o in result.occurrences]
    assert result.unreadable == 0


def test_recurrence_id_override_appears_once_in_its_modified_form():
    result = run(SERIES_WITH_OVERRIDE)

    moved = [o for o in result.occurrences if o.summary == "Standup (moved)"]
    assert len(moved) == 1
    assert moved[0].start == datetime(2026, 6, 11, 11, 30, tzinfo=MOSCOW)
    # The instance it replaces is gone, not returned alongside it.
    assert datetime(2026, 6, 11, 9, tzinfo=MOSCOW) not in [
        o.start for o in result.occurrences
    ]
    assert len(result.occurrences) == 5


def test_all_day_event_stays_a_date():
    (occurrence,) = run(ALL_DAY).occurrences

    assert occurrence.all_day is True
    assert occurrence.start == date(2026, 6, 12)
    assert not isinstance(occurrence.start, datetime)
    assert occurrence.end == date(2026, 6, 13)


def test_events_outside_the_range_are_filtered_out_locally():
    """Yandex answers with extras, so the server's own range filter is not trusted."""
    outside = """BEGIN:VEVENT
UID:outside-1
SUMMARY:Last year
DTSTART;TZID=Europe/Moscow:20250610T100000
DTEND;TZID=Europe/Moscow:20250610T110000
END:VEVENT
"""
    result = run(SINGLE, outside)

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 0


def test_one_unparseable_document_does_not_lose_the_others():
    result = expand(
        [
            source(SINGLE),
            CalendarSource(ics="this is not iCalendar", calendar_url="u", calendar_name="c"),
        ],
        start=RANGE_START,
        end=RANGE_END,
    )

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 1


def test_one_malformed_series_among_good_ones_is_counted_not_dropped():
    broken = """BEGIN:VEVENT
UID:broken-1
SUMMARY:Broken rule
DTSTART;TZID=Europe/Moscow:20260610T100000
RRULE:FREQ=NONSENSE;COUNT=x
END:VEVENT
"""
    result = run(SINGLE, broken)

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 1


def test_a_malformed_event_beside_a_good_one_in_the_same_document():
    """A CalDAV object may hold more than one component; one bad UID costs one."""
    body = SINGLE + """BEGIN:VEVENT
UID:broken-2
SUMMARY:Broken rule
DTSTART;TZID=Europe/Moscow:20260610T100000
RRULE:FREQ=NONSENSE;COUNT=x
END:VEVENT
"""
    result = run(body)

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 1


def test_an_event_without_a_uid_is_counted_not_dropped():
    body = """BEGIN:VEVENT
SUMMARY:Anonymous
DTSTART;TZID=Europe/Moscow:20260610T100000
DTEND;TZID=Europe/Moscow:20260610T110000
END:VEVENT
"""
    result = run(SINGLE, body)

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 1


def test_a_floating_timestamp_is_reported_rather_than_given_an_invented_offset():
    body = """BEGIN:VEVENT
UID:floating-1
SUMMARY:Floating
DTSTART:20260610T100000
DTEND:20260610T110000
END:VEVENT
"""
    result = run(SINGLE, body)

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 1


def test_order_is_start_then_uid_then_recurrence_id():
    result = run(SERIES_WITH_OVERRIDE, SINGLE, ALL_DAY)

    keys = [
        position_sort_key(o.start, o.calendar_url, o.uid, o.recurrence_id)
        for o in result.occurrences
    ]
    assert keys == sorted(keys)


def test_expansion_is_deterministic():
    first = run(SERIES_WITH_OVERRIDE, SINGLE, ALL_DAY)
    second = run(SERIES_WITH_OVERRIDE, SINGLE, ALL_DAY)

    assert first == second


def test_the_ceiling_truncates_in_order_and_says_so():
    result = run(SERIES, ceiling=2)

    assert result.truncated is True
    assert len(result.occurrences) == 2
    assert [o.start for o in result.occurrences] == [
        datetime(2026, 6, 8, 9, tzinfo=MOSCOW),
        datetime(2026, 6, 9, 9, tzinfo=MOSCOW),
    ]


def test_a_result_within_the_ceiling_is_not_marked_truncated():
    assert run(SERIES, ceiling=5).truncated is False


@pytest.mark.parametrize(
    "value",
    [date(2026, 6, 12), datetime(2026, 6, 10, 10, tzinfo=MOSCOW)],
)
def test_instants_round_trip_without_changing_type(value):
    assert parse_instant(format_instant(value)) == value
    assert type(parse_instant(format_instant(value))) is type(value)


def test_a_naive_instant_string_is_refused():
    with pytest.raises(ValueError):
        parse_instant("2026-06-10T10:00:00")


# -- window semantics ------------------------------------------------------
#
# Start-inclusive, end-exclusive, judged on the occurrence's own start. The
# three boundary cases are the whole contract, so all three are pinned.


def _at(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _one_off(uid: str, begins: datetime, *, hours: int = 1) -> str:
    return (
        f"BEGIN:VEVENT\nUID:{uid}\nSUMMARY:{uid}\n"
        f"DTSTART:{_at(begins)}\n"
        f"DTEND:{_at(begins + timedelta(hours=hours))}\n"
        "END:VEVENT\n"
    )


def test_an_event_starting_before_the_window_is_not_returned_even_if_it_overlaps():
    """Otherwise the same meeting reappears in every range it spans."""
    result = run(_one_off("early-1", RANGE_START - timedelta(hours=2), hours=6))

    assert result.occurrences == ()


def test_an_event_starting_exactly_at_the_window_start_is_returned():
    result = run(_one_off("edge-start", RANGE_START))

    assert [o.uid for o in result.occurrences] == ["edge-start"]


def test_an_event_starting_exactly_at_the_window_end_belongs_to_the_next_range():
    result = run(_one_off("edge-end", RANGE_END))

    assert result.occurrences == ()

    following = expand(
        [source(_one_off("edge-end", RANGE_END))],
        start=RANGE_END,
        end=RANGE_END + timedelta(days=1),
    )
    assert [o.uid for o in following.occurrences] == ["edge-end"]


# -- cancellations ---------------------------------------------------------

CANCELLED_EVENT = """BEGIN:VEVENT
UID:cancelled-1
SUMMARY:Called off
DTSTART;TZID=Europe/Moscow:20260610T100000
DTEND;TZID=Europe/Moscow:20260610T110000
STATUS:CANCELLED
END:VEVENT
"""

SERIES_WITH_CANCELLED_INSTANCE = SERIES + """BEGIN:VEVENT
UID:series-1
SUMMARY:Standup
RECURRENCE-ID;TZID=Europe/Moscow:20260610T090000
DTSTART;TZID=Europe/Moscow:20260610T090000
DTEND;TZID=Europe/Moscow:20260610T091500
STATUS:CANCELLED
END:VEVENT
"""


def test_a_cancelled_event_is_absent_not_returned_as_a_meeting():
    result = run(SINGLE, CANCELLED_EVENT)

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 0


def test_a_cancelled_recurrence_override_removes_only_that_instance():
    result = run(SERIES_WITH_CANCELLED_INSTANCE)

    starts = [o.start for o in result.occurrences]
    assert datetime(2026, 6, 10, 9, tzinfo=MOSCOW) not in starts
    assert len(result.occurrences) == 4
    assert result.unreadable == 0


# -- grouping across sources ----------------------------------------------


def test_an_override_stored_as_its_own_object_still_replaces_its_instance():
    """CalDAV may keep an override in a separate object; per-source grouping
    would expand it standalone and return the instance it replaces twice."""
    override = """BEGIN:VEVENT
UID:series-1
SUMMARY:Standup (moved)
RECURRENCE-ID;TZID=Europe/Moscow:20260611T090000
DTSTART;TZID=Europe/Moscow:20260611T113000
DTEND;TZID=Europe/Moscow:20260611T120000
END:VEVENT
"""
    result = run(SERIES, override)

    assert len(result.occurrences) == 5
    moved = [o for o in result.occurrences if o.summary == "Standup (moved)"]
    assert len(moved) == 1
    assert datetime(2026, 6, 11, 9, tzinfo=MOSCOW) not in [
        o.start for o in result.occurrences
    ]


def test_the_same_uid_in_two_calendars_is_two_occurrences_with_distinct_keys():
    """A duplicate across calendars is routine; identical keys would drop one."""
    result = expand(
        [
            source(SINGLE, url="https://caldav.example/me/personal/"),
            source(SINGLE, url="https://caldav.example/me/work/"),
        ],
        start=RANGE_START,
        end=RANGE_END,
    )

    assert len(result.occurrences) == 2
    keys = [occurrence_sort_key(o) for o in result.occurrences]
    assert len(set(keys)) == 2, "the two share a sort key; a cursor would drop one"
    assert {o.calendar_url for o in result.occurrences} == {
        "https://caldav.example/me/personal/",
        "https://caldav.example/me/work/",
    }


# -- scoping the unreadable count -----------------------------------------


def test_a_broken_series_that_cannot_reach_the_window_is_not_counted():
    """One bad invite from years ago must not taint every future query."""
    broken_long_ago = """BEGIN:VEVENT
UID:floating-old
SUMMARY:Floating, and years before the window
DTSTART:20180610T100000
DTEND:20180610T110000
END:VEVENT
"""
    result = run(SINGLE, broken_long_ago)

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 0


def test_a_broken_series_that_could_reach_the_window_is_still_counted():
    broken_recurring = """BEGIN:VEVENT
UID:floating-live
SUMMARY:Floating, and still recurring into the window
DTSTART:20180610T100000
DTEND:20180610T110000
RRULE:FREQ=DAILY
END:VEVENT
"""
    result = run(SINGLE, broken_recurring)

    assert [o.uid for o in result.occurrences] == ["single-1"]
    assert result.unreadable == 1


# -- the ceiling and the resume point --------------------------------------


@pytest.mark.parametrize("ceiling", [0, -1, 1.5, True])
def test_a_ceiling_below_one_is_refused(ceiling):
    with pytest.raises(ProtocolError):
        run(SERIES, ceiling=ceiling)


def test_the_ceiling_counts_what_is_left_after_the_resume_point():
    """Applied to the whole set instead, the tail would be unreachable."""
    first = run(SERIES, ceiling=2)
    assert first.truncated is True

    seen = [o.recurrence_id for o in first.occurrences]
    after = occurrence_sort_key(first.occurrences[-1])
    while True:
        page = run(SERIES, ceiling=2, after=after)
        if not page.occurrences:
            assert page.truncated is False
            break
        seen.extend(o.recurrence_id for o in page.occurrences)
        after = occurrence_sort_key(page.occurrences[-1])

    assert len(seen) == 5
    assert len(set(seen)) == 5


# -- daylight saving and all-day series ------------------------------------


def test_a_series_crossing_the_october_transition_keeps_its_local_time():
    """The library owns this; that it is delegated is exactly why it is tested."""
    body = """BEGIN:VEVENT
UID:dst-october
SUMMARY:Weekly
DTSTART;TZID=Europe/Berlin:20261021T090000
DTEND;TZID=Europe/Berlin:20261021T100000
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
"""
    result = expand(
        [source(body)],
        start=datetime(2026, 10, 1, tzinfo=timezone.utc),
        end=datetime(2026, 11, 30, tzinfo=timezone.utc),
    )

    assert len(result.occurrences) == 3
    # Weekly from 21 October: the 25th is when Berlin leaves summer time.
    offsets = [o.start.utcoffset() for o in result.occurrences]
    assert offsets == [timedelta(hours=2), timedelta(hours=1), timedelta(hours=1)]
    assert all(o.start.hour == 9 for o in result.occurrences)
    assert result.unreadable == 0


def test_a_series_crossing_the_march_transition_keeps_its_local_time():
    body = """BEGIN:VEVENT
UID:dst-march
SUMMARY:Weekly
DTSTART;TZID=Europe/Berlin:20260318T090000
DTEND;TZID=Europe/Berlin:20260318T100000
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
"""
    result = expand(
        [source(body)],
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )

    assert len(result.occurrences) == 3
    offsets = [o.start.utcoffset() for o in result.occurrences]
    assert offsets == [timedelta(hours=1), timedelta(hours=1), timedelta(hours=2)]
    assert all(o.start.hour == 9 for o in result.occurrences)


def test_a_weekly_all_day_series_stays_dates_through_the_cursor_round_trip():
    body = """BEGIN:VEVENT
UID:allday-series
SUMMARY:Sprint day
DTSTART;VALUE=DATE:20260603
DTEND;VALUE=DATE:20260604
RRULE:FREQ=WEEKLY;COUNT=4
END:VEVENT
"""
    result = run(body)

    assert len(result.occurrences) == 4
    for occurrence in result.occurrences:
        assert occurrence.all_day is True
        assert not isinstance(occurrence.start, datetime)
        assert not isinstance(occurrence.end, datetime)
        # The cursor writes the start out and reads it back; a date must survive.
        restored = parse_instant(format_instant(occurrence.start))
        assert restored == occurrence.start
        assert type(restored) is date

    # And the round trip must land on the same place in the order.
    for occurrence in result.occurrences:
        assert position_sort_key(
            parse_instant(format_instant(occurrence.start)),
            occurrence.calendar_url,
            occurrence.uid,
            occurrence.recurrence_id,
        ) == occurrence_sort_key(occurrence)


def test_an_all_day_and_a_timed_series_order_together_deterministically():
    mixed = run(ALL_DAY, SERIES)
    keys = [occurrence_sort_key(o) for o in mixed.occurrences]
    assert keys == sorted(keys)
