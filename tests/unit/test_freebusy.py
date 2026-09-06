"""`calendar_freebusy_query`: what counts as busy, and what must never be guessed.

Every test here is named for the harm it prevents. The client is a fake, so
nothing opens a socket; what the fake *records* is part of the contract too --
a range that was refused must never have reached the wire.

Two halves:

* the client layer, where transparency and the operator's own reply are read off
  the component, and where an occurrence that began before the window can be
  seen at all;
* the tool, where those facts become intervals: classified, clipped, merged, and
  reported without a single event title.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import anyio
import pytest
from yandex_calendar_mcp.client.recurrence import (
    CalendarSource,
    Expansion,
    Occurrence,
    expand,
)
from yandex_calendar_mcp.tools.freebusy import (
    BUSY,
    BUSY_TENTATIVE,
    BUSY_UNANSWERED,
    CLIPPED_BOTH_NOTE,
    CLIPPED_END_NOTE,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_RANGE_DAYS,
    MORE_PAGES,
    RANGE_TRUNCATED,
    TOOL_NAME,
    UNREADABLE_DATA,
    BusyInterval,
    FreeBusyPage,
    KINDS,
    build_calendar_freebusy_query,
    merge_intervals,
)
from yandex_core.errors import ProtocolError
from yandex_core.paging import encode_cursor
from yandex_core.risk import RISK_REGISTRY, RiskClass

MOSCOW = timezone(timedelta(hours=3))
START = datetime(2026, 6, 1, tzinfo=MOSCOW)
END = datetime(2026, 6, 8, tzinfo=MOSCOW)
CALENDAR = "https://caldav.example/me/personal/"


def moment(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=MOSCOW)


def occurrence(
    uid: str,
    start,
    end,
    *,
    transparency: str = "OPAQUE",
    participation_status: str | None = None,
    summary: str = "Quarterly compensation review",
    recurrence_id: str | None = None,
    all_day: bool = False,
    calendar_url: str = CALENDAR,
) -> Occurrence:
    return Occurrence(
        uid=uid,
        recurrence_id=recurrence_id,
        summary=summary,
        start=start,
        end=end,
        all_day=all_day,
        calendar_url=calendar_url,
        calendar_name="Personal",
        transparency=transparency,
        participation_status=participation_status,
    )


class FakeClient:
    """Stands in for the CalDAV client, recording exactly what it was asked."""

    def __init__(
        self,
        occurrences=(),
        *,
        unreadable: int = 0,
        unreadable_calendars: int = 0,
        truncated: bool = False,
    ) -> None:
        self.occurrences = list(occurrences)
        self.unreadable = unreadable
        self.unreadable_calendars = unreadable_calendars
        self.truncated = truncated
        self.calls: list[dict] = []

    async def list_occurrences(self, **kwargs):
        self.calls.append(kwargs)
        return Expansion(
            occurrences=tuple(self.occurrences),
            unreadable=self.unreadable,
            unreadable_calendars=self.unreadable_calendars,
            truncated=self.truncated,
        )


def build(client: FakeClient):
    async def provider():
        return client

    return build_calendar_freebusy_query(provider)


def call(tool, **kwargs):
    kwargs.setdefault("start", START)
    kwargs.setdefault("end", END)
    return anyio.run(lambda: tool(**kwargs))


# -- what the occurrence has to carry before any of this is possible ------


ICS_HEADER = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"


def document(*events: str) -> CalendarSource:
    return CalendarSource(
        ics=ICS_HEADER + "".join(events) + "END:VCALENDAR\r\n",
        calendar_url=CALENDAR,
        calendar_name="Personal",
    )


def event(
    uid: str,
    *,
    start: str = "20260603T100000",
    end: str = "20260603T110000",
    extra: str = "",
) -> str:
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:Meeting\r\n"
        f"DTSTART;TZID=Europe/Moscow:{start}\r\n"
        f"DTEND;TZID=Europe/Moscow:{end}\r\n" + extra + "END:VEVENT\r\n"
    )


def test_transparency_is_read_from_the_event_not_assumed_opaque():
    """Without it, an event marked free would block the whole afternoon."""
    expansion = expand(
        [
            document(
                event("open", extra="TRANSP:TRANSPARENT\r\n"),
                event("solid", start="20260603T120000", end="20260603T130000"),
            )
        ],
        start=START,
        end=END,
    )
    by_uid = {o.uid: o for o in expansion.occurrences}
    assert by_uid["open"].transparency == "TRANSPARENT"
    assert by_uid["solid"].transparency == "OPAQUE"


def test_the_operators_own_reply_is_read_and_no_one_elses():
    """Reading somebody else's PARTSTAT would report their calendar, not ours."""
    invite = event(
        "invite",
        extra=(
            "ATTENDEE;PARTSTAT=ACCEPTED;CN=Someone Else:mailto:other@yandex.ru\r\n"
            "ATTENDEE;PARTSTAT=TENTATIVE;CN=Me:mailto:me@yandex.ru\r\n"
        ),
    )
    expansion = expand([document(invite)], start=START, end=END, operator="me@yandex.ru")
    (only,) = expansion.occurrences
    assert only.participation_status == "TENTATIVE"


def test_a_bare_login_still_matches_the_operators_invitation():
    """A profile login is routinely `me`, not `me@yandex.ru`.

    The domain it is completed with comes from the account this client is
    connected to, never from a literal in this module.
    """
    invite = event(
        "invite",
        extra="ATTENDEE;PARTSTAT=DECLINED:mailto:me@yandex.ru\r\n",
    )
    expansion = expand(
        [document(invite)],
        start=START,
        end=END,
        operator="me",
        operator_domains=("yandex.ru",),
    )
    (only,) = expansion.occurrences
    assert only.participation_status == "DECLINED"


def test_a_strangers_reply_on_another_domain_is_never_read_as_the_operators():
    """A local-part match across domains hands a stranger the power to free the hour.

    With a bare profile login `me`, `ATTENDEE:mailto:me@othercorp.com` is a
    different person entirely. Reading their DECLINED as ours would delete a
    real busy interval from the answer.
    """
    invite = event(
        "invite",
        extra="ATTENDEE;PARTSTAT=DECLINED:mailto:me@othercorp.com\r\n",
    )
    expansion = expand([document(invite)], start=START, end=END, operator="me")
    (only,) = expansion.occurrences
    assert only.participation_status is None


def test_a_custom_domain_account_still_finds_its_own_reply():
    """A 360 account is not on yandex.ru; a hard-coded domain matches nobody there.

    Every invitation would then read as unanswered -- or, worse, every declined
    one as busy.
    """
    invite = event(
        "invite",
        extra="ATTENDEE;PARTSTAT=DECLINED:mailto:me@othercorp.com\r\n",
    )
    expansion = expand(
        [document(invite)],
        start=START,
        end=END,
        operator="me",
        operator_domains=("othercorp.com",),
    )
    (only,) = expansion.occurrences
    assert only.participation_status == "DECLINED"


def test_a_full_login_owns_only_its_own_domain():
    """`me@yandex.ru` must not claim the replies of `me@othercorp.com`."""
    invite = event(
        "invite",
        extra="ATTENDEE;PARTSTAT=DECLINED:mailto:me@othercorp.com\r\n",
    )
    expansion = expand(
        [document(invite)], start=START, end=END, operator="me@yandex.ru"
    )
    (only,) = expansion.occurrences
    assert only.participation_status is None


def test_a_malformed_meeting_that_began_before_the_range_is_counted_not_dropped():
    """The failure this module exists to prevent: a broken event read as free time.

    An event with no UID cannot be expanded, so it is reported as unreadable --
    but only if it could have landed in the window. Judged by its start alone,
    a meeting that began last night and runs into this morning is ruled out,
    and the range comes back `complete` with `unreadable = 0`.
    """
    broken = (
        "BEGIN:VEVENT\r\nSUMMARY:No identity\r\n"
        "DTSTART;TZID=Europe/Moscow:20260531T230000\r\n"
        "DTEND;TZID=Europe/Moscow:20260601T020000\r\nEND:VEVENT\r\n"
    )
    overlapping = expand([document(broken)], start=START, end=END, overlap=True)
    assert overlapping.unreadable == 1

    listing = expand([document(broken)], start=START, end=END)
    assert listing.unreadable == 0, "the listing contract is judged on the start"


def test_an_event_with_no_attendee_line_reports_no_reply_rather_than_a_guess():
    """An event you made yourself has no PARTSTAT; inventing one invents a fact."""
    expansion = expand(
        [document(event("mine"))], start=START, end=END, operator="me@yandex.ru"
    )
    (only,) = expansion.occurrences
    assert only.participation_status is None


def test_an_event_that_began_before_the_window_can_be_seen_at_all():
    """Without this the busy tool cannot know the range's first hour is taken."""
    crossing = event("crossing", start="20260531T230000", end="20260601T020000")
    inside = expand([document(crossing)], start=START, end=END)
    assert inside.occurrences == (), "the listing contract must not change"

    overlapping = expand([document(crossing)], start=START, end=END, overlap=True)
    assert [o.uid for o in overlapping.occurrences] == ["crossing"]


# -- the tool -------------------------------------------------------------


def test_each_accepted_meeting_produces_one_busy_interval():
    client = FakeClient(
        [
            occurrence("a", moment(2, 10), moment(2, 11)),
            occurrence("b", moment(4, 15), moment(4, 16), participation_status="ACCEPTED"),
        ]
    )
    page = call(build(client))
    assert [(i.start, i.end, i.kind) for i in page.items] == [
        (moment(2, 10), moment(2, 11), BUSY),
        (moment(4, 15), moment(4, 16), BUSY),
    ]
    assert page.complete is True
    assert page.next_cursor is None


def test_meetings_that_touch_end_to_start_become_one_interval():
    """Two reported intervals with no gap read as a gap that is not there."""
    client = FakeClient(
        [
            occurrence("a", moment(2, 10), moment(2, 11)),
            occurrence("b", moment(2, 11), moment(2, 12)),
        ]
    )
    page = call(build(client))
    assert [(i.start, i.end) for i in page.items] == [(moment(2, 10), moment(2, 12))]


def test_overlapping_meetings_become_one_interval_spanning_both():
    client = FakeClient(
        [
            occurrence("a", moment(2, 10), moment(2, 12)),
            occurrence("b", moment(2, 11), moment(2, 13)),
        ]
    )
    page = call(build(client))
    assert [(i.start, i.end) for i in page.items] == [(moment(2, 10), moment(2, 13))]


def test_a_transparent_event_consumes_no_time():
    client = FakeClient(
        [occurrence("a", moment(2, 10), moment(2, 11), transparency="TRANSPARENT")]
    )
    page = call(build(client))
    assert page.items == []
    assert page.complete is True


def test_a_declined_invitation_consumes_no_time():
    client = FakeClient(
        [occurrence("a", moment(2, 10), moment(2, 11), participation_status="DECLINED")]
    )
    assert call(build(client)).items == []


def test_a_tentative_invitation_is_its_own_kind_and_never_merges_into_certainty():
    """A quarter of this account is unanswered or tentative; collapsing it lies."""
    client = FakeClient(
        [
            occurrence("sure", moment(2, 10), moment(2, 11)),
            occurrence(
                "maybe", moment(2, 11), moment(2, 12), participation_status="TENTATIVE"
            ),
        ]
    )
    page = call(build(client))
    kinds = {(i.kind, i.start, i.end) for i in page.items}
    assert (BUSY, moment(2, 10), moment(2, 11)) in kinds
    assert (BUSY_TENTATIVE, moment(2, 11), moment(2, 12)) in kinds
    assert len(page.items) == 2


def test_an_unanswered_invitation_is_distinguishable_from_a_tentative_one():
    client = FakeClient(
        [
            occurrence(
                "maybe", moment(2, 10), moment(2, 11), participation_status="TENTATIVE"
            ),
            occurrence(
                "silent",
                moment(2, 11),
                moment(2, 12),
                participation_status="NEEDS-ACTION",
            ),
        ]
    )
    page = call(build(client))
    kinds = [i.kind for i in page.items]
    assert BUSY_TENTATIVE in kinds
    assert BUSY_UNANSWERED in kinds
    assert BUSY_TENTATIVE != BUSY_UNANSWERED
    assert len(page.items) == 2


def test_two_tentative_meetings_that_touch_do_merge_with_each_other():
    """Kinds must not merge into each other -- but a kind must merge with itself."""
    client = FakeClient(
        [
            occurrence("a", moment(2, 10), moment(2, 11), participation_status="TENTATIVE"),
            occurrence("b", moment(2, 11), moment(2, 12), participation_status="TENTATIVE"),
        ]
    )
    page = call(build(client))
    assert [(i.kind, i.start, i.end) for i in page.items] == [
        (BUSY_TENTATIVE, moment(2, 10), moment(2, 12))
    ]


def test_a_delegated_invitation_is_not_this_accounts_time():
    """DELEGATED is the one reply that says the meeting is now somebody else's.

    Falling through to plain busy would block an hour this account handed over.
    """
    client = FakeClient(
        [occurrence("a", moment(2, 10), moment(2, 11), participation_status="DELEGATED")]
    )
    assert call(build(client)).items == []


def test_a_reply_this_server_has_never_seen_is_still_busy():
    """The conservative reading never quietly frees an hour that may be taken."""
    client = FakeClient(
        [occurrence("a", moment(2, 10), moment(2, 11), participation_status="X-INVENTED")]
    )
    (only,) = call(build(client)).items
    assert only.kind == BUSY


def test_an_event_of_zero_length_produces_no_interval_at_all():
    """A zero-width interval reports no busy time; returning one invites a caller
    to treat an instant as an obstacle."""
    client = FakeClient([occurrence("a", moment(2, 10), moment(2, 10))])
    page = call(build(client))
    assert page.items == []
    assert page.unreadable == 0, "a zero-length event is odd, not unreadable"
    assert page.complete is True


def test_an_occurrence_that_ends_before_it_begins_is_counted_never_dropped():
    """Corrupt data that produces no interval must not vanish into a free hour."""
    client = FakeClient([occurrence("a", moment(2, 11), moment(2, 10))])
    page = call(build(client))
    assert page.items == []
    assert page.unreadable == 1
    assert page.complete is False
    assert UNREADABLE_DATA in page.incomplete_reasons


def test_an_event_spanning_the_whole_window_is_clipped_at_both_ends():
    client = FakeClient(
        [occurrence("conference", START - timedelta(days=1), END + timedelta(days=1))]
    )
    (only,) = call(build(client)).items
    assert (only.start, only.end) == (START, END)
    assert only.clipped_start is True
    assert only.clipped_end is True
    assert only.clipping_note == CLIPPED_BOTH_NOTE


def test_a_clipping_flag_survives_the_merge_that_absorbs_its_interval():
    """Otherwise busy time appears to stop at the range end while it continues.

    The absorbed occurrence ends exactly where the earlier one does -- at the
    edge -- so a merge that only carries flags forward when the end *grows*
    drops the one fact the caller needed.
    """
    client = FakeClient(
        [
            occurrence("ends-at-the-edge", moment(7, 22), END),
            occurrence("runs-past-it", moment(7, 23), END + timedelta(hours=2)),
        ]
    )
    (only,) = call(build(client)).items
    assert (only.start, only.end) == (moment(7, 22), END)
    assert only.clipped_end is True
    assert only.clipping_note == CLIPPED_END_NOTE


def test_merge_intervals_joins_only_within_one_kind():
    """Exercised directly: every other test reaches it through the whole tool."""
    tentative = BusyInterval(start=moment(2, 10), end=moment(2, 12), kind=BUSY_TENTATIVE)
    merged = merge_intervals(
        [
            BusyInterval(start=moment(2, 11), end=moment(2, 13), kind=BUSY),
            BusyInterval(start=moment(2, 10), end=moment(2, 11), kind=BUSY),
            tentative,
        ]
    )
    assert [(i.kind, i.start, i.end) for i in merged] == [
        (BUSY, moment(2, 10), moment(2, 13)),
        (BUSY_TENTATIVE, moment(2, 10), moment(2, 12)),
    ]


def test_an_empty_answer_with_an_unreadable_calendar_is_never_a_free_range():
    """The exact case this module exists to prevent being misread."""
    client = FakeClient([], unreadable_calendars=1)
    page = call(build(client))
    assert page.items == []
    assert page.unreadable_calendars == 1
    assert page.complete is False
    assert UNREADABLE_DATA in page.incomplete_reasons


def test_each_instance_of_a_series_gets_its_own_interval():
    client = FakeClient(
        [
            occurrence(
                "standup",
                moment(day, 9),
                moment(day, 9, 15),
                recurrence_id=moment(day, 9).isoformat(),
            )
            for day in (2, 3, 4)
        ]
    )
    page = call(build(client))
    assert len(page.items) == 3
    assert [i.start for i in page.items] == [moment(2, 9), moment(3, 9), moment(4, 9)]


def test_an_all_day_event_occupies_its_whole_day():
    """A date coerced to midnight UTC would block the wrong 24 hours."""
    client = FakeClient(
        [occurrence("off", date(2026, 6, 3), date(2026, 6, 4), all_day=True)]
    )
    page = call(build(client))
    assert [(i.start, i.end) for i in page.items] == [
        (datetime(2026, 6, 3, tzinfo=MOSCOW), datetime(2026, 6, 4, tzinfo=MOSCOW))
    ]


def test_an_all_day_event_with_no_end_still_occupies_a_whole_day():
    client = FakeClient(
        [occurrence("off", date(2026, 6, 3), date(2026, 6, 3), all_day=True)]
    )
    page = call(build(client))
    assert [(i.start, i.end) for i in page.items] == [
        (datetime(2026, 6, 3, tzinfo=MOSCOW), datetime(2026, 6, 4, tzinfo=MOSCOW))
    ]


def test_an_event_crossing_the_edge_is_clipped_and_says_so():
    """An interval starting at the edge is a different fact from a meeting that does."""
    client = FakeClient(
        [
            occurrence("before", moment(1, 0) - timedelta(hours=2), moment(1, 2)),
            occurrence("after", moment(7, 23), moment(8, 2)),
        ]
    )
    page = call(build(client))
    first, last = page.items
    assert first.start == START
    assert first.clipped_start is True
    assert first.clipped_end is False
    assert first.clipping_note
    assert last.end == END
    assert last.clipped_end is True
    assert last.clipped_start is False
    assert last.clipping_note


def test_an_interval_wholly_inside_the_range_never_claims_to_be_clipped():
    client = FakeClient([occurrence("a", moment(2, 10), moment(2, 11))])
    (only,) = call(build(client)).items
    assert only.clipped_start is False
    assert only.clipped_end is False
    assert only.clipping_note is None


def test_the_client_is_asked_for_events_that_merely_overlap_the_range():
    """Fetching only events that *start* inside would lose the range's first hour."""
    client = FakeClient()
    call(build(client))
    assert client.calls[0]["overlap"] is True


def test_an_empty_range_is_an_empty_complete_answer_and_not_an_error():
    page = call(build(FakeClient()))
    assert page.items == []
    assert page.complete is True
    assert page.next_cursor is None
    assert page.incomplete_reasons == []


def test_a_range_wider_than_the_maximum_is_refused_by_name_never_narrowed():
    client = FakeClient()
    tool = build(client)
    with pytest.raises(ProtocolError) as raised:
        call(tool, end=START + timedelta(days=MAX_RANGE_DAYS + 1))
    assert str(MAX_RANGE_DAYS) in str(raised.value)
    assert client.calls == [], "a refused range must never reach the server"


def test_an_inverted_range_is_refused_before_anything_is_sent():
    client = FakeClient()
    with pytest.raises(ProtocolError):
        call(build(client), start=END, end=START)
    assert client.calls == []


@pytest.mark.parametrize("field", ["start", "end"])
def test_a_range_without_an_offset_is_refused_before_anything_is_sent(field):
    client = FakeClient()
    naive = {field: datetime(2026, 6, 1) if field == "start" else datetime(2026, 6, 8)}
    with pytest.raises(ProtocolError) as raised:
        call(build(client), **naive)
    assert "offset" in str(raised.value).lower()
    assert client.calls == []


def test_a_range_is_required_and_never_guessed():
    tool = build(FakeClient())
    with pytest.raises(TypeError):
        anyio.run(lambda: tool(start=START))


def test_one_unreadable_calendar_leaves_the_others_answering_and_is_counted():
    """A plain empty answer would read as `you are free all week`."""
    client = FakeClient(
        [occurrence("a", moment(2, 10), moment(2, 11))], unreadable_calendars=1
    )
    page = call(build(client))
    assert len(page.items) == 1
    assert page.unreadable_calendars == 1
    assert page.complete is False
    assert page.incomplete_reasons == [UNREADABLE_DATA]


def test_an_unreadable_event_is_counted_and_reported_never_dropped_in_silence():
    client = FakeClient([occurrence("a", moment(2, 10), moment(2, 11))], unreadable=2)
    page = call(build(client))
    assert page.unreadable == 2
    assert page.complete is False
    assert page.incomplete_reasons == [UNREADABLE_DATA]


def test_a_truncated_expansion_is_reported_rather_than_passed_off_as_whole():
    client = FakeClient([occurrence("a", moment(2, 10), moment(2, 11))], truncated=True)
    page = call(build(client))
    assert page.complete is False
    assert page.incomplete_reasons == [RANGE_TRUNCATED]


def test_the_answer_carries_no_event_titles_descriptions_or_attendees():
    """This tool answers about time. A title here is a leak, not a convenience."""
    client = FakeClient(
        [
            occurrence(
                "a", moment(2, 10), moment(2, 11), summary="Quarterly compensation review"
            )
        ]
    )
    page = call(build(client))
    # Asserted over the model's own field names, not over the serialised page:
    # a page carries a base64 cursor, and a substring search across it fails --
    # or passes -- for reasons that have nothing to do with what is reported.
    fields = set(BusyInterval.model_fields) | set(FreeBusyPage.model_fields)
    assert not fields & {"summary", "title", "description", "attendees", "uid"}
    (interval,) = page.items
    assert "compensation" not in interval.model_dump_json().lower()


def test_a_page_cut_short_by_the_limit_carries_a_cursor_that_continues_it():
    client = FakeClient(
        [occurrence(f"e{day}", moment(day, 9), moment(day, 10)) for day in (2, 3, 4)]
    )
    tool = build(client)
    first = call(tool, limit=2)
    assert len(first.items) == 2
    assert first.complete is False
    assert first.incomplete_reasons == [MORE_PAGES]
    assert first.next_cursor

    second = call(tool, limit=2, cursor=first.next_cursor)
    assert [i.start for i in second.items] == [moment(4, 9)]
    assert second.complete is True
    assert second.next_cursor is None


def test_paging_continues_past_the_second_page_and_terminates():
    """A cursor that only ever works once leaves the tail of a range unreachable."""
    client = FakeClient(
        [occurrence(f"e{day}", moment(day, 9), moment(day, 10)) for day in (2, 3, 4, 5, 6)]
    )
    tool = build(client)
    seen = []
    cursor = None
    for _ in range(10):
        page = call(tool, limit=2, cursor=cursor)
        seen.extend(i.start for i in page.items)
        cursor = page.next_cursor
        if page.complete:
            break
    else:  # pragma: no cover - only reached if paging never terminates
        pytest.fail("paging never reported a complete page")
    assert cursor is None
    assert seen == [moment(day, 9) for day in (2, 3, 4, 5, 6)]


def test_a_page_cut_short_by_the_limit_still_says_the_range_was_truncated():
    """Told only `more_pages`, a caller that stops paging never learns that
    intervals are missing from the later part of the range."""
    client = FakeClient(
        [occurrence(f"e{day}", moment(day, 9), moment(day, 10)) for day in (2, 3, 4)],
        truncated=True,
        unreadable=1,
    )
    page = call(build(client), limit=2)
    assert page.incomplete_reasons == [MORE_PAGES, RANGE_TRUNCATED, UNREADABLE_DATA]


def test_the_permitted_kinds_and_reasons_reach_the_json_schema():
    """A plain `str` tells an MCP client nothing about what it may receive."""
    schema = FreeBusyPage.model_json_schema()
    definitions = schema.get("$defs", {})
    kind = definitions["BusyInterval"]["properties"]["kind"]
    assert set(kind.get("enum", [])) == set(KINDS)
    reasons = schema["properties"]["incomplete_reasons"]["items"]
    assert set(reasons.get("enum", [])) == {MORE_PAGES, RANGE_TRUNCATED, UNREADABLE_DATA}


def test_a_cursor_issued_for_a_different_range_is_refused_not_quietly_honoured():
    client = FakeClient(
        [occurrence(f"e{day}", moment(day, 9), moment(day, 10)) for day in (2, 3, 4)]
    )
    tool = build(client)
    first = call(tool, limit=2)
    with pytest.raises(ProtocolError):
        call(tool, limit=2, cursor=first.next_cursor, end=END + timedelta(days=1))


def test_another_tools_cursor_is_refused():
    with pytest.raises(ProtocolError):
        call(build(FakeClient()), cursor=encode_cursor({"after": {}}, tool="something_else"))


@pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1, "10", True])
def test_a_limit_outside_the_documented_range_is_refused(limit):
    client = FakeClient()
    with pytest.raises(ProtocolError):
        call(build(client), limit=limit)
    assert client.calls == []


def test_the_default_limit_is_the_documented_one():
    assert 1 <= DEFAULT_LIMIT <= MAX_LIMIT


def test_a_named_calendar_is_the_only_one_asked():
    client = FakeClient()
    call(build(client), calendar_url=CALENDAR)
    assert client.calls[0]["calendar_url"] == CALENDAR


def test_the_tool_is_registered_read_only():
    """An unregistered tool cannot be served at all; a mislabelled one lies."""
    assert RISK_REGISTRY[TOOL_NAME] is RiskClass.READ
