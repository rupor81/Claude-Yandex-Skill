"""Removing an event, when "the event" means two different things.

This is the least recoverable tool in the epic, so every test is named for the
harm it prevents rather than for the branch it walks.  Five harms dominate:

* Removing a year of history when one cancelled meeting was meant.  `scope` is
  required and never guessed, exactly as it is for a change.
* Leaving a contradiction behind.  Cancelling an instance that had been moved
  removes its override in the same write; an exclusion and an override for one
  moment are a document different readers resolve differently.
* Removing something the caller did not name -- another instance, another
  component, another event sharing the object.
* Claiming a protection this server does not give.  Measured: a DELETE carrying
  a stale ETag was answered 204 and the object went anyway, so the series path
  compares the ETag immediately before deleting and *says* that is a check
  rather than a guarantee.
* Reporting success without confirming it.  The event is read afterwards: for
  an instance, that the others survived; for a series, that it is gone.

No socket is opened: `caldav.DAVClient` is the shared fake from `conftest`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import anyio
import pytest
from caldav.lib import error as caldav_error
from conftest import FakeCalendar, install_fake_dav_client
from niquests import exceptions as http_error
from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient
from yandex_calendar_mcp.tools.events import (
    DELETE_TOOL_NAME,
    SCOPE_OCCURRENCE,
    SCOPE_SERIES,
    build_calendar_event_delete,
    build_calendar_events_list,
)
from yandex_core.errors import (
    Conflict,
    NotFound,
    ProtocolError,
    RateLimited,
    TransportError,
)

URL = "https://caldav.yandex.ru"
PERSONAL = f"{URL}/calendars/me/personal/"
PASSWORD = "hunter2-app-password"


# -- the documents these tests work on ------------------------------------
#
# Written out rather than composed, so what is asserted is the code's reading
# of a stored document and not its agreement with its own composer.

ONE_OFF = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:design-review\r\nSUMMARY:Design review\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T070000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\nTRANSP:OPAQUE\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)

SERIES = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)

#: The same series, with its 10 June instance already moved to 08:00.  This is
#: the document that makes both override rules visible at once: cancelling the
#: moved instance must take the override with it, and cancelling a *different*
#: one must leave it exactly where it is.
SERIES_WITH_OVERRIDE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nRECURRENCE-ID:20260610T060000Z\r\n"
    "SUMMARY:Standup (moved)\r\n"
    "DTSTART:20260610T080000Z\r\nDTEND:20260610T083000Z\r\n"
    "DTSTAMP:20260602T000000Z\r\nSEQUENCE:1\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)

#: A series with exactly one instance, so cancelling it leaves the object
#: holding a series that never happens again.
LAST_INSTANCE_SERIES = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=1\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)

#: The series with its 9 June instance already excluded.
SERIES_WITH_EXDATE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "EXDATE:20260609T060000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)

#: Instances of SERIES, in the spelling `calendar_events_list` returns.
EIGHTH = "2026-06-08T09:00:00+03:00"
NINTH = "2026-06-09T09:00:00+03:00"
TENTH = "2026-06-10T09:00:00+03:00"


def _provider():
    async def provider() -> CalDAVCalendarClient:
        return CalDAVCalendarClient(url=URL, username="me@yandex.ru", password=PASSWORD)

    return provider


def _refusing_provider():
    """A provider that fails the test if anything tries to reach the network."""

    async def provider() -> CalDAVCalendarClient:
        raise AssertionError("a request was prepared before the arguments were checked")

    return provider


def delete(**kwargs):
    tool = build_calendar_event_delete(kwargs.pop("provider", None) or _provider())
    call = dict(uid="standup", scope=SCOPE_SERIES, etag="etag-standup")
    call.update(kwargs)
    return anyio.run(lambda: tool(**call))


def _series_calendar(document=SERIES):
    return FakeCalendar("Personal", PERSONAL, [document])


def _one_off_calendar():
    return FakeCalendar("Personal", PERSONAL, [ONE_OFF])


def _body(puts):
    assert len(puts) == 1, f"expected exactly one write, got {len(puts)}"
    return puts[0]["body"]


def _starts(calendar, uid="standup"):
    """The times the series in this calendar actually happens, expanded."""
    tool = build_calendar_events_list(_provider())
    page = anyio.run(
        lambda: tool(
            start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 30, tzinfo=timezone.utc),
            limit=50,
        )
    )
    return [item.start for item in page.items if item.uid == uid]


# -- scope: required, and never guessed -----------------------------------


@pytest.mark.parametrize("scope", [None, "", "   ", "everything"])
def test_an_omitted_scope_is_refused_before_any_request_naming_both_meanings(scope):
    """The most dangerous guess in the epic is not made."""
    with pytest.raises(ProtocolError) as caught:
        delete(provider=_refusing_provider(), scope=scope)

    message = str(caught.value)
    assert "scope" in message
    assert SCOPE_OCCURRENCE in message and SCOPE_SERIES in message


def test_a_missing_scope_says_which_of_the_two_cannot_be_undone():
    """A caller choosing between them must be told which one is irreversible."""
    with pytest.raises(ProtocolError) as caught:
        delete(provider=_refusing_provider(), scope=None)

    message = str(caught.value).lower()
    assert "irreversible" in message or "cannot be undone" in message


def test_occurrence_scope_without_a_recurrence_id_is_refused_before_any_request():
    """There is no instance to cancel, and the series must not be taken instead."""
    with pytest.raises(ProtocolError) as caught:
        delete(provider=_refusing_provider(), scope=SCOPE_OCCURRENCE)

    assert "recurrence_id" in str(caught.value)


def test_a_recurrence_id_with_series_scope_is_refused_as_contradictory():
    """Ignoring either half removes something the caller did not name."""
    with pytest.raises(ProtocolError) as caught:
        delete(provider=_refusing_provider(), scope=SCOPE_SERIES, recurrence_id=NINTH)

    message = str(caught.value)
    assert "recurrence_id" in message and SCOPE_SERIES in message


@pytest.mark.parametrize("etag", [None, "", "   "])
def test_a_missing_etag_is_refused_before_any_request(etag):
    """The version the caller last read is what makes the check possible at all."""
    with pytest.raises(ProtocolError) as caught:
        delete(provider=_refusing_provider(), etag=etag)

    assert "etag" in str(caught.value).lower()


# -- cancelling one instance ----------------------------------------------


def test_cancelling_one_instance_leaves_every_other_instance_at_its_own_time(
    monkeypatch,
):
    """The harm: "cancel Tuesday" removing the year."""
    calendar = _series_calendar()
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert answer.deleted is True
    assert answer.scope == SCOPE_OCCURRENCE
    body = _body(puts)
    assert "EXDATE" in body
    assert "20260609T060000Z" in body

    remaining = _starts(calendar)
    assert len(remaining) == 4, "cancelling one instance changed how many there are"
    assert all(start.day != 9 for start in remaining)
    assert [start.day for start in remaining] == [8, 10, 11, 12]


def test_cancelling_an_instance_that_was_moved_removes_its_override_in_one_write(
    monkeypatch,
):
    """An exclusion beside an override is a contradiction readers resolve differently."""
    calendar = _series_calendar(SERIES_WITH_OVERRIDE)
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=TENTH)

    assert answer.deleted is True
    body = _body(puts)
    assert "20260610T060000Z" in body, "the instance was not excluded"
    assert "RECURRENCE-ID" not in body, "the override for the cancelled instance stayed"
    assert "Standup (moved)" not in body

    remaining = _starts(calendar)
    assert [start.day for start in remaining] == [8, 9, 11, 12]


def test_cancelling_one_instance_leaves_a_different_instances_override_intact(
    monkeypatch,
):
    """A meeting somebody moved must not vanish because another one was cancelled."""
    calendar = _series_calendar(SERIES_WITH_OVERRIDE)
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    body = _body(puts)
    assert "RECURRENCE-ID:20260610T060000Z" in body.replace("\r\n ", "")
    assert "Standup (moved)" in body

    remaining = _starts(calendar)
    # 08:00Z is where somebody moved that instance to; the series says 06:00Z.
    assert [start.hour for start in remaining if start.day == 10] == [8], (
        "the instance that had been moved is no longer at the time it was moved to"
    )


def test_cancelling_an_instance_reads_the_series_back_and_reports_what_is_left(
    monkeypatch,
):
    """Success is confirmed against the server, never claimed from the write."""
    calendar = _series_calendar()
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert answer.confirmed is True
    assert answer.occurrences_remaining is True
    assert answer.etag and answer.etag != "etag-standup", (
        "the ETag reported after a write is the one from before it"
    )


def test_cancelling_the_last_instance_says_the_object_is_still_there(monkeypatch):
    """Never silently removed: an empty series is an object, not an absence."""
    calendar = _series_calendar(LAST_INSTANCE_SERIES)
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=EIGHTH)

    assert answer.deleted is True
    assert answer.occurrences_remaining is False
    assert deletes == [], "cancelling the last instance removed the object"
    assert calendar.holds(calendar.href_for("standup")), "the object was removed"
    assert "no occurrences" in (answer.series_note or "").lower()


def test_an_instance_that_is_already_cancelled_sends_no_write(monkeypatch):
    """Reported as already gone; a write would bump the version for nobody."""
    calendar = _series_calendar(SERIES_WITH_EXDATE)
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert puts == [], "a write was sent for an instance that was already gone"
    assert answer.already_gone is True
    assert answer.deleted is False
    assert answer.etag == "etag-standup", "the caller's version was invalidated for nothing"
    assert "already" in answer.delete_note.lower()


def test_a_stale_etag_refuses_a_cancellation_and_writes_nothing(monkeypatch):
    """The precondition is the whole point of asking for the ETag."""
    calendar = FakeCalendar("Personal", PERSONAL, [SERIES], etags={"standup": "newer"})
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(Conflict) as caught:
        delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH, etag="etag-standup")

    assert puts == [], "a write was sent on a stale precondition"
    assert "standup" in str(caught.value)
    assert [start.day for start in _starts(calendar)] == [8, 9, 10, 11, 12]


def test_cancelling_an_instance_of_a_one_off_event_is_a_not_found_for_the_instance(
    monkeypatch,
):
    """The event exists; the instance does not, and saying so is the difference."""
    calendar = _one_off_calendar()
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(NotFound) as caught:
        delete(
            uid="design-review",
            scope=SCOPE_OCCURRENCE,
            etag="etag-design-review",
            recurrence_id=NINTH,
        )

    assert puts == []
    message = str(caught.value)
    assert "design-review" in message
    assert "instance" in message


def test_an_unknown_instance_of_a_real_series_names_the_instance_not_the_event(
    monkeypatch,
):
    """"Which of the two was missing" is the whole content of this answer."""
    calendar = _series_calendar()
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(NotFound) as caught:
        delete(
            scope=SCOPE_OCCURRENCE,
            recurrence_id="2026-07-01T09:00:00+03:00",
            etag="etag-standup",
        )

    assert puts == []
    message = str(caught.value)
    assert "2026-07-01" in message
    assert "instance" in message


# -- removing a series ----------------------------------------------------


def test_deleting_a_series_removes_the_object_and_confirms_it_is_gone(monkeypatch):
    """The object, not one component of it: a series is one CalDAV object here."""
    calendar = _series_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    answer = delete(scope=SCOPE_SERIES)

    assert answer.deleted is True
    assert answer.confirmed is True
    assert len(deletes) == 1
    assert deletes[0] == calendar.href_for("standup")
    assert not calendar.holds(calendar.href_for("standup"))
    assert _starts(calendar) == []


def test_deleting_a_one_off_event_removes_it(monkeypatch):
    """`series` is what a non-recurring event takes; there is nothing else to say."""
    calendar = _one_off_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    answer = delete(uid="design-review", scope=SCOPE_SERIES, etag="etag-design-review")

    assert answer.deleted is True
    assert answer.confirmed is True
    assert len(deletes) == 1
    assert not calendar.holds(calendar.href_for("design-review"))


def test_deleting_a_series_says_the_etag_check_is_not_a_guarantee(monkeypatch):
    """Measured: this server honours If-Match on a write and ignores it on a delete."""
    calendar = _series_calendar()
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[], deletes=[])

    answer = delete(scope=SCOPE_SERIES)

    note = (answer.precondition_note or "").lower()
    assert note, "a delete that cannot be made conditional said nothing about it"
    assert "not a guarantee" in note or "cannot close" in note


def test_a_stale_etag_refuses_a_series_delete_before_anything_is_removed(monkeypatch):
    """It cannot be closed, but it can be checked -- and it is, immediately before."""
    calendar = FakeCalendar("Personal", PERSONAL, [SERIES], etags={"standup": "newer"})
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    with pytest.raises(Conflict) as caught:
        delete(scope=SCOPE_SERIES, etag="etag-standup")

    assert deletes == [], "a delete went out on a stale precondition"
    assert calendar.holds(calendar.href_for("standup"))
    message = str(caught.value).lower()
    assert "not a guarantee" in message or "cannot close" in message


def test_a_series_that_changed_between_the_read_and_the_delete_is_refused(monkeypatch):
    """The narrowing this server does allow: the ETag is read again, last thing.

    The fake re-stamps the object's ETag on every write, so a write landing
    between the first read and the delete is exactly what this reproduces.
    """
    calendar = _series_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    original = calendar.event_by_url
    seen = {"n": 0}

    def racing_event_by_url(href, data=None):
        obj = original(href, data)
        seen["n"] += 1
        if seen["n"] == 1:
            # Somebody else writes the object after we read it and before the
            # ETag is read again.
            calendar.add(href, SERIES.replace("Standup", "Standup (theirs)"))
        return obj

    calendar.event_by_url = racing_event_by_url

    with pytest.raises(Conflict):
        delete(scope=SCOPE_SERIES, etag="etag-standup")

    assert deletes == [], "the delete went ahead after the object had changed"


def test_an_unknown_uid_is_a_not_found_and_deletes_nothing(monkeypatch):
    """Nothing to delete is an error naming the UID, never a quiet success."""
    calendar = _series_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    with pytest.raises(NotFound) as caught:
        delete(uid="no-such-event", scope=SCOPE_SERIES, etag="etag-whatever")

    assert deletes == []
    assert "no-such-event" in str(caught.value)


def test_a_delete_whose_outcome_is_unknown_says_so_and_is_not_retried(monkeypatch):
    """A blind retry after a lost connection removes whatever took its place."""
    calendar = _series_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        deletes=deletes,
        delete_raises=http_error.ConnectionError("connection reset"),
    )

    with pytest.raises(TransportError) as caught:
        delete(scope=SCOPE_SERIES)

    assert len(deletes) == 1, "the delete was retried after an unknown outcome"
    message = str(caught.value)
    assert "standup" in message
    assert "unknown" in message.lower()


def test_a_rate_limited_delete_is_not_retried(monkeypatch):
    """The library's own retry is off; a refusal is reported, not repeated."""
    calendar = _series_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        deletes=deletes,
        delete_raises=caldav_error.RateLimitError("429"),
    )

    with pytest.raises(RateLimited):
        delete(scope=SCOPE_SERIES)

    assert len(deletes) == 1


def test_a_delete_the_server_answered_with_404_is_a_not_found(monkeypatch):
    """The object went between the read and the delete: nothing here removed it."""
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=[], delete_status=404
    )

    with pytest.raises(NotFound) as caught:
        delete(scope=SCOPE_SERIES)

    assert "standup" in str(caught.value)


def test_a_delete_the_server_answered_unreadably_is_not_reported_as_success(
    monkeypatch,
):
    """"The server said nothing" is not "the event is gone"."""
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=[], delete_status=None
    )

    with pytest.raises(ProtocolError) as caught:
        delete(scope=SCOPE_SERIES)

    assert "standup" in str(caught.value)


def test_a_series_still_readable_after_an_accepted_delete_is_not_called_confirmed(
    monkeypatch,
):
    """Reporting success without confirming it is the failure mode of this tool."""
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=[], delete_status=204
    )

    answer = delete(scope=SCOPE_SERIES)

    assert answer.deleted is True
    assert answer.confirmed is False
    assert answer.confirmation_note
    assert calendar.holds(calendar.href_for("standup"))


def test_an_event_in_a_url_that_is_not_a_calendar_deletes_nothing(monkeypatch):
    """A URL this account does not list is refused before anything is removed."""
    calendar = _series_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    with pytest.raises(NotFound) as caught:
        delete(scope=SCOPE_SERIES, calendar_url=f"{URL}/calendars/me/nope/")

    assert deletes == []
    assert "nope" in str(caught.value)


# -- the contract itself --------------------------------------------------


def test_the_tool_is_named_for_the_object_and_the_verb():
    """`<server>_<object>_<verb>`, as every other tool in this server is."""
    assert DELETE_TOOL_NAME == "calendar_event_delete"
    assert build_calendar_event_delete(_provider()).__name__ == DELETE_TOOL_NAME


def test_the_tools_own_documentation_says_it_is_destructive_and_unprotected():
    """A caller reads this before deciding to call it, not afterwards."""
    text = (build_calendar_event_delete(_provider()).__doc__ or "").lower()
    assert "destructive" in text
    assert "scope" in text
    assert "guarantee" in text or "cannot close" in text


# -- an object that holds more than the event named ------------------------

#: One CalDAV object holding two different events.  Rare, and produced by
#: clients that batch: the guard that exists is against one UID spread over
#: several objects, which is the opposite shape and does not catch this one.
TWO_EVENTS_IN_ONE_OBJECT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:board\r\nSUMMARY:Board meeting\r\n"
    "DTSTART:20260615T090000Z\r\nDTEND:20260615T110000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _shared_object_calendar():
    """The two-event object, stored under the href this server addresses."""
    return FakeCalendar(
        "Personal", PERSONAL, [(f"{PERSONAL}standup.ics", TWO_EVENTS_IN_ONE_OBJECT)]
    )


def test_deleting_a_series_never_removes_another_event_sharing_the_object(
    monkeypatch,
):
    """The harm: "delete the standup" taking the board meeting with it.

    A DELETE removes the object, and the object is not the event. The only
    guard here was against one UID spread over several objects -- the opposite
    shape -- so nothing looked at what else the single object held.
    """
    calendar = _shared_object_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    with pytest.raises(ProtocolError) as caught:
        delete(scope=SCOPE_SERIES)

    assert deletes == [], "the object was deleted with another event inside it"
    assert calendar.holds(f"{PERSONAL}standup.ics")
    message = str(caught.value)
    assert "board" in message, "the refusal does not say what else was in the object"
    assert "standup" in message
    # Both events are still expandable: nothing was written at all.
    assert [start.day for start in _starts(calendar)] == [8, 9, 10, 11, 12]
    assert _starts(calendar, uid="board")


def test_cancelling_one_instance_in_a_shared_object_leaves_the_other_event(
    monkeypatch,
):
    """A cancellation edits the object, so it may proceed -- and must not touch
    the event it does not name."""
    calendar = _shared_object_calendar()
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert answer.deleted is True
    assert "Board meeting" in _body(puts), "the other event was dropped by the write"
    assert _starts(calendar, uid="board"), "the other event stopped happening"


# -- what the answer says happened ----------------------------------------


def test_an_already_cancelled_instance_reports_the_series_not_the_instance(
    monkeypatch,
):
    """`stored` is documented as the series as the server holds it now.

    Handing back the record of the one cancelled instance puts that instance's
    start and a cancelled status in a field a caller reads to see that the rest
    of the series survived.
    """
    calendar = _series_calendar(SERIES_WITH_EXDATE)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert answer.already_gone is True
    assert answer.stored is not None
    assert answer.stored.start.day == 8, (
        "`stored` carries the cancelled instance's own start, not the series'"
    )
    assert answer.stored.is_series is True
    assert (answer.stored.status or "").upper() != "CANCELLED", (
        "the series is reported as cancelled because one instance of it is"
    )


def test_an_already_cancelled_instance_does_not_claim_a_readback_afterwards(
    monkeypatch,
):
    """Nothing was sent and nothing was read after it; the evidence is older."""
    calendar = _series_calendar(SERIES_WITH_EXDATE)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    note = (answer.confirmation_note or "").lower()
    assert note
    assert "afterwards" not in note, (
        "the answer claims a readback after a request that was never sent"
    )
    assert "before" in note or "no write" in note


class RefusingReadback(FakeCalendar):
    """A calendar that accepts the write and then cannot be read again.

    Not a transport failure and not a rejected credential -- those are raised
    as themselves, saying the write happened. This is the ordinary case: the
    write landed and the readback came back as something unreadable.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._written = False

    def add(self, href, data):
        super().add(href, data)
        self._written = True

    def event_by_url(self, href, data=None):
        if self._written:
            raise ValueError("the readback returned something unreadable")
        return super().event_by_url(href, data)

    def object_by_uid(self, uid, *args, **kwargs):
        if self._written:
            raise ValueError("the readback returned something unreadable")
        return super().object_by_uid(uid, *args, **kwargs)


def test_a_cancellation_whose_readback_failed_says_so_where_the_etag_is_missing(
    monkeypatch,
):
    """The ETag is null because the readback failed, not because none was sent."""
    calendar = RefusingReadback("Personal", PERSONAL, [SERIES])
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert answer.etag is None
    note = (answer.etag_note or "").lower()
    assert "supplied no etag" not in note, (
        "the caller is told the server sent no version; the readback failed"
    )
    assert "read" in note and "back" in note
    assert answer.confirmed is False
    assert answer.confirmation_note


def test_a_delete_that_could_not_be_confirmed_does_not_assert_the_event_is_gone(
    monkeypatch,
):
    """"Deleted" and "we could not see that it was deleted" are different answers."""
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=[], delete_status=204
    )

    answer = delete(scope=SCOPE_SERIES)

    assert answer.confirmed is False
    note = answer.delete_note.lower()
    assert "not reported as deleted" in note, (
        "the note asserts the event was removed on evidence nobody has"
    )
    assert "confirmation_note" in note


def test_a_confirmed_cancellation_stays_confirmed_when_the_rest_of_the_read_fails(
    monkeypatch,
):
    """The instance was read back and it is off; a later read failing cannot undo that.

    Reported unconfirmed, the caller is invited to repeat a destructive request
    that has already taken effect.
    """
    import yandex_calendar_mcp.client.caldav_client as client_module

    calendar = _series_calendar()
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    def refuse(*args, **kwargs):
        raise ValueError("the second read failed")

    monkeypatch.setattr(client_module, "has_occurrences", refuse)

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert answer.confirmed is True
    assert answer.occurrences_remaining is None
    note = answer.confirmation_note.lower()
    assert "was confirmed" in note
    assert "do not repeat the cancellation" in note, (
        "the caller is left to decide whether to send a destructive request again"
    )


def test_an_idempotent_cancellation_survives_a_failure_to_expand_the_series(
    monkeypatch,
):
    """A no-op that sends nothing must not raise because a later question could not
    be answered."""
    import yandex_calendar_mcp.client.caldav_client as client_module

    calendar = _series_calendar(SERIES_WITH_EXDATE)
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    def refuse(*args, **kwargs):
        raise ValueError("the expansion failed")

    monkeypatch.setattr(client_module, "has_occurrences", refuse)

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert puts == []
    assert answer.already_gone is True
    assert answer.occurrences_remaining is None


#: The contradiction itself: the 10 June instance is excluded *and* has an
#: entry of its own.  Readers disagree about which wins, which is why the
#: cancelling path removes the entry whenever it writes an exclusion.
SERIES_WITH_EXDATE_AND_OVERRIDE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "EXDATE:20260610T060000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nRECURRENCE-ID:20260610T060000Z\r\n"
    "SUMMARY:Standup (moved)\r\n"
    "DTSTART:20260610T080000Z\r\nDTEND:20260610T083000Z\r\n"
    "DTSTAMP:20260602T000000Z\r\nSEQUENCE:1\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_cancelling_an_instance_that_is_both_excluded_and_overridden_repairs_it(
    monkeypatch,
):
    """Answering "already gone" leaves the meeting showing in half the clients."""
    calendar = _series_calendar(SERIES_WITH_EXDATE_AND_OVERRIDE)
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=TENTH)

    body = _body(puts)
    assert "Standup (moved)" not in body, "the contradicting entry was left in place"
    assert "RECURRENCE-ID" not in body
    assert "20260610T060000Z" in body, "the exclusion was dropped along with it"
    assert answer.deleted is True
    assert "contradiction" in answer.delete_note.lower()
    # And the instance really is off everywhere now.
    assert [start.day for start in _starts(calendar)] == [8, 9, 11, 12]


# -- guards on the path nobody can undo ------------------------------------


#: The same UID as a master in one object and an override in another.
SPLIT_MASTER = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)
SPLIT_OVERRIDE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nRECURRENCE-ID:20260610T060000Z\r\n"
    "SUMMARY:Standup (moved)\r\n"
    "DTSTART:20260610T080000Z\r\nDTEND:20260610T083000Z\r\n"
    "DTSTAMP:20260602T000000Z\r\nSEQUENCE:1\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


class SplitAcrossTwoObjects(FakeCalendar):
    """One event whose override the server keeps in an object of its own."""

    def object_by_uid(self, uid, *args, **kwargs):
        self.asked_by_uid.append(uid)
        return self._wrap(f"{PERSONAL}standup-override.ics", SPLIT_OVERRIDE)


def test_an_event_spread_over_two_objects_is_refused_and_told_how_to_remove_it(
    monkeypatch,
):
    """One request covers one object; removing half of an event is not an option.

    The refusal is shared with the change path, and told to a caller who asked
    to delete it must not tell them to go and change it in another client.
    """
    calendar = SplitAcrossTwoObjects(
        "Personal",
        PERSONAL,
        [
            (f"{PERSONAL}standup.ics", SPLIT_MASTER),
            (f"{PERSONAL}standup-override.ics", SPLIT_OVERRIDE),
        ],
    )
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    with pytest.raises(ProtocolError) as caught:
        delete(scope=SCOPE_SERIES)

    assert deletes == [], "half of an event was removed under one precondition"
    message = str(caught.value)
    assert "2 separate calendar objects" in message
    assert "change it in a client" not in message, (
        "a caller who asked to delete an event is told how to change one"
    )
    assert "remove" in message


#: A second calendar on the same account, holding an event with the same UID.
OTHER = f"{URL}/calendars/me/shared/"


def test_a_uid_in_two_calendars_is_never_deleted_from_whichever_came_first(
    monkeypatch,
):
    """The harm: the wrong calendar's meeting removed, with nothing said.

    Calendars are searched in listing order and the first hit was taken. Two
    calendars can hold the same UID -- an invitation accepted in both, an
    imported .ics -- and this is the one tool where guessing cannot be undone.
    """
    personal = FakeCalendar("Personal", PERSONAL, [SERIES])
    shared = FakeCalendar("Shared", OTHER, [SERIES])
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[personal, shared], puts=[], deletes=deletes
    )

    with pytest.raises(ProtocolError) as caught:
        delete(scope=SCOPE_SERIES)

    assert deletes == [], "an event was deleted from a calendar chosen by position"
    assert personal.holds(personal.href_for("standup"))
    assert shared.holds(shared.href_for("standup"))
    message = str(caught.value)
    assert PERSONAL in message and OTHER in message
    assert "calendar_url" in message


def test_naming_the_calendar_removes_the_event_from_that_one_only(monkeypatch):
    """The refusal has to leave the caller a way through, and this is it."""
    personal = FakeCalendar("Personal", PERSONAL, [SERIES])
    shared = FakeCalendar("Shared", OTHER, [SERIES])
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[personal, shared], puts=[], deletes=deletes
    )

    answer = delete(scope=SCOPE_SERIES, calendar_url=OTHER)

    assert answer.deleted is True
    assert answer.calendar_url == OTHER
    assert deletes == [shared.href_for("standup")]
    assert personal.holds(personal.href_for("standup")), (
        "the event was removed from a calendar the caller did not name"
    )


# -- the cancellation's own answers from the server ------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (412, Conflict),
        (409, Conflict),
        (404, NotFound),
        (500, ProtocolError),
        (None, ProtocolError),
    ],
)
def test_a_cancellation_the_server_did_not_accept_is_never_reported_as_done(
    monkeypatch, status, expected
):
    """Between a refused write and a false "cancelled" there is only this branch."""
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], put_status=status
    )

    with pytest.raises(expected) as caught:
        delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert "standup" in str(caught.value)
    # And the instance is still on the calendar, exactly as it was.
    assert [start.day for start in _starts(calendar)] == [8, 9, 10, 11, 12]


def test_a_cancellation_whose_outcome_is_unknown_says_so_and_is_not_retried(
    monkeypatch,
):
    """A connection lost mid-write leaves the caller unable to tell what happened."""
    calendar = _series_calendar()
    puts = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=puts,
        put_raises=http_error.ConnectionError("connection reset"),
    )

    with pytest.raises(TransportError) as caught:
        delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert len(puts) == 1, "the write was repeated after an unknown outcome"
    assert "unknown" in str(caught.value).lower()


def test_a_rate_limited_cancellation_is_reported_as_refused_not_unknown(monkeypatch):
    """The library's own retry is off, so a 429 is a write that never happened."""
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        put_raises=caldav_error.RateLimitError("429"),
    )

    with pytest.raises(RateLimited):
        delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)


def test_an_instance_still_on_the_calendar_after_the_write_is_not_called_cancelled(
    monkeypatch,
):
    """The server answered 204 and stored nothing; only the readback shows it."""
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], put_status=204
    )

    answer = delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert answer.confirmed is False
    assert "still" in (answer.confirmation_note or "").lower()
    assert "not reported as" in answer.delete_note.lower()
    assert [start.day for start in _starts(calendar)] == [8, 9, 10, 11, 12]


# -- the loudest warning this tool emits -----------------------------------


def test_a_delete_with_no_etag_to_compare_says_no_check_was_made_at_all(monkeypatch):
    """The whole protection is a comparison; a server supplying no version has none.

    Reporting the ordinary "narrowed window" sentence here would describe a
    check that did not happen.
    """
    calendar = FakeCalendar("Personal", PERSONAL, [SERIES], etags={"standup": None})
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    answer = delete(scope=SCOPE_SERIES)

    assert answer.deleted is True
    assert len(deletes) == 1
    note = (answer.precondition_note or "").lower()
    assert "could not" in note and "no check" in note


# -- a calendar that refuses the request -----------------------------------


def test_a_calendar_that_refuses_a_cancellation_names_the_calendar_not_the_account(
    monkeypatch,
):
    """A 403 on one collection is not the account-wide app-password policy.

    Reported as that, an operator is sent to an administrator who can do
    nothing about a calendar this account may read and not write.
    """
    from yandex_core.errors import PolicyError

    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        put_raises=caldav_error.AuthorizationError(url=PERSONAL, reason="Forbidden"),
    )

    with pytest.raises(PolicyError) as caught:
        delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    message = str(caught.value)
    assert PERSONAL in message
    assert "app password" not in message.lower()
    assert "Nothing was cancelled" in message, (
        "the refusal does not say what did not happen on this path"
    )
    assert "`calendar_list`" in message, "the refusal names no next step"
    assert PASSWORD not in message


def test_a_calendar_that_refuses_a_delete_names_the_calendar_and_what_did_not_happen(
    monkeypatch,
):
    from yandex_core.errors import PolicyError

    calendar = _series_calendar()
    deletes = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        deletes=deletes,
        delete_raises=caldav_error.AuthorizationError(
            url=PERSONAL, reason="Forbidden"
        ),
    )

    with pytest.raises(PolicyError) as caught:
        delete(scope=SCOPE_SERIES)

    message = str(caught.value)
    assert PERSONAL in message
    assert "app password" not in message.lower()
    assert "Nothing was deleted" in message
    assert "`calendar_list`" in message
    assert calendar.holds(calendar.href_for("standup"))


# -- a readback that breaks off after the act ------------------------------


class BreaksOffAfterTheAct(FakeCalendar):
    """A calendar that answers until the destructive request lands, then does not.

    The connection is lost *after* the server accepted it. The one instruction
    that must survive is "do not send it again": the address may already hold
    something else.
    """

    def __init__(self, *args, break_after=1, **kwargs):
        super().__init__(*args, **kwargs)
        self._acted = False
        self._break_after = break_after

    def _maybe_break(self):
        if self._acted:
            raise http_error.ConnectionError("connection reset after the request")

    def add(self, href, data):
        super().add(href, data)
        self._acted = True

    def remove(self, href):
        removed = super().remove(href)
        self._acted = True
        return removed

    def event_by_url(self, href, data=None):
        self._maybe_break()
        return super().event_by_url(href, data)

    def object_by_uid(self, uid, *args, **kwargs):
        self._maybe_break()
        return super().object_by_uid(uid, *args, **kwargs)


def test_a_delete_whose_readback_broke_off_says_the_event_was_deleted_anyway(
    monkeypatch,
):
    """Losing this sentence loses the one instruction that prevents a repeat."""
    calendar = BreaksOffAfterTheAct("Personal", PERSONAL, [SERIES])
    deletes = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], deletes=deletes
    )

    with pytest.raises(TransportError) as caught:
        delete(scope=SCOPE_SERIES)

    assert len(deletes) == 1
    message = str(caught.value)
    assert "WAS deleted" in message
    assert "Do not delete it again" in message
    assert "standup" in message


def test_a_cancellation_whose_readback_broke_off_says_it_was_cancelled_anyway(
    monkeypatch,
):
    calendar = BreaksOffAfterTheAct("Personal", PERSONAL, [SERIES])
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(TransportError) as caught:
        delete(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH)

    assert len(puts) == 1
    message = str(caught.value)
    assert "WAS cancelled" in message
    assert "Do not cancel it again" in message


# -- an all-day series -----------------------------------------------------

#: The path where the boundary writer, the exclusion comparison and the
#: midnight coercion all meet: a `VALUE=DATE` series, one instance of which is
#: cancelled.  A date coerced to a timestamp anywhere along it moves the day
#: for every reader not on UTC.
ALL_DAY_SERIES = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:offsite\r\nSUMMARY:Offsite\r\n"
    "DTSTART;VALUE=DATE:20260608\r\nDTEND;VALUE=DATE:20260609\r\n"
    "RRULE:FREQ=DAILY;COUNT=4\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_cancelling_one_day_of_an_all_day_series_keeps_dates_as_dates(monkeypatch):
    """A `VALUE=DATE` exclusion written as a timestamp cancels a different day."""
    calendar = FakeCalendar("Personal", PERSONAL, [ALL_DAY_SERIES])
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    answer = delete(
        uid="offsite",
        scope=SCOPE_OCCURRENCE,
        etag="etag-offsite",
        recurrence_id="2026-06-09",
    )

    assert answer.deleted is True
    body = _body(puts)
    assert "EXDATE;VALUE=DATE:20260609" in body, (
        "the exclusion was written as something other than a date"
    )
    assert "20260609T" not in body, "a date was coerced to a timestamp"

    remaining = _starts(calendar, uid="offsite")
    assert [start.day for start in remaining] == [8, 10, 11]
    assert all(not isinstance(start, datetime) for start in remaining), (
        "an all-day occurrence came back as a timestamp"
    )


def test_cancelling_the_same_day_of_an_all_day_series_twice_is_a_no_op(monkeypatch):
    """The exclusion comparison has to recognise the date it just wrote."""
    calendar = FakeCalendar("Personal", PERSONAL, [ALL_DAY_SERIES])
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    delete(
        uid="offsite",
        scope=SCOPE_OCCURRENCE,
        etag="etag-offsite",
        recurrence_id="2026-06-09",
    )
    fresh = calendar.etag_at(calendar.href_for("offsite"))
    puts = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    answer = delete(
        uid="offsite",
        scope=SCOPE_OCCURRENCE,
        etag=fresh,
        recurrence_id="2026-06-09",
    )

    assert puts == [], "the same day was excluded a second time"
    assert answer.already_gone is True
