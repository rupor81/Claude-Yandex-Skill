"""Changing an event, when "the event" means two different things.

This is the first tool that overwrites what the operator already has, so every
test is named for the harm it prevents rather than for the branch it walks.
Four harms dominate:

* Changing every instance of a series when one was meant, or the reverse.  The
  two are not recoverable from each other, so `scope` is required and never
  guessed.
* Overwriting somebody else's edit.  The write carries the ETag the caller last
  read as a precondition; a stale one is refused and nothing is written.
* Losing a moved instance.  A series and the overrides of its instances live in
  one object on this server -- measured -- so the stored document is edited and
  written back whole, never replaced with a freshly composed one.
* Reporting success without confirming it.  The event is read back and the
  answer carries the stored values.

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
    UPDATE_TOOL_NAME,
    SCOPE_OCCURRENCE,
    SCOPE_SERIES,
    build_calendar_event_get,
    build_calendar_event_update,
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

MOSCOW = timezone(timedelta(hours=3))


# -- the documents these tests work on ------------------------------------
#
# Written out rather than composed, so what is asserted is the code's reading
# of a stored document and not its agreement with its own composer.

ONE_OFF = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:design-review\r\nSUMMARY:Design review\r\n"
    "DESCRIPTION:Bring the sketches\r\nLOCATION:Room 4\r\n"
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
#: the document that makes "a replacement would have destroyed it" visible.
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

#: An instance of SERIES, in the spelling `calendar_events_list` returns.
NINTH = "2026-06-09T09:00:00+03:00"
TENTH = "2026-06-10T09:00:00+03:00"
ELEVENTH = "2026-06-11T09:00:00+03:00"


def _provider():
    async def provider() -> CalDAVCalendarClient:
        return CalDAVCalendarClient(url=URL, username="me@yandex.ru", password=PASSWORD)

    return provider


def _refusing_provider():
    """A provider that fails the test if anything tries to reach the network."""

    async def provider() -> CalDAVCalendarClient:
        raise AssertionError("a request was prepared before the arguments were checked")

    return provider


def update(**kwargs):
    tool = build_calendar_event_update(kwargs.pop("provider", None) or _provider())
    call = dict(uid="standup", scope=SCOPE_SERIES, etag="etag-standup")
    call.update(kwargs)
    return anyio.run(lambda: tool(**call))


def read(uid, **kwargs):
    tool = build_calendar_event_get(_provider())
    return anyio.run(lambda: tool(uid=uid, **kwargs))


def _series_calendar(document=SERIES):
    return FakeCalendar("Personal", PERSONAL, [document])


def _one_off_calendar():
    return FakeCalendar("Personal", PERSONAL, [ONE_OFF])


def _body(puts):
    assert len(puts) == 1, f"expected exactly one write, got {len(puts)}"
    return puts[0]["body"]


# -- scope: required, and never guessed -----------------------------------


@pytest.mark.parametrize("scope", [None, "", "   "])
def test_an_omitted_scope_is_refused_before_any_request_naming_both_meanings(scope):
    """Guessing is wrong a fraction of the time and catastrophic one way."""
    with pytest.raises(ProtocolError) as caught:
        update(provider=_refusing_provider(), scope=scope)

    message = str(caught.value)
    assert "scope" in message
    assert SCOPE_OCCURRENCE in message and SCOPE_SERIES in message


def test_a_scope_that_is_neither_word_is_refused_rather_than_read_charitably():
    with pytest.raises(ProtocolError) as caught:
        update(provider=_refusing_provider(), scope="this-and-following")
    assert SCOPE_OCCURRENCE in str(caught.value)


def test_an_occurrence_scope_without_a_recurrence_id_is_refused():
    """There is no instance named, so there is no instance to change."""
    with pytest.raises(ProtocolError) as caught:
        update(provider=_refusing_provider(), scope=SCOPE_OCCURRENCE, summary="New")
    assert "recurrence_id" in str(caught.value)


def test_a_recurrence_id_with_series_scope_is_refused_as_contradictory():
    """Ignoring one of the two would change something the caller did not ask for."""
    with pytest.raises(ProtocolError) as caught:
        update(
            provider=_refusing_provider(),
            scope=SCOPE_SERIES,
            recurrence_id=NINTH,
            summary="New",
        )
    message = str(caught.value)
    assert "recurrence_id" in message and SCOPE_SERIES in message


# -- the ETag: required, sent, and honoured -------------------------------


@pytest.mark.parametrize("etag", [None, "", "  "])
def test_a_missing_etag_is_refused_before_any_request(etag):
    """A write with no precondition can silently overwrite somebody else's edit."""
    with pytest.raises(ProtocolError) as caught:
        update(provider=_refusing_provider(), etag=etag, summary="New")
    assert "etag" in str(caught.value).lower()


def test_the_write_carries_the_callers_etag_as_a_precondition(monkeypatch):
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    update(summary="Daily standup")

    headers = puts[0]["headers"]
    assert headers.get("If-Match") == "etag-standup"
    assert "If-None-Match" not in headers, "an update must not refuse to replace"


def test_a_stale_etag_is_refused_and_nothing_is_written(monkeypatch):
    """Somebody else changed it first; their edit is not overwritten."""
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(Conflict) as caught:
        update(etag="etag-from-an-hour-ago", summary="Daily standup")

    message = str(caught.value)
    assert "read the event again" in message.lower(), "the caller is not told to re-read"
    assert puts == [], "a write went out carrying an ETag known to be stale"
    assert "SUMMARY:Standup\r\n" in calendar.event_by_url(
        calendar.href_for("standup")
    ).data


def test_a_precondition_the_server_refuses_is_a_conflict_that_wrote_nothing(
    monkeypatch,
):
    """The object changed between the read and the write: a 412 comes back."""
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=puts, put_status=412
    )

    with pytest.raises(Conflict) as caught:
        update(summary="Daily standup")

    assert len(puts) == 1, "a refused write was retried"
    message = str(caught.value)
    assert "standup" in message
    assert "read the event again" in message.lower(), "the caller is not told to re-read"


def test_a_refused_write_is_never_retried_with_a_fresh_etag(monkeypatch):
    """The rule that makes the precondition worth anything at all."""
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=puts, put_status=412
    )

    with pytest.raises(Conflict):
        update(summary="Daily standup")

    assert len(puts) == 1
    assert puts[0]["headers"]["If-Match"] == "etag-standup"


# -- one instance, or all of them -----------------------------------------


def test_changing_one_occurrence_leaves_the_other_instances_alone(monkeypatch):
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(
        scope=SCOPE_OCCURRENCE,
        recurrence_id=NINTH,
        start="2026-06-09T10:00:00+03:00",
        end="2026-06-09T10:30:00+03:00",
    )

    assert updated.changed is True
    assert updated.scope == SCOPE_OCCURRENCE
    body = _body(puts)
    assert "RRULE:FREQ=DAILY;COUNT=5" in body, "the series definition was lost"
    assert "DTSTART:20260608T060000Z" in body, "the series' own start was moved"
    assert "RECURRENCE-ID:20260609T060000Z" in body
    assert "DTSTART:20260609T070000Z" in body

    # Asserted through the read path, not by reading the bytes back: another
    # instance must still be where it was.
    other = read("standup", recurrence_id=ELEVENTH)
    assert other.start == datetime(2026, 6, 11, 6, 0, tzinfo=timezone.utc)


def test_changing_the_series_changes_every_instance(monkeypatch):
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(summary="Daily standup")

    assert updated.changed is True
    assert updated.scope == SCOPE_SERIES
    assert "SUMMARY:Daily standup" in _body(puts)
    for instance in (NINTH, ELEVENTH):
        assert read("standup", recurrence_id=instance).summary == "Daily standup"


def test_changing_a_series_keeps_an_instance_that_was_already_moved(monkeypatch):
    """The row this whole design exists for.

    A series and the overrides of its instances live in one object on this
    server -- measured.  Composing a replacement document would take the moved
    instance with it, and the caller would see a successful edit with no sign
    that anything was lost.
    """
    calendar = _series_calendar(SERIES_WITH_OVERRIDE)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    update(summary="Daily standup")

    body = _body(puts)
    assert "RECURRENCE-ID:20260610T060000Z" in body, "the moved instance was dropped"
    assert "SUMMARY:Standup (moved)" in body, "the moved instance lost its own values"
    assert "DTSTART:20260610T080000Z" in body, "the moved instance was put back"

    moved = read("standup", recurrence_id=TENTH)
    assert moved.start == datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)
    assert moved.summary == "Standup (moved)"


def test_changing_an_existing_override_edits_it_rather_than_adding_a_second(
    monkeypatch,
):
    calendar = _series_calendar(SERIES_WITH_OVERRIDE)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    update(scope=SCOPE_OCCURRENCE, recurrence_id=TENTH, summary="Standup (rescheduled)")

    body = _body(puts)
    assert body.count("RECURRENCE-ID:20260610T060000Z") == 1, "a second override"
    assert "SUMMARY:Standup (moved)" not in body
    assert "SUMMARY:Standup (rescheduled)" in body
    # Its own time survives: only the summary was named.
    assert "DTSTART:20260610T080000Z" in body


def test_changing_a_one_off_event_with_series_scope_changes_it(monkeypatch):
    calendar = _one_off_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(uid="design-review", etag="etag-design-review", location="Room 9")

    assert updated.changed is True
    assert updated.stored is not None
    assert updated.stored.location == "Room 9"
    assert "LOCATION:Room 9" in _body(puts)


# -- nothing outside what was asked for -----------------------------------


def test_a_field_that_was_not_named_survives_untouched(monkeypatch):
    calendar = _one_off_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(uid="design-review", etag="etag-design-review", summary="Design")

    body = _body(puts)
    assert "DESCRIPTION:Bring the sketches" in body
    assert "LOCATION:Room 4" in body
    assert "DTSTART:20260608T060000Z" in body
    assert updated.stored.description == "Bring the sketches"


def test_the_events_identity_survives_the_change(monkeypatch):
    """A new UID, or a new href, is a new event and an orphaned old one."""
    calendar = _one_off_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(uid="design-review", etag="etag-design-review", summary="Design")

    assert updated.uid == "design-review"
    assert "UID:design-review" in _body(puts)
    assert puts[0]["url"] == calendar.href_for("design-review")
    assert updated.href == calendar.href_for("design-review")


def test_an_unrelated_component_in_the_same_object_survives(monkeypatch):
    """A VTIMEZONE the event's own times refer to is not this tool's to drop."""
    with_timezone = ONE_OFF.replace(
        "BEGIN:VEVENT",
        "BEGIN:VTIMEZONE\r\nTZID:Europe/Moscow\r\n"
        "BEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nTZOFFSETFROM:+0300\r\n"
        "TZOFFSETTO:+0300\r\nTZNAME:MSK\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
        "BEGIN:VEVENT",
        1,
    )
    calendar = FakeCalendar("Personal", PERSONAL, [with_timezone])
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    update(uid="design-review", etag="etag-design-review", summary="Design")

    assert "BEGIN:VTIMEZONE" in _body(puts)
    assert "TZID:Europe/Moscow" in _body(puts)


# -- a change that changes nothing ----------------------------------------


def test_values_identical_to_the_stored_ones_are_a_no_op_with_no_write(monkeypatch):
    calendar = _one_off_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(
        uid="design-review", etag="etag-design-review", summary="Design review"
    )

    assert updated.changed is False
    assert updated.change_note and "no" in updated.change_note.lower()
    assert puts == [], "a write was sent for a change that changes nothing"
    # It is still an honest answer about the event, not an empty one.
    assert updated.stored is not None
    assert updated.stored.summary == "Design review"
    assert updated.etag == "etag-design-review"


def test_a_no_op_on_an_instance_that_already_matches_writes_nothing(monkeypatch):
    calendar = _series_calendar(SERIES_WITH_OVERRIDE)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(
        scope=SCOPE_OCCURRENCE, recurrence_id=TENTH, summary="Standup (moved)"
    )

    assert updated.changed is False
    assert puts == [], "an override was rewritten for no reason"


def test_an_update_naming_no_field_at_all_is_refused_before_any_request():
    """Nothing was asked for, so there is nothing this could honestly report."""
    with pytest.raises(ProtocolError) as caught:
        update(provider=_refusing_provider())
    assert "summary" in str(caught.value)


# -- refused before anything is written -----------------------------------


def test_a_naive_timestamp_is_refused_before_any_request():
    with pytest.raises(ProtocolError) as caught:
        update(
            provider=_refusing_provider(),
            scope=SCOPE_OCCURRENCE,
            recurrence_id=NINTH,
            start="2026-06-09T10:00:00",
            end="2026-06-09T10:30:00",
        )
    assert "offset" in str(caught.value)


def test_an_inverted_range_is_refused_before_any_request():
    with pytest.raises(ProtocolError) as caught:
        update(
            provider=_refusing_provider(),
            uid="design-review",
            etag="etag-design-review",
            start="2026-06-08T10:00:00+03:00",
            end="2026-06-08T09:00:00+03:00",
        )
    assert "`end`" in str(caught.value)


@pytest.mark.parametrize("field", ["start", "end"])
def test_moving_one_end_of_an_event_without_the_other_is_refused(field):
    """Half a move is a guess about the other half, and this server does not guess."""
    with pytest.raises(ProtocolError) as caught:
        update(
            provider=_refusing_provider(),
            uid="design-review",
            etag="etag-design-review",
            **{field: "2026-06-08T10:00:00+03:00"},
        )
    message = str(caught.value)
    assert "start" in message and "end" in message


@pytest.mark.parametrize("summary", ["", "   "])
def test_a_blank_summary_is_refused_rather_than_stored(summary):
    with pytest.raises(ProtocolError) as caught:
        update(provider=_refusing_provider(), summary=summary)
    assert "summary" in str(caught.value)


def test_an_unbounded_field_is_refused_before_anything_is_composed():
    with pytest.raises(ProtocolError) as caught:
        update(provider=_refusing_provider(), summary="x" * 1_000_000)
    assert "characters" in str(caught.value)


@pytest.mark.parametrize("uid", ["", "   "])
def test_a_blank_uid_is_refused_before_any_request(uid):
    with pytest.raises(ProtocolError):
        update(provider=_refusing_provider(), uid=uid, summary="New")


# -- nothing to change ----------------------------------------------------


def test_an_unknown_uid_is_a_not_found_naming_the_uid(monkeypatch):
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(NotFound) as caught:
        update(uid="no-such-event", etag="etag-no-such-event", summary="New")

    assert "no-such-event" in str(caught.value)
    assert puts == [], "a write was sent for an event that does not exist"


def test_an_unknown_instance_is_told_apart_from_an_unknown_event(monkeypatch):
    """"That meeting is not on this account" and "that day is not in this
    series" need different corrections from the caller."""
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(NotFound) as caught:
        update(
            scope=SCOPE_OCCURRENCE,
            recurrence_id="2026-07-30T09:00:00+03:00",
            summary="New",
        )

    message = str(caught.value)
    assert "standup" in message
    assert "instance" in message.lower(), "the caller cannot tell which was missing"
    assert puts == []


# -- what the server now holds --------------------------------------------


def test_the_answer_reports_the_stored_values_and_the_new_etag(monkeypatch):
    calendar = _series_calendar()
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    updated = update(summary="Daily standup")

    assert updated.stored is not None
    assert updated.stored.summary == "Daily standup"
    assert updated.etag and updated.etag != "etag-standup", (
        "the ETag reported is the one from before the write"
    )
    assert updated.etag_note is None
    assert updated.differs_from_request is False
    assert updated.differences == []
    assert updated.difference_note


class AdjustingCalendar(FakeCalendar):
    """A server that stores something other than what it was sent."""

    def add(self, href, data):
        super().add(href, data.replace("SUMMARY:Daily standup", "SUMMARY:Daily"))


def test_a_value_the_server_changed_is_reported_as_a_difference(monkeypatch):
    calendar = AdjustingCalendar("Personal", PERSONAL, [SERIES])
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    updated = update(summary="Daily standup")

    assert updated.stored.summary == "Daily"
    assert updated.differs_from_request is True
    assert any("summary" in line for line in updated.differences)


class SecondDroppingCalendar(FakeCalendar):
    """This server is measured to store an event to the whole minute."""

    def add(self, href, data):
        super().add(href, data.replace("T070041Z", "T070000Z"))


def test_an_instant_the_server_moved_is_reported_as_moved(monkeypatch):
    calendar = SecondDroppingCalendar("Personal", PERSONAL, [ONE_OFF])
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    updated = update(
        uid="design-review",
        etag="etag-design-review",
        start="2026-06-08T10:00:41+03:00",
        end="2026-06-08T11:00:41+03:00",
    )

    assert updated.differs_from_request is True
    assert any("start" in line for line in updated.differences)


def test_a_microsecond_this_server_dropped_is_not_blamed_on_yandex(monkeypatch):
    calendar = _one_off_calendar()
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    updated = update(
        uid="design-review",
        etag="etag-design-review",
        start="2026-06-08T10:00:00.500000+03:00",
        end="2026-06-08T11:00:00.500000+03:00",
    )

    assert updated.differences == []
    assert updated.differs_from_request is False


# -- the write went wrong, or its outcome is unknown ----------------------


def test_a_connection_lost_mid_write_says_the_outcome_is_unknown(monkeypatch):
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=puts,
        put_raises=http_error.ConnectionError("connection reset"),
    )

    with pytest.raises(TransportError) as caught:
        update(summary="Daily standup")

    message = str(caught.value)
    assert "unknown" in message.lower()
    assert "standup" in message
    assert "calendar_event_get" in message
    assert len(puts) == 1, "a write of unknown outcome was retried"


def test_a_rate_limited_write_is_not_re_issued_and_names_the_uid(monkeypatch):
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=puts,
        put_raises=caldav_error.RateLimitError(url=PERSONAL, reason="Too Many"),
    )

    with pytest.raises(RateLimited) as caught:
        update(summary="Daily standup")

    assert len(puts) == 1
    assert "standup" in str(caught.value)


def test_a_conditional_write_answered_201_is_the_change_this_server_made(
    monkeypatch,
):
    """Measured: this server answers a successful conditional update with 201.

    Accepting that is safe only because of the precondition. `If-Match` against
    an href holding nothing is answered 412, never 201, so under this header a
    201 cannot mean "there was nothing there" -- which is exactly what it does
    mean on a create, where the guard asks the opposite question.
    """
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=puts, put_status=201
    )

    updated = update(summary="Daily standup")

    assert updated.changed is True
    assert puts[0]["headers"]["If-Match"] == "etag-standup", (
        "201 was read as success on a write that carried no precondition"
    )


def test_a_write_answered_with_no_status_at_all_is_not_success(monkeypatch):
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], put_status=None
    )

    with pytest.raises(ProtocolError) as caught:
        update(summary="Daily standup")

    assert "no status" in str(caught.value)


def test_a_server_failure_during_the_write_is_never_reported_as_changed(monkeypatch):
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], put_status=500
    )

    with pytest.raises(ProtocolError) as caught:
        update(summary="Daily standup")

    assert "500" in str(caught.value)


def test_a_calendar_that_refuses_the_write_is_a_permission_failure(monkeypatch):
    from yandex_core.errors import PolicyError

    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        put_raises=caldav_error.AuthorizationError(url=PERSONAL, reason="Forbidden"),
    )

    with pytest.raises(PolicyError) as caught:
        update(summary="Daily standup")

    assert PASSWORD not in str(caught.value)


# -- written, but not re-readable -----------------------------------------


class UnreadableAfterWrite(FakeCalendar):
    """A calendar that accepts the write and then will not answer the readback."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._written = False

    def add(self, href, data):
        super().add(href, data)
        self._written = True

    def event_by_url(self, href, data=None):
        if self._written:
            raise caldav_error.DAVError("the server would not answer the readback")
        return super().event_by_url(href, data)

    def object_by_uid(self, uid, *args, **kwargs):
        if self._written:
            raise caldav_error.DAVError("the server would not answer the readback")
        return super().object_by_uid(uid, *args, **kwargs)


def test_a_change_that_took_effect_is_never_reported_as_a_failure(monkeypatch):
    calendar = UnreadableAfterWrite("Personal", PERSONAL, [SERIES])
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    updated = update(summary="Daily standup")

    assert updated.changed is True
    assert updated.stored is None
    assert updated.stored_note and "read back" in updated.stored_note.lower()
    assert updated.etag is None
    assert updated.etag_note
    assert updated.differs_from_request is False
    assert updated.difference_note and "not" in updated.difference_note.lower()


class RevokedDuringReadback(FakeCalendar):
    """The credential stops working between the write and the readback."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._written = False

    def add(self, href, data):
        super().add(href, data)
        self._written = True

    def event_by_url(self, href, data=None):
        if self._written:
            raise caldav_error.AuthorizationError(url=str(href), reason="Unauthorized")
        return super().event_by_url(href, data)

    def object_by_uid(self, uid, *args, **kwargs):
        if self._written:
            raise caldav_error.AuthorizationError(
                url=str(self.url), reason="Unauthorized"
            )
        return super().object_by_uid(uid, *args, **kwargs)


def test_a_credential_revoked_before_the_readback_still_says_the_change_happened(
    monkeypatch,
):
    from yandex_core.errors import AuthError

    calendar = RevokedDuringReadback("Personal", PERSONAL, [SERIES])
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    with pytest.raises(AuthError) as caught:
        update(summary="Daily standup")

    message = str(caught.value)
    assert "standup" in message
    assert "changed" in message.lower() or "updated" in message.lower()
    assert PASSWORD not in message


# -- the client layer holds the same rules --------------------------------


def test_the_client_layer_also_refuses_an_unscoped_change(monkeypatch):
    """`client/` is documented as usable from a plain script."""
    from yandex_calendar_mcp.client.compose import EventEdit

    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    client = CalDAVCalendarClient(url=URL, username="me@yandex.ru", password=PASSWORD)
    with pytest.raises(ProtocolError) as caught:
        anyio.run(
            lambda: client.update_event(
                uid="standup",
                scope=None,
                etag="etag-standup",
                edit=EventEdit(summary="Daily standup"),
            )
        )

    assert "scope" in str(caught.value)
    assert puts == []


def test_the_client_layer_also_requires_the_etag(monkeypatch):
    from yandex_calendar_mcp.client.compose import EventEdit

    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    client = CalDAVCalendarClient(url=URL, username="me@yandex.ru", password=PASSWORD)
    with pytest.raises(ProtocolError) as caught:
        anyio.run(
            lambda: client.update_event(
                uid="standup",
                scope=SCOPE_SERIES,
                etag=None,
                edit=EventEdit(summary="Daily standup"),
            )
        )

    assert "etag" in str(caught.value).lower()
    assert puts == []


# -- the contract itself --------------------------------------------------


def test_the_tool_declares_itself_destructive():
    """It overwrites what was there, and a caller that gates writes must know."""
    from yandex_core.risk import RiskClass, RISK_REGISTRY, annotations_for

    assert RISK_REGISTRY[UPDATE_TOOL_NAME] is RiskClass.DESTRUCTIVE
    annotations = annotations_for(UPDATE_TOOL_NAME)
    assert annotations.destructive_hint is True
    assert annotations.read_only_hint is False


def test_the_tool_is_async_like_every_other():
    import inspect

    tool = build_calendar_event_update(_provider())
    assert inspect.iscoroutinefunction(tool)
    assert tool.__name__ == UPDATE_TOOL_NAME


def test_the_deferred_changes_are_not_offered_by_this_tool():
    """Moving calendars, redefining recurrence and inviting people are all
    "ask first" -- so this tool cannot do any of them by accident."""
    import inspect

    parameters = set(
        inspect.signature(build_calendar_event_update(_provider())).parameters
    )
    assert not parameters & {
        "attendees",
        "invitees",
        "rrule",
        "recurrence",
        "new_calendar_url",
        "move_to",
        "target_calendar_url",
    }


def test_scope_and_etag_are_required_parameters_with_no_default():
    """A default scope is a guess made once and wrong for ever after."""
    import inspect

    signature = inspect.signature(build_calendar_event_update(_provider()))
    for name in ("uid", "scope", "etag"):
        assert signature.parameters[name].default is inspect.Parameter.empty


# -- an instance that is not happening -------------------------------------


CANCELLED_INSTANCE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\nEXDATE:20260609T060000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_changing_a_cancelled_instance_is_refused_rather_than_reviving_it(
    monkeypatch,
):
    """An EXDATE instance is not in the expansion at all.

    Writing an override for it puts a called-off meeting back on somebody's
    calendar, and leaves the stored object saying the instance is both
    cancelled and not.
    """
    calendar = FakeCalendar("Personal", PERSONAL, [CANCELLED_INSTANCE])
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(ProtocolError) as caught:
        update(scope=SCOPE_OCCURRENCE, recurrence_id=NINTH, summary="Standup again")

    assert "cancelled" in str(caught.value).lower()
    assert puts == [], "a cancelled instance was written back into the series"


# -- one ETag covers one object -------------------------------------------


MASTER_ONLY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\nDTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)

OVERRIDE_ONLY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nRECURRENCE-ID:20260610T060000Z\r\n"
    "SUMMARY:Standup (moved)\r\n"
    "DTSTART:20260610T080000Z\r\nDTEND:20260610T083000Z\r\n"
    "DTSTAMP:20260602T000000Z\r\nSEQUENCE:1\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_an_event_spread_over_two_objects_is_refused_rather_than_half_changed(
    monkeypatch,
):
    """One ETag is a guard over one object.

    Editing one of two and sending that single precondition would claim a guard
    over a document it never covered, and the caller would be told the whole
    event was changed when half of it was not.
    """
    calendar = FakeCalendar(
        "Personal",
        PERSONAL,
        [
            (f"{PERSONAL}stored-elsewhere.ics", MASTER_ONLY),
            (f"{PERSONAL}standup.ics", OVERRIDE_ONLY),
        ],
    )
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(ProtocolError) as caught:
        update(scope=SCOPE_OCCURRENCE, recurrence_id=TENTH, summary="Standup again")

    assert "standup" in str(caught.value)
    assert puts == [], "half of an event was changed under one precondition"


# -- the change is visible to every other client --------------------------


def test_the_changed_component_says_it_supersedes_the_one_others_hold(monkeypatch):
    """Without a SEQUENCE bump the change is written and other calendars keep
    showing the old value."""
    calendar = _one_off_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    update(uid="design-review", etag="etag-design-review", summary="Design")

    body = _body(puts)
    assert "SEQUENCE:1" in body
    assert "SEQUENCE:0" not in body
    assert "LAST-MODIFIED" in body


# -- an all-day event stays a day -----------------------------------------


ALL_DAY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:offsite\r\nSUMMARY:Offsite\r\n"
    "DTSTART;VALUE=DATE:20260608\r\nDTEND;VALUE=DATE:20260609\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_moving_an_all_day_event_keeps_it_a_whole_day(monkeypatch):
    """A day coerced to midnight is the wrong 24 hours for most of the world."""
    calendar = FakeCalendar("Personal", PERSONAL, [ALL_DAY])
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(
        uid="offsite", etag="etag-offsite", start="2026-06-15", end="2026-06-16"
    )

    assert "DTSTART;VALUE=DATE:20260615" in _body(puts)
    assert updated.stored.all_day is True
    assert updated.differs_from_request is False


def test_turning_an_all_day_event_into_a_timed_one_is_reported_as_the_change_it_is(
    monkeypatch,
):
    calendar = FakeCalendar("Personal", PERSONAL, [ALL_DAY])
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(
        uid="offsite",
        etag="etag-offsite",
        start="2026-06-08T09:00:00+03:00",
        end="2026-06-08T17:00:00+03:00",
    )

    assert updated.changed is True
    assert updated.stored.all_day is False
    assert "DTSTART:20260608T060000Z" in _body(puts)


class DifferentlySpelledHref(FakeCalendar):
    """The measured live behaviour: two addresses, one object, two spellings.

    On the real account the href built from the UID and the one the library's
    UID lookup reports differ only in whether the `@` in the principal's own
    path segment is percent-encoded -- and the two documents they return are
    the same event serialised twice, with a DTSTAMP the server re-stamps per
    response, so they are not equal as text either. Counting them as two
    objects made an ordinary event look like one stored across several, and
    refused every change to it.
    """

    def object_by_uid(self, uid, *args, **kwargs):
        found = super().object_by_uid(uid, *args, **kwargs)
        # The same path, percent-encoded the way the live server encodes the
        # `@` in the principal segment. `%70` is `p`: an encoding of exactly
        # the same address, which is the whole point.
        found.url = str(found.url).replace("/personal/", "/%70ersonal/")
        return found


def test_one_object_answering_at_two_spellings_of_its_href_is_one_object(
    monkeypatch,
):
    calendar = DifferentlySpelledHref("Personal", PERSONAL, [SERIES_WITH_OVERRIDE])
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(
        scope=SCOPE_OCCURRENCE, recurrence_id=TENTH, summary="Standup (rescheduled)"
    )

    assert updated.changed is True
    assert "SUMMARY:Standup (rescheduled)" in _body(puts)


# -- two hrefs that really are two objects --------------------------------


SLASHED_UID = "a/b"
SLASHED_MASTER = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:a/b\r\nSUMMARY:First\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)
SLASHED_OTHER = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:a/b\r\nSUMMARY:Second\r\n"
    "DTSTART:20260609T060000Z\r\nDTEND:20260609T063000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


class SecondObjectUnderADifferentPath(FakeCalendar):
    """Two genuinely different hrefs, one of which encodes a slash.

    `<calendar>/a%2Fb.ics` is one object whose *name* contains a slash;
    `<calendar>/a/b.ics` is an object in a subordinate path. Unquoting a whole
    href in one pass turns the first into the second, so two different
    documents reduce to one key -- the exact opposite of what the guard exists
    for, with the write then landing on whichever one was addressed.
    """

    def object_by_uid(self, uid, *args, **kwargs):
        self.asked_by_uid.append(uid)
        return self._wrap(f"{PERSONAL}a/b.ics", SLASHED_OTHER)


def test_two_hrefs_that_differ_by_an_encoded_slash_are_two_objects(monkeypatch):
    """Collapsing them writes to one document while reporting on both."""
    calendar = SecondObjectUnderADifferentPath(
        "Personal",
        PERSONAL,
        [
            (f"{PERSONAL}a%2Fb.ics", SLASHED_MASTER),
            (f"{PERSONAL}a/b.ics", SLASHED_OTHER),
        ],
    )
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(ProtocolError) as caught:
        update(uid=SLASHED_UID, etag="etag-a/b", summary="Renamed")

    assert "2 separate calendar objects" in str(caught.value)
    assert puts == [], "one of two documents was written under one precondition"


# -- an all-day request the server did not honour --------------------------


class TimestampingCalendar(FakeCalendar):
    """A server that stores an all-day request as timestamps."""

    def add(self, href, data):
        super().add(
            href,
            data.replace("DTSTART;VALUE=DATE:20260615", "DTSTART:20260615T000000Z")
            .replace("DTEND;VALUE=DATE:20260616", "DTEND:20260616T000000Z"),
        )


def test_an_all_day_request_the_server_turned_into_timestamps_is_reported(
    monkeypatch,
):
    """Otherwise the answer says the server stored exactly what was asked for,
    and the event is now the wrong 24 hours for everybody not on UTC."""
    calendar = TimestampingCalendar("Personal", PERSONAL, [ALL_DAY])
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    updated = update(
        uid="offsite", etag="etag-offsite", start="2026-06-15", end="2026-06-16"
    )

    assert updated.stored.all_day is False
    assert updated.differs_from_request is True
    assert any("all_day" in line for line in updated.differences)


# -- statuses that mean something other than a stale ETag ------------------


def test_a_409_does_not_send_the_caller_to_re_read_and_retry(monkeypatch):
    """On CalDAV a 409 is usually a missing collection or a UID conflict.

    Reporting it with the stale-ETag message sends the caller to do the one
    thing that cannot help, and they will keep doing it.
    """
    calendar = _series_calendar()
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], put_status=409
    )

    with pytest.raises(Conflict) as caught:
        update(summary="Daily standup")

    message = str(caught.value)
    assert "409" in message
    assert "has changed since the ETag" not in message, (
        "a 409 is reported as somebody else's edit"
    )
    assert "standup" in message


def test_a_rate_limited_write_says_it_was_refused_rather_than_unknown(monkeypatch):
    """With the library's retry disabled a 429 is a refusal, not a mystery.

    Calling it unknown costs every rate-limited caller a needless re-read, and
    blunts the phrase for the transport case where it is true.
    """
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=puts,
        put_raises=caldav_error.RateLimitError(url=PERSONAL, reason="Too Many"),
    )

    with pytest.raises(RateLimited) as caught:
        update(summary="Daily standup")

    message = str(caught.value)
    assert len(puts) == 1
    assert "standup" in message
    assert "rate limit" in message.lower(), "the reason for the refusal is not named"
    assert "nothing was changed" in message.lower()
    assert "unknown" not in message.lower(), (
        "a refused write is described as one whose outcome nobody knows"
    )


# -- a called-off meeting is not revived by editing it ---------------------


CANCELLED_EVENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:design-review\r\nSUMMARY:Design review\r\n"
    "STATUS:CANCELLED\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T070000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_editing_a_cancelled_event_with_series_scope_is_refused_too(monkeypatch):
    """The occurrence branch already refuses this harm; `scope: series` walked
    straight past it and put a called-off meeting back on the calendar."""
    calendar = FakeCalendar("Personal", PERSONAL, [CANCELLED_EVENT])
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(ProtocolError) as caught:
        update(uid="design-review", etag="etag-design-review", location="Room 9")

    assert "cancelled" in str(caught.value).lower()
    assert puts == [], "a cancelled event was edited back into existence"


# -- the conflict a caller can act on comes first --------------------------


def test_a_stale_etag_is_reported_as_a_conflict_before_anything_else(monkeypatch):
    """A caller holding a stale ETag needs to be told to re-read.

    Told instead that their event has an unusual document layout, they cannot
    act on it at all -- and the layout is not what made this call fail.
    """
    calendar = FakeCalendar(
        "Personal",
        PERSONAL,
        [
            (f"{PERSONAL}stored-elsewhere.ics", MASTER_ONLY),
            (f"{PERSONAL}standup.ics", OVERRIDE_ONLY),
        ],
    )
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(Conflict) as caught:
        update(
            scope=SCOPE_OCCURRENCE,
            recurrence_id=TENTH,
            etag="etag-from-an-hour-ago",
            summary="Standup again",
        )

    assert "read the event again" in str(caught.value).lower()
    assert puts == []


# -- untested guards on the destructive path -------------------------------


def test_a_calendar_url_the_account_does_not_list_writes_nothing(monkeypatch):
    """A write aimed at a URL that is not a calendar is not reliably refused on
    this server: it goes somewhere nothing will ever find it."""
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(NotFound) as caught:
        update(
            calendar_url=f"{URL}/calendars/me/nonexistent/", summary="Daily standup"
        )

    message = str(caught.value)
    assert f"{URL}/calendars/me/nonexistent/" in message
    assert "nothing was changed" in message
    assert puts == [], "a write was aimed at a URL that is not a calendar"


def test_a_404_on_the_write_is_a_not_found_and_not_a_stale_etag(monkeypatch):
    """The object was read a moment ago and is gone. A caller branching on
    not-found must not meet a Conflict, or a ProtocolError, instead."""
    calendar = _series_calendar()
    puts: list = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=puts, put_status=404
    )

    with pytest.raises(NotFound) as caught:
        update(summary="Daily standup")

    message = str(caught.value)
    assert "404" in message
    assert "standup" in message
    assert calendar.href_for("standup") in message
    assert len(puts) == 1, "a write answered 404 was repeated"


# -- what was written, and the version it left behind ----------------------


def test_the_answer_carries_the_values_that_were_written(monkeypatch):
    """When the readback fails this is the only record of what the change did,
    and the answer says the change WAS applied."""
    calendar = UnreadableAfterWrite("Personal", PERSONAL, [ONE_OFF])
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    updated = update(
        uid="design-review",
        etag="etag-design-review",
        summary="Design review II",
        start="2026-06-08T12:00:00+03:00",
        end="2026-06-08T13:00:00+03:00",
    )

    assert updated.changed is True
    assert updated.stored is None
    assert updated.sent == {
        "summary": "Design review II",
        "start": "2026-06-08T09:00:00+00:00",
        "end": "2026-06-08T10:00:00+00:00",
    }


def test_a_no_op_reports_the_etag_the_caller_may_still_use(monkeypatch):
    """Nothing was written, so the version the caller holds is still current.

    A server that supplied no ETag of its own must not turn that into a null
    the caller reads as "your precondition is gone".
    """
    calendar = FakeCalendar(
        "Personal", PERSONAL, [ONE_OFF], etags={"design-review": None}
    )
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    updated = update(
        uid="design-review", etag="etag-design-review", summary="Design review"
    )

    assert updated.changed is False
    assert puts == []
    assert updated.etag == "etag-design-review"
    assert updated.etag_note is None
