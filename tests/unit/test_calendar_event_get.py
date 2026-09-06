"""`calendar_event_get`: one event, in full, addressed rather than searched for.

Every test here is named for the harm it prevents. The recurring themes:

* A UID is *addressed*, never searched for. Yandex's UID search answers with the
  whole calendar while looking like a filtered query, so a search-based lookup
  would confidently return the wrong meeting. The fakes record every search, and
  the tests assert none happened.
* An ETag has three spellings on the real account and only one is usable: the
  DAV property. The cached attribute is empty and the GET header carries a
  `--gzip` suffix. Story 1.7 sends this value back as a precondition, so reading
  the wrong one would make a concurrency check fail on events nobody touched.
* A miss is only a miss when the whole search succeeded. If a calendar errored
  while the account was scanned, the event may be in the one that failed.

The client is faked, so nothing here opens a socket.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timedelta, timezone

import anyio
import pytest
from caldav.lib import error as caldav_error
from conftest import FakeCalendar, FakeObject, install_fake_dav_client
from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient
from yandex_calendar_mcp.client.recurrence import EventRecord
from yandex_calendar_mcp.tools.events import (
    GET_TOOL_NAME,
    MAX_DESCRIPTION_CHARS,
    SCOPE_OCCURRENCE,
    SCOPE_SERIES,
    SCOPE_SINGLE,
    EventDetail,
    build_calendar_event_get,
    build_calendar_events_list,
)
from yandex_core.errors import (
    AuthError,
    NotFound,
    PolicyError,
    ProtocolError,
    TransportError,
)

# The client falls back to `requests` when `niquests` is absent; importing the
# other way round here would fail collection on an install where the code works.
try:  # pragma: no cover - import shape depends on the installed caldav
    from niquests import exceptions as http_error
except ImportError:  # pragma: no cover
    from requests import exceptions as http_error  # type: ignore[no-redef]

MOSCOW = timezone(timedelta(hours=3))
URL = "https://caldav.example"
PERSONAL = f"{URL}/calendars/me/personal/"
WORK = f"{URL}/calendars/me/work/"


def document(*events: str) -> str:
    body = "".join(events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{body}END:VCALENDAR\r\n"


STANDALONE = document(
    "BEGIN:VEVENT\r\n"
    "UID:design-review\r\n"
    "SUMMARY:Design review\r\n"
    "DESCRIPTION:Bring the sketches.\r\n"
    "LOCATION:Room 4\r\n"
    "ORGANIZER;CN=Tatyana Startseva:mailto:tatyana@example.com\r\n"
    "ATTENDEE;PARTSTAT=ACCEPTED;CN=Aleksandr Denisov;ROLE=REQ-PARTICIPANT:"
    "mailto:aleksandr@example.com\r\n"
    "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:nameless@example.com\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T100000\r\n"
    "END:VEVENT\r\n"
)

#: A daily series with one instance moved and one instance cancelled outright.
SERIES = document(
    "BEGIN:VEVENT\r\n"
    "UID:standup\r\n"
    "SUMMARY:Standup\r\n"
    "DESCRIPTION:Fifteen minutes.\r\n"
    "LOCATION:Room 1\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T091500\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "EXDATE;TZID=Europe/Moscow:20260610T090000\r\n"
    "END:VEVENT\r\n",
    "BEGIN:VEVENT\r\n"
    "UID:standup\r\n"
    "RECURRENCE-ID;TZID=Europe/Moscow:20260609T090000\r\n"
    "SUMMARY:Standup (moved)\r\n"
    "LOCATION:Room 9\r\n"
    "DTSTART;TZID=Europe/Moscow:20260609T113000\r\n"
    "DTEND;TZID=Europe/Moscow:20260609T120000\r\n"
    "END:VEVENT\r\n",
    "BEGIN:VEVENT\r\n"
    "UID:standup\r\n"
    "RECURRENCE-ID;TZID=Europe/Moscow:20260611T090000\r\n"
    "SUMMARY:Standup\r\n"
    "STATUS:CANCELLED\r\n"
    "DTSTART;TZID=Europe/Moscow:20260611T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260611T091500\r\n"
    "END:VEVENT\r\n",
)

ALL_DAY = document(
    "BEGIN:VEVENT\r\n"
    "UID:day-off\r\n"
    "SUMMARY:Day off\r\n"
    "DTSTART;VALUE=DATE:20260612\r\n"
    "DTEND;VALUE=DATE:20260613\r\n"
    "END:VEVENT\r\n"
)

MOVED = "2026-06-09T09:00:00+03:00"
EXCLUDED = "2026-06-10T09:00:00+03:00"
CANCELLED_OVERRIDE = "2026-06-11T09:00:00+03:00"
PLAIN_INSTANCE = "2026-06-12T09:00:00+03:00"


def make_tool(monkeypatch, calendars):
    """The real client, the real parsing, a fake socket."""
    install_fake_dav_client(monkeypatch, calendars=calendars)

    async def provider() -> CalDAVCalendarClient:
        return CalDAVCalendarClient(url=URL, username="me@example.com", password="pw")

    return build_calendar_event_get(provider)


def make_tools(monkeypatch, calendars):
    """Both event tools over one fake account, so they can be compared."""
    install_fake_dav_client(monkeypatch, calendars=calendars)

    async def provider() -> CalDAVCalendarClient:
        return CalDAVCalendarClient(url=URL, username="me@example.com", password="pw")

    return build_calendar_event_get(provider), build_calendar_events_list(provider)


def call(tool, **kwargs):
    return anyio.run(lambda: tool(**kwargs))


# -- the detail a listing leaves out -------------------------------------


def test_a_standalone_event_returns_what_a_listing_could_not_say(monkeypatch):
    """Without this the caller can see a meeting exists but not what it is."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    detail = call(tool, uid="design-review")

    assert detail.uid == "design-review"
    assert detail.summary == "Design review"
    assert detail.description == "Bring the sketches."
    assert detail.location == "Room 4"
    assert detail.organizer.email == "tatyana@example.com"
    assert detail.organizer.name == "Tatyana Startseva"
    assert [a.email for a in detail.attendees] == [
        "aleksandr@example.com",
        "nameless@example.com",
    ]
    assert detail.attendees[0].response_status == "ACCEPTED"
    assert detail.attendees[1].response_status == "NEEDS-ACTION"
    assert detail.start == datetime(2026, 6, 8, 9, tzinfo=MOSCOW)
    assert detail.end == datetime(2026, 6, 8, 10, tzinfo=MOSCOW)
    assert detail.etag == "etag-design-review"
    assert detail.scope == SCOPE_SINGLE
    assert detail.recurrence_id is None
    assert detail.cancelled is False
    assert detail.calendar_url == PERSONAL
    assert detail.calendar_name == "Personal"


def test_an_attendee_without_a_name_keeps_its_address_and_says_the_name_is_absent(
    monkeypatch,
):
    """An invented name is worse than none; a dropped address is worse still."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    nameless = call(tool, uid="design-review").attendees[1]
    assert nameless.email == "nameless@example.com"
    assert nameless.name is None


def test_an_all_day_event_stays_a_date_and_is_never_midnight_somewhere(monkeypatch):
    """Coercing a date to midnight moves the day for every reader at an offset."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [ALL_DAY])])
    detail = call(tool, uid="day-off")
    assert detail.all_day is True
    assert detail.start == date(2026, 6, 12)
    assert not isinstance(detail.start, datetime)
    assert not isinstance(detail.end, datetime)


# -- series, instances, overrides, cancellations --------------------------


def test_a_series_asked_for_without_an_instance_says_it_is_the_series(monkeypatch):
    """Silently answering with instance one would misreport the whole series."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    detail = call(tool, uid="standup")
    assert detail.scope == SCOPE_SERIES
    assert detail.is_series is True
    assert detail.recurrence_id is None
    assert detail.summary == "Standup"


def test_one_instance_is_returned_with_its_own_start_and_end(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    detail = call(tool, uid="standup", recurrence_id=PLAIN_INSTANCE)
    assert detail.scope == SCOPE_OCCURRENCE
    assert detail.recurrence_id == PLAIN_INSTANCE
    assert detail.start == datetime(2026, 6, 12, 9, tzinfo=MOSCOW)
    assert detail.end == datetime(2026, 6, 12, 9, 15, tzinfo=MOSCOW)
    assert detail.cancelled is False


def test_a_modified_instance_returns_the_override_not_the_series_defaults(monkeypatch):
    """Returning the series' 09:00 for a meeting moved to 11:30 sends people late."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    detail = call(tool, uid="standup", recurrence_id=MOVED)
    assert detail.summary == "Standup (moved)"
    assert detail.location == "Room 9"
    assert detail.start == datetime(2026, 6, 9, 11, 30, tzinfo=MOSCOW)
    assert detail.end == datetime(2026, 6, 9, 12, tzinfo=MOSCOW)
    assert detail.recurrence_id == MOVED


def test_an_excluded_instance_is_reported_cancelled_not_as_a_live_meeting(monkeypatch):
    """An EXDATE instance is not happening; returning it as live sends someone."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    detail = call(tool, uid="standup", recurrence_id=EXCLUDED)
    assert detail.cancelled is True
    assert detail.status == "CANCELLED"
    assert detail.recurrence_id == EXCLUDED


def test_an_instance_cancelled_by_an_override_is_reported_cancelled(monkeypatch):
    """The other spelling of a cancellation must read the same to the caller."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    detail = call(tool, uid="standup", recurrence_id=CANCELLED_OVERRIDE)
    assert detail.cancelled is True
    assert detail.status == "CANCELLED"


# -- misses that must never be dressed up ---------------------------------


def test_an_unknown_uid_is_a_not_found_naming_it_never_an_empty_success(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    with pytest.raises(NotFound) as caught:
        call(tool, uid="no-such-event")
    assert "no-such-event" in str(caught.value)


def test_an_unknown_instance_is_distinguishable_from_an_unknown_uid(monkeypatch):
    """"The series is not there" and "that day is not in it" need different fixes."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    with pytest.raises(NotFound) as caught:
        call(tool, uid="standup", recurrence_id="2027-01-01T09:00:00+03:00")
    message = str(caught.value)
    assert "standup" in message
    assert "2027-01-01T09:00:00+03:00" in message

    with pytest.raises(NotFound) as missing_uid:
        call(tool, uid="no-such-event", recurrence_id="2027-01-01T09:00:00+03:00")
    assert str(missing_uid.value) != message


def test_a_recurrence_id_on_a_one_off_event_is_a_not_found_not_the_event(monkeypatch):
    """Ignoring the instance and answering with the event would hide the mistake."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    with pytest.raises(NotFound) as caught:
        call(tool, uid="design-review", recurrence_id=MOVED)
    assert "design-review" in str(caught.value)


def test_a_missing_uid_lookup_never_falls_back_to_searching_for_it(monkeypatch):
    """A UID search on Yandex returns the whole calendar and looks like a hit."""
    calendars = [FakeCalendar("Personal", PERSONAL, [STANDALONE])]
    tool = make_tool(monkeypatch, calendars)
    with pytest.raises(NotFound):
        call(tool, uid="no-such-event")
    assert calendars[0].searched is None, "the client searched for a UID"


def test_a_found_event_was_addressed_by_url_and_never_searched_for(monkeypatch):
    calendars = [FakeCalendar("Personal", PERSONAL, [STANDALONE])]
    tool = make_tool(monkeypatch, calendars)
    call(tool, uid="design-review")
    assert calendars[0].searched is None
    assert calendars[0].fetched == [f"{PERSONAL}design-review.ics"]


# -- which calendars are addressed ----------------------------------------


def test_a_calendar_hint_addresses_only_that_calendar(monkeypatch):
    personal = FakeCalendar("Personal", PERSONAL, [STANDALONE])
    work = FakeCalendar("Work", WORK, [SERIES])
    tool = make_tool(monkeypatch, [personal, work])
    detail = call(tool, uid="standup", calendar_url=WORK)
    assert detail.calendar_name == "Work"
    assert personal.fetched == []


def test_an_unknown_calendar_url_is_a_not_found(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    with pytest.raises(NotFound):
        call(tool, uid="design-review", calendar_url=f"{URL}/calendars/me/gone/")


def test_without_a_hint_every_calendar_is_tried_before_a_miss(monkeypatch):
    personal = FakeCalendar("Personal", PERSONAL, [STANDALONE])
    work = FakeCalendar("Work", WORK, [SERIES])
    tool = make_tool(monkeypatch, [personal, work])
    with pytest.raises(NotFound):
        call(tool, uid="nowhere")
    assert personal.fetched and work.fetched


def test_a_later_calendar_is_still_tried_after_an_earlier_one_missed(monkeypatch):
    personal = FakeCalendar("Personal", PERSONAL, [STANDALONE])
    work = FakeCalendar("Work", WORK, [SERIES])
    tool = make_tool(monkeypatch, [personal, work])
    detail = call(tool, uid="standup")
    assert detail.calendar_name == "Work"


def test_a_miss_after_an_unreachable_calendar_says_the_search_was_incomplete(
    monkeypatch,
):
    """The event may be in the calendar that failed; a plain miss asserts it is not."""
    broken = FakeCalendar(
        "Shared",
        WORK,
        fetch_raises=caldav_error.AuthorizationError(url=WORK, reason="Forbidden"),
    )
    personal = FakeCalendar("Personal", PERSONAL, [STANDALONE])
    tool = make_tool(monkeypatch, [personal, broken])
    with pytest.raises(NotFound) as caught:
        call(tool, uid="nowhere")
    message = str(caught.value)
    assert "incomplete" in message.lower()
    assert "nowhere" in message


def test_an_unreachable_calendar_does_not_stop_the_others_from_answering(monkeypatch):
    broken = FakeCalendar(
        "Shared",
        f"{URL}/calendars/me/shared/",
        fetch_raises=caldav_error.AuthorizationError(url=WORK, reason="Forbidden"),
    )
    personal = FakeCalendar("Personal", PERSONAL, [STANDALONE])
    tool = make_tool(monkeypatch, [broken, personal])
    detail = call(tool, uid="design-review")
    assert detail.summary == "Design review"


def test_a_transport_failure_is_not_reported_as_a_missing_event(monkeypatch):
    """"Not found" would send the caller looking for an event that is really there."""
    broken = FakeCalendar(
        "Personal", PERSONAL, fetch_raises=http_error.ConnectionError("no route")
    )
    tool = make_tool(monkeypatch, [broken])
    with pytest.raises(TransportError):
        call(tool, uid="design-review")


# -- the ETag, which story 1.7 will send back -----------------------------


def test_the_etag_is_the_property_not_the_header_and_not_the_cached_attribute(
    monkeypatch,
):
    """Mixing the spellings makes 1.7's precondition fail on untouched events."""
    calendar = FakeCalendar(
        "Personal",
        PERSONAL,
        [STANDALONE],
        etags={"design-review": "1788415102079"},
        cached_etags={"design-review": None},
        header_etags={"design-review": "1788415102079--gzip"},
    )
    tool = make_tool(monkeypatch, [calendar])
    assert call(tool, uid="design-review").etag == "1788415102079"


def test_an_absent_etag_is_null_and_the_answer_says_so(monkeypatch):
    """An invented ETag would make a later conditional update overwrite an edit."""
    calendar = FakeCalendar(
        "Personal", PERSONAL, [STANDALONE], etags={"design-review": None}
    )
    tool = make_tool(monkeypatch, [calendar])
    detail = call(tool, uid="design-review")
    assert detail.etag is None
    assert detail.etag_note
    assert "etag" in detail.etag_note.lower()


def test_a_blank_etag_is_treated_as_absent_rather_than_passed_on(monkeypatch):
    calendar = FakeCalendar(
        "Personal", PERSONAL, [STANDALONE], etags={"design-review": "  "}
    )
    tool = make_tool(monkeypatch, [calendar])
    assert call(tool, uid="design-review").etag is None


def test_an_etag_present_carries_no_note(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    assert call(tool, uid="design-review").etag_note is None


# -- validation, before anything is sent ----------------------------------


@pytest.mark.parametrize("uid", ["", "   "])
def test_a_blank_uid_is_refused_before_any_request(monkeypatch, uid):
    calendars = [FakeCalendar("Personal", PERSONAL, [STANDALONE])]
    tool = make_tool(monkeypatch, calendars)
    with pytest.raises(ProtocolError):
        call(tool, uid=uid)
    assert calendars[0].fetched == []


def test_a_blank_recurrence_id_is_refused_rather_than_read_as_the_series(monkeypatch):
    """Reading a blank as "the series" answers a question nobody asked."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    with pytest.raises(ProtocolError):
        call(tool, uid="standup", recurrence_id="   ")


def test_a_naive_recurrence_id_is_refused_before_any_request(monkeypatch):
    """A moment with no offset means a different instance to every reader."""
    calendars = [FakeCalendar("Personal", PERSONAL, [SERIES])]
    tool = make_tool(monkeypatch, calendars)
    with pytest.raises(ProtocolError):
        call(tool, uid="standup", recurrence_id="2026-06-09T09:00:00")
    assert calendars[0].fetched == []


def test_an_unparseable_recurrence_id_is_refused_by_name(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    with pytest.raises(ProtocolError) as caught:
        call(tool, uid="standup", recurrence_id="tuesday")
    assert "recurrence_id" in str(caught.value)


def test_the_tool_is_named_for_the_registry(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    assert tool.__name__ == GET_TOOL_NAME == "calendar_event_get"


# -- an unreadable document is not a missing event ------------------------


def test_an_unparseable_document_is_not_reported_as_a_missing_event(monkeypatch):
    """"Not found" would be a claim about the account; the fault is in the data."""
    calendar = FakeCalendar("Personal", PERSONAL, ["UID:broken\r\nnot an icalendar"])
    tool = make_tool(monkeypatch, [calendar])
    with pytest.raises(ProtocolError) as caught:
        call(tool, uid="broken")
    assert not isinstance(caught.value, NotFound)


# -- DURATION: the two tools must not disagree about when a meeting ends ---

#: The other legal way to say how long a meeting is. Yandex writes it.
DURATION_ONLY = document(
    "BEGIN:VEVENT\r\n"
    "UID:duration-only\r\n"
    "SUMMARY:Sprint sync\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DURATION:PT45M\r\n"
    "END:VEVENT\r\n"
)

RANGE = {"start": "2026-06-01T00:00:00+03:00", "end": "2026-06-30T00:00:00+03:00"}


def test_an_event_timed_by_duration_is_not_reported_as_zero_length(monkeypatch):
    """A 45-minute meeting reported as instantaneous reads as a free slot."""
    get_tool, _ = make_tools(
        monkeypatch, [FakeCalendar("Personal", PERSONAL, [DURATION_ONLY])]
    )
    detail = call(get_tool, uid="duration-only")
    assert detail.start == datetime(2026, 6, 8, 9, tzinfo=MOSCOW)
    assert detail.end == datetime(2026, 6, 8, 9, 45, tzinfo=MOSCOW)


def test_both_tools_report_the_same_end_for_one_meeting_timed_by_duration(monkeypatch):
    """Two tools disagreeing about one meeting's end is worse than either error."""
    get_tool, list_tool = make_tools(
        monkeypatch, [FakeCalendar("Personal", PERSONAL, [DURATION_ONLY])]
    )
    detail = call(get_tool, uid="duration-only")
    page = call(list_tool, **RANGE)
    (occurrence,) = [item for item in page.items if item.uid == "duration-only"]
    assert detail.end == occurrence.end
    assert detail.start == occurrence.start


# -- a series stored as more than one CalDAV object -----------------------

SPLIT_MASTER = document(
    "BEGIN:VEVENT\r\n"
    "UID:standup\r\n"
    "SUMMARY:Standup\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T091500\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "END:VEVENT\r\n"
)

SPLIT_OVERRIDE = document(
    "BEGIN:VEVENT\r\n"
    "UID:standup\r\n"
    "RECURRENCE-ID;TZID=Europe/Moscow:20260609T090000\r\n"
    "SUMMARY:Standup (moved)\r\n"
    "LOCATION:Room 9\r\n"
    "DTSTART;TZID=Europe/Moscow:20260609T113000\r\n"
    "DTEND;TZID=Europe/Moscow:20260609T120000\r\n"
    "END:VEVENT\r\n"
)


def split_series_calendar():
    """The override at the addressed href, the series' own object elsewhere."""
    return FakeCalendar(
        "Personal",
        PERSONAL,
        [
            (f"{PERSONAL}standup-master.ics", SPLIT_MASTER),
            (f"{PERSONAL}standup.ics", SPLIT_OVERRIDE),
        ],
    )


def test_an_instance_that_exists_is_not_a_not_found_because_of_where_it_is_stored(
    monkeypatch,
):
    """One document holding only an override must not hide the rest of the series."""
    tool = make_tool(monkeypatch, [split_series_calendar()])
    detail = call(tool, uid="standup", recurrence_id=PLAIN_INSTANCE)
    assert detail.scope == SCOPE_OCCURRENCE
    assert detail.start == datetime(2026, 6, 12, 9, tzinfo=MOSCOW)


def test_a_moved_instance_of_a_split_series_keeps_its_moved_time(monkeypatch):
    """Answering with the series' 09:00 for a meeting moved to 11:30 sends people late."""
    tool = make_tool(monkeypatch, [split_series_calendar()])
    detail = call(tool, uid="standup", recurrence_id=MOVED)
    assert detail.summary == "Standup (moved)"
    assert detail.start == datetime(2026, 6, 9, 11, 30, tzinfo=MOSCOW)


# -- an ETag that cannot be read is not a missing event -------------------


def test_an_event_whose_etag_cannot_be_read_is_still_returned(monkeypatch):
    """A failed PROPFIND told the caller their meeting did not exist."""
    calendar = FakeCalendar(
        "Personal",
        PERSONAL,
        [STANDALONE],
        property_error=RuntimeError("PROPFIND blew up"),
    )
    tool = make_tool(monkeypatch, [calendar])
    detail = call(tool, uid="design-review")
    assert detail.summary == "Design review"
    assert detail.etag is None
    assert detail.etag_note and "could not be read" in detail.etag_note.lower()


def test_an_unreadable_etag_is_not_described_as_one_the_server_never_sent(monkeypatch):
    """"None supplied" and "we could not read it" call for different next steps."""
    unreadable = FakeCalendar(
        "Personal", PERSONAL, [STANDALONE], property_error=RuntimeError("boom")
    )
    absent = FakeCalendar(
        "Personal", PERSONAL, [STANDALONE], etags={"design-review": None}
    )
    tool = make_tool(monkeypatch, [unreadable])
    first = call(tool, uid="design-review").etag_note
    tool = make_tool(monkeypatch, [absent])
    second = call(tool, uid="design-review").etag_note
    assert first and second and first != second


def test_a_caldav_without_use_cached_does_not_make_the_event_vanish(monkeypatch):
    """An older library signature must not read as "that event does not exist"."""
    calendar = FakeCalendar(
        "Personal", PERSONAL, [STANDALONE], legacy_property=True
    )
    tool = make_tool(monkeypatch, [calendar])
    assert call(tool, uid="design-review").etag == "etag-design-review"


# -- a heap of overrides is not a series ----------------------------------

ORPHAN_OVERRIDE = document(
    "BEGIN:VEVENT\r\n"
    "UID:orphan\r\n"
    "RECURRENCE-ID;TZID=Europe/Moscow:20260609T090000\r\n"
    "SUMMARY:Orphaned instance\r\n"
    "LOCATION:Room 9\r\n"
    "DTSTART;TZID=Europe/Moscow:20260609T113000\r\n"
    "DTEND;TZID=Europe/Moscow:20260609T120000\r\n"
    "END:VEVENT\r\n"
)


def test_a_document_of_overrides_alone_is_not_passed_off_as_the_series(monkeypatch):
    """One instance's time reported as the series' own is a plausible wrong answer."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [ORPHAN_OVERRIDE])])
    with pytest.raises(ProtocolError) as caught:
        call(tool, uid="orphan")
    assert not isinstance(caught.value, NotFound)
    assert "series" in str(caught.value).lower()


# -- EXDATE obeys the same rule about offsets as everything else ----------

FLOATING_EXDATE = document(
    "BEGIN:VEVENT\r\n"
    "UID:floating-exdate\r\n"
    "SUMMARY:Standup\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T091500\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "EXDATE:20260610T090000\r\n"
    "END:VEVENT\r\n"
)


def test_a_floating_exdate_never_yields_a_start_the_tool_would_itself_refuse(
    monkeypatch,
):
    """A naive start here is a value this tool's own validator rejects on the way in."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [FLOATING_EXDATE])])
    with pytest.raises(ProtocolError):
        call(tool, uid="floating-exdate", recurrence_id="2026-06-10T09:00:00+00:00")


# -- one corrupt document must not end the scan ---------------------------


def test_a_corrupt_document_in_one_calendar_does_not_hide_the_event_in_another(
    monkeypatch,
):
    """The first calendar's bad data said nothing about the second calendar."""
    broken = FakeCalendar(
        "Broken",
        PERSONAL,
        [(f"{PERSONAL}design-review.ics", "UID:design-review\r\nnot an icalendar")],
    )
    good = FakeCalendar("Work", WORK, [STANDALONE])
    tool = make_tool(monkeypatch, [broken, good])
    detail = call(tool, uid="design-review")
    assert detail.calendar_name == "Work"


# -- a UID in two calendars, and which one answers ------------------------


def test_the_rule_for_a_uid_in_two_calendars_is_stated_not_left_to_be_found():
    """A silent choice between two real meetings is the worst kind of guess."""
    tool = build_calendar_event_get(lambda: None)
    doc = (tool.__doc__ or "").lower()
    assert "first" in doc and "calendar_url" in doc
    described = EventDetail.model_fields["calendar_url"].description.lower()
    assert "first" in described


def test_the_scan_stops_at_the_calendar_that_answered(monkeypatch):
    """Reading on past a hit costs a request and can only change the answer."""
    personal = FakeCalendar("Personal", PERSONAL, [STANDALONE])
    work = FakeCalendar("Work", WORK, [STANDALONE])
    tool = make_tool(monkeypatch, [personal, work])
    detail = call(tool, uid="design-review")
    assert detail.calendar_name == "Personal"
    assert work.fetched == []


# -- a URL that is not a calendar, and a calendar that refuses ------------


def test_a_calendar_url_that_names_no_calendar_is_not_reported_as_a_missing_event(
    monkeypatch,
):
    """"No such event" sends the caller hunting for a meeting that is really there."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    with pytest.raises(NotFound) as unlisted:
        call(tool, uid="no-such-event", calendar_url=f"{URL}/calendars/me/gone/")
    with pytest.raises(NotFound) as genuine:
        call(tool, uid="no-such-event", calendar_url=PERSONAL)
    # Same UID, same shape of question: only the calendar differs, so the two
    # messages may not be the same sentence with a different URL in it.
    assert str(unlisted.value) != str(genuine.value)
    assert "gone" in str(unlisted.value)
    assert "may not name a calendar" in str(unlisted.value).lower()
    assert "may not name a calendar" not in str(genuine.value).lower()


def test_a_forbidden_named_calendar_is_an_access_failure_not_a_missing_event(
    monkeypatch,
):
    """The only calendar asked about could not be opened; nothing was learned."""
    forbidden = FakeCalendar(
        "Shared",
        WORK,
        fetch_raises=caldav_error.AuthorizationError(url=WORK, reason="Forbidden"),
    )
    tool = make_tool(monkeypatch, [forbidden])
    with pytest.raises(PolicyError):
        call(tool, uid="design-review", calendar_url=WORK)


def test_a_rejected_password_is_an_auth_failure_not_a_missing_event(monkeypatch):
    """A wrong app password reported as a missing event hides the real fix."""
    rejected = FakeCalendar(
        "Personal",
        PERSONAL,
        fetch_raises=caldav_error.AuthorizationError(url=PERSONAL, reason="Unauthorized"),
    )
    tool = make_tool(monkeypatch, [rejected])
    with pytest.raises(AuthError):
        call(tool, uid="design-review")


# -- addressing, encoding, and the href the server actually used ----------

AT_UID = "meeting@yandex.ru"
AT_EVENT = document(
    "BEGIN:VEVENT\r\n"
    f"UID:{AT_UID}\r\n"
    "SUMMARY:Planning\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T100000\r\n"
    "END:VEVENT\r\n"
)


def test_an_event_stored_under_a_different_href_is_still_found(monkeypatch):
    """A UID with an `@` is routine here; a guessed encoding must not 404 it away."""
    calendar = FakeCalendar(
        "Personal", PERSONAL, [(f"{PERSONAL}{AT_UID}.ics", AT_EVENT)]
    )
    tool = make_tool(monkeypatch, [calendar])
    detail = call(tool, uid=AT_UID)
    assert detail.uid == AT_UID
    assert detail.summary == "Planning"
    assert calendar.searched is None


def test_an_href_landing_on_another_event_keeps_looking(monkeypatch):
    """The addressed href can hold somebody else's meeting; that is a miss, not a hit."""
    decoy = FakeCalendar(
        "Personal", PERSONAL, [(f"{PERSONAL}design-review.ics", SERIES)]
    )
    work = FakeCalendar("Work", WORK, [STANDALONE])
    tool = make_tool(monkeypatch, [decoy, work])
    detail = call(tool, uid="design-review")
    assert detail.calendar_name == "Work"
    assert detail.summary == "Design review"


# -- a superseded time is worse than an admitted failure ------------------

THIS_AND_FUTURE = document(
    "BEGIN:VEVENT\r\n"
    "UID:shifted\r\n"
    "SUMMARY:Standup\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T091500\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "END:VEVENT\r\n",
    "BEGIN:VEVENT\r\n"
    "UID:shifted\r\n"
    "RECURRENCE-ID;RANGE=THISANDFUTURE;TZID=Europe/Moscow:20260610T090000\r\n"
    "SUMMARY:Standup (later)\r\n"
    "DTSTART;TZID=Europe/Moscow:20260610T140000\r\n"
    "DTEND;TZID=Europe/Moscow:20260610T141500\r\n"
    "END:VEVENT\r\n",
)


def test_an_instance_after_a_this_and_future_override_is_not_answered_at_the_old_time(
    monkeypatch,
):
    """Every later instance moved; returning 09:00 sends the caller five hours early."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [THIS_AND_FUTURE])])
    with pytest.raises(ProtocolError) as caught:
        call(tool, uid="shifted", recurrence_id="2026-06-12T09:00:00+03:00")
    assert not isinstance(caught.value, NotFound)
    assert "thisandfuture" in str(caught.value).lower().replace("-", "")


# -- everything the invitation said, not merely the first line of it ------

TWO_ORGANIZERS = document(
    "BEGIN:VEVENT\r\n"
    "UID:two-chairs\r\n"
    "SUMMARY:Joint review\r\n"
    "ORGANIZER;CN=First Chair:mailto:first@example.com\r\n"
    "ORGANIZER;CN=Second Chair:mailto:second@example.com\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T100000\r\n"
    "END:VEVENT\r\n"
)


def test_a_second_organizer_is_reported_rather_than_dropped(monkeypatch):
    """Dropping one silently makes the answer look complete when it is not."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [TWO_ORGANIZERS])])
    detail = call(tool, uid="two-chairs")
    assert [person.email for person in detail.organizers] == [
        "first@example.com",
        "second@example.com",
    ]
    assert detail.organizer.email == "first@example.com"


WORDY = document(
    "BEGIN:VEVENT\r\n"
    "UID:wordy\r\n"
    "SUMMARY:Quoted mail\r\n"
    "DESCRIPTION:" + ("x" * 5000) + "\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T100000\r\n"
    "END:VEVENT\r\n"
)


def test_a_long_description_is_capped_and_the_truncation_declared(monkeypatch):
    """An unbounded body is a page of quoted mail nobody asked for, unannounced."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [WORDY])])
    detail = call(tool, uid="wordy")
    assert len(detail.description) == MAX_DESCRIPTION_CHARS
    assert detail.description_truncated is True


def test_a_short_description_is_not_reported_as_truncated(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    detail = call(tool, uid="design-review")
    assert detail.description == "Bring the sketches."
    assert detail.description_truncated is False


CONFERENCE = document(
    "BEGIN:VEVENT\r\n"
    "UID:telemost\r\n"
    "SUMMARY:Remote review\r\n"
    "CONFERENCE;VALUE=URI;FEATURE=VIDEO:https://telemost.yandex.ru/j/123\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T100000\r\n"
    "END:VEVENT\r\n"
)


def test_the_join_link_is_returned_rather_than_left_to_be_dug_out(monkeypatch):
    """A detail tool that omits the join link makes the caller parse the body."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [CONFERENCE])])
    assert call(tool, uid="telemost").join_url == "https://telemost.yandex.ru/j/123"


def test_a_series_says_how_it_recurs_rather_than_only_that_it_does(monkeypatch):
    """"is_series: true" with no rule leaves the caller unable to plan around it."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [SERIES])])
    detail = call(tool, uid="standup")
    assert detail.scope == SCOPE_SERIES
    assert detail.recurrence_summary
    assert "dai" in detail.recurrence_summary.lower()


def test_a_one_off_event_claims_no_recurrence(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [STANDALONE])])
    assert call(tool, uid="design-review").recurrence_summary is None


# -- an all-day series, addressed by date ---------------------------------

ALL_DAY_SERIES = document(
    "BEGIN:VEVENT\r\n"
    "UID:sprint-demo\r\n"
    "SUMMARY:Sprint demo\r\n"
    "DTSTART;VALUE=DATE:20260612\r\n"
    "DTEND;VALUE=DATE:20260613\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
    "END:VEVENT\r\n"
)


def test_an_all_day_series_instance_is_addressable_by_the_date_the_schema_promises(
    monkeypatch,
):
    """The schema tells callers to pass a plain date; it must actually work."""
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [ALL_DAY_SERIES])])
    detail = call(tool, uid="sprint-demo", recurrence_id="2026-06-19")
    assert detail.scope == SCOPE_OCCURRENCE
    assert detail.recurrence_id == "2026-06-19"
    assert detail.all_day is True
    assert detail.start == date(2026, 6, 19)
    assert not isinstance(detail.start, datetime)


# -- a document that is not a usable event --------------------------------

NO_START = document(
    "BEGIN:VEVENT\r\nUID:startless\r\nSUMMARY:No start at all\r\nEND:VEVENT\r\n"
)


def test_an_event_with_no_start_is_named_as_such_not_given_a_made_up_time(monkeypatch):
    tool = make_tool(monkeypatch, [FakeCalendar("Personal", PERSONAL, [NO_START])])
    with pytest.raises(ProtocolError) as caught:
        call(tool, uid="startless")
    assert not isinstance(caught.value, NotFound)
    assert "start" in str(caught.value).lower()


# -- the mapping between the record and the answer ------------------------


def test_every_field_of_the_record_reaches_the_tool_output():
    """A hand-written mapping loses a new field silently; this is the tripwire."""
    record_fields = {field.name for field in dataclass_fields(EventRecord)}
    missing = record_fields - set(EventDetail.model_fields)
    assert not missing, f"EventRecord fields never reach the caller: {sorted(missing)}"


# -- the fakes must not validate the code against themselves --------------


def test_asking_for_a_different_dav_property_never_yields_the_etag():
    """A fake that answers every property hides code reading the wrong one."""
    from caldav.elements import dav

    obj = FakeObject(STANDALONE, etag="abc")
    assert obj.get_property(dav.GetEtag(), use_cached=False) == "abc"
    assert obj.get_property(dav.DisplayName(), use_cached=False) is None


def test_the_second_address_is_only_asked_for_when_the_answer_needs_it(monkeypatch):
    """Two requests per calendar on every read would double the cost of a scan."""
    calendar = FakeCalendar("Personal", PERSONAL, [SERIES])
    tool = make_tool(monkeypatch, [calendar])
    call(tool, uid="standup")
    assert calendar.asked_by_uid == []
    call(tool, uid="standup", recurrence_id=MOVED)
    assert calendar.asked_by_uid == ["standup"]
