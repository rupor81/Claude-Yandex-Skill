"""Writing one event, and never claiming more than was established.

This is the first tool in the project that changes the operator's data, so every
test here is named for the harm it prevents rather than for the code path it
covers.  Three harms dominate:

* Writing into a calendar the caller did not name, or into a URL that is not the
  calendar it looks like.  Measured on the live account: a URL this server hands
  back from creating a calendar is *not* that calendar's address, and a delete
  aimed at it answered success while removing nothing.
* Reporting what was asked for as though it were what exists.  The server
  adjusts stored values, so the answer is read back and reports the stored ones.
* Overwriting a meeting that was already there, or retrying a write whose
  outcome nobody knows.

No socket is opened: `caldav.DAVClient` is the shared fake from `conftest`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import anyio
import pytest
from caldav.lib import error as caldav_error
from conftest import FakeCalendar, install_fake_dav_client
from niquests import exceptions as http_error
from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient
from yandex_calendar_mcp.tools.events import (
    CREATE_TOOL_NAME,
    build_calendar_event_create,
)
from yandex_core.errors import (
    Conflict,
    NotFound,
    PolicyError,
    ProtocolError,
    TransportError,
)

URL = "https://caldav.yandex.ru"
PERSONAL = f"{URL}/calendars/me/personal/"
PASSWORD = "hunter2-app-password"

START = datetime(2026, 6, 8, 9, 0, tzinfo=timezone(timedelta(hours=3)))
END = START + timedelta(hours=1)


def _provider():
    async def provider() -> CalDAVCalendarClient:
        return CalDAVCalendarClient(url=URL, username="me@yandex.ru", password=PASSWORD)

    return provider


def _refusing_provider():
    """A provider that fails the test if anything tries to reach the network."""

    async def provider() -> CalDAVCalendarClient:
        raise AssertionError("a request was prepared before the arguments were checked")

    return provider


def create(**kwargs):
    tool = build_calendar_event_create(kwargs.pop("provider", None) or _provider())
    call = dict(calendar_url=PERSONAL, summary="Design review", start=START, end=END)
    call.update(kwargs)
    return anyio.run(lambda: tool(**call))


# -- the plain case -------------------------------------------------------


def test_a_created_event_is_reported_with_its_uid_href_and_etag(monkeypatch):
    """Without the UID and the ETag, story 1.7 cannot change what was just made."""
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    created = create()

    assert created.uid
    assert created.href == f"{PERSONAL}{created.uid}.ics"
    assert created.etag, "no ETag was reported, so the event cannot be changed safely"
    assert created.etag_note is None
    assert created.calendar_url == PERSONAL
    assert created.calendar_name == "Personal"
    assert len(puts) == 1, "one write, not two"


def test_the_answer_reports_the_stored_values_not_the_requested_ones(monkeypatch):
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert created.stored is not None
    assert created.stored.summary == "Design review"
    assert created.stored.start == START
    assert created.stored.end == END
    assert created.stored.all_day is False
    assert created.stored_note is None
    assert created.differs_from_request is False
    assert created.differences == []


def test_the_event_really_exists_on_the_server_afterwards(monkeypatch):
    """"Created" is a claim about the account, and it is verified before it is made."""
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert calendar.holds(created.href)


def test_optional_detail_is_stored_and_echoed_back_as_stored(monkeypatch):
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create(description="Bring the sketches", location="Room 4")

    assert created.stored.description == "Bring the sketches"
    assert created.stored.location == "Room 4"


def test_an_all_day_event_is_stored_as_one_and_reported_as_one(monkeypatch):
    """A date coerced to midnight blocks the wrong 24 hours for everyone off UTC."""
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    created = create(start="2026-06-08", end="2026-06-09")

    assert created.stored.all_day is True
    assert created.stored.start == date(2026, 6, 8)
    assert not isinstance(created.stored.start, datetime)
    assert created.stored.end == date(2026, 6, 9)
    assert "VALUE=DATE" in puts[0]["body"]


def test_a_timed_event_is_written_with_an_unambiguous_instant(monkeypatch):
    """A local time with no VTIMEZONE means a different moment to every reader."""
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    create()

    body = puts[0]["body"]
    assert "DTSTART:20260608T060000Z" in body
    assert "DTEND:20260608T070000Z" in body


# -- the calendar is named, and addressed by its real href ----------------


def test_the_write_goes_to_the_calendars_real_href_not_the_url_it_was_given(
    monkeypatch,
):
    """A URL this server hands back is a hint; the address comes from the listing.

    Measured: writes aimed at the URL returned from creating a calendar went
    somewhere else, and a delete aimed at it answered success while removing
    nothing. So the caller's URL selects a calendar and the listing supplies the
    address it is written to.
    """
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    created = create(calendar_url=PERSONAL.rstrip("/"))

    assert puts[0]["url"].startswith(PERSONAL)
    assert created.calendar_url == PERSONAL
    assert calendar.holds(created.href)


def test_a_calendar_url_naming_nothing_is_a_not_found_and_writes_nothing(monkeypatch):
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(NotFound) as caught:
        create(calendar_url=f"{URL}/calendars/me/nonexistent/")

    assert f"{URL}/calendars/me/nonexistent/" in str(caught.value)
    assert puts == [], "a write was attempted against a URL that is not a calendar"


def test_a_calendar_that_refuses_the_write_is_a_permission_failure_naming_it(
    monkeypatch,
):
    """A read-only calendar is not a wrong password and not a missing calendar."""
    calendar = FakeCalendar("Shared", PERSONAL)
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        put_raises=caldav_error.AuthorizationError(url=PERSONAL, reason="Forbidden"),
    )

    with pytest.raises(PolicyError) as caught:
        create()

    message = str(caught.value)
    assert PERSONAL in message
    assert PASSWORD not in message


def test_no_calendar_is_ever_chosen_for_the_caller(monkeypatch):
    """This account has four calendars and the server marks none of them default."""
    with pytest.raises(ProtocolError) as caught:
        create(provider=_refusing_provider(), calendar_url=None)
    assert "calendar_list" in str(caught.value)

    with pytest.raises(ProtocolError):
        create(provider=_refusing_provider(), calendar_url="   ")


# -- refused before anything is written -----------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", datetime(2026, 6, 8, 9, 0)),
        ("end", datetime(2026, 6, 8, 10, 0)),
        ("start", "2026-06-08T09:00:00"),
    ],
)
def test_a_naive_timestamp_is_refused_before_any_request(field, value):
    with pytest.raises(ProtocolError) as caught:
        create(provider=_refusing_provider(), **{field: value})
    assert "offset" in str(caught.value)


@pytest.mark.parametrize("end", [START, START - timedelta(minutes=30)])
def test_an_inverted_or_zero_range_is_refused_before_any_request(end):
    with pytest.raises(ProtocolError) as caught:
        create(provider=_refusing_provider(), end=end)
    assert "`end`" in str(caught.value)


def test_an_all_day_event_ending_on_its_start_date_is_refused():
    """`end` is exclusive, so a same-date pair asks for a zero-length day."""
    with pytest.raises(ProtocolError):
        create(provider=_refusing_provider(), start="2026-06-08", end="2026-06-08")


def test_a_date_paired_with_a_timestamp_is_refused():
    """Half an all-day event has no reading that is not a guess."""
    with pytest.raises(ProtocolError) as caught:
        create(provider=_refusing_provider(), start="2026-06-08", end=END)
    assert "all-day" in str(caught.value).lower()


@pytest.mark.parametrize("summary", ["", "   ", "\t\n"])
def test_a_blank_summary_is_refused_before_any_request(summary):
    """An untitled event is unfindable later, and nobody meant to create one."""
    with pytest.raises(ProtocolError) as caught:
        create(provider=_refusing_provider(), summary=summary)
    assert "summary" in str(caught.value)


def test_a_blank_optional_field_is_refused_rather_than_stored(monkeypatch):
    with pytest.raises(ProtocolError):
        create(provider=_refusing_provider(), description="   ")
    with pytest.raises(ProtocolError):
        create(provider=_refusing_provider(), location="")


# -- never overwrites, never retries blindly ------------------------------


EXISTING = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:already-there\r\nSUMMARY:Somebody else's meeting\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T070000Z\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_a_write_onto_an_existing_href_fails_rather_than_replacing_it(monkeypatch):
    """The guard is the write's own; nothing is decided by a prior read."""
    calendar = FakeCalendar("Personal", PERSONAL, [EXISTING])
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    # A uuid4 collision is not reachable by waiting for one, so the UID is
    # fixed to the one already on the calendar. What is under test is the
    # guard on the write, not how the UID was arrived at.
    monkeypatch.setattr(
        "yandex_calendar_mcp.client.caldav_client.new_uid", lambda: "already-there"
    )

    with pytest.raises(Conflict) as caught:
        create()

    assert "already-there" in str(caught.value)
    assert puts[0]["headers"].get("If-None-Match") == "*"
    stored = calendar.event_by_url(calendar.href_for("already-there")).data
    assert "Somebody else's meeting" in stored, "an existing event was overwritten"


def test_a_connection_lost_mid_write_says_the_outcome_is_unknown(monkeypatch):
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=puts,
        put_raises=http_error.ConnectionError("connection reset"),
    )

    with pytest.raises(TransportError) as caught:
        create()

    message = str(caught.value)
    assert "unknown" in message.lower()
    assert "calendar_event_get" in message, "the caller is not told how to check"
    # The UID it names must be the one that was actually written to.
    (uid,) = [
        put["url"].rsplit("/", 1)[-1].removesuffix(".ics") for put in puts
    ]
    assert uid in message
    assert len(puts) == 1, "a write of unknown outcome was retried"


# -- honest about the readback --------------------------------------------


class AdjustingCalendar(FakeCalendar):
    """A server that stores something other than what it was sent.

    This one truncates the summary, which is the shape of adjustment this
    server is reported to make. What matters is only that stored differs from
    requested, and that the answer says so instead of echoing the request.
    """

    def add(self, href, data):
        super().add(href, data.replace("SUMMARY:Design review", "SUMMARY:Design"))


def test_a_value_the_server_changed_is_reported_as_changed(monkeypatch):
    calendar = AdjustingCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert created.stored.summary == "Design"
    assert created.differs_from_request is True
    assert any("summary" in difference for difference in created.differences)
    assert created.difference_note


class UnreadableAfterWrite(FakeCalendar):
    """A calendar that accepts the write and then will not answer a read."""

    def event_by_url(self, href, data=None):
        raise caldav_error.DAVError("the server would not answer the readback")

    def object_by_uid(self, uid, *args, **kwargs):
        raise caldav_error.DAVError("the server would not answer the readback")


def test_a_write_that_succeeded_is_never_reported_as_a_failure(monkeypatch):
    """The event exists. Saying otherwise sends somebody to create it twice."""
    calendar = UnreadableAfterWrite("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert created.created is True
    assert created.uid
    assert created.stored is None
    assert created.stored_note and "read back" in created.stored_note.lower()
    assert created.etag is None
    assert created.etag_note
    # Nothing may be presented as stored, including the absence of a difference.
    assert created.differs_from_request is False
    assert created.difference_note and "not" in created.difference_note.lower()


def test_an_absent_etag_is_never_invented(monkeypatch):
    calendar = FakeCalendar("Personal", PERSONAL, etags={})
    calendar._etags = {}
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])
    monkeypatch.setattr(
        "yandex_calendar_mcp.client.caldav_client._etag_of", lambda obj: (None, False)
    )

    created = create()

    assert created.etag is None
    assert created.etag_note and "invented" in created.etag_note.lower()


# -- the contract itself ---------------------------------------------------


def test_the_tool_declares_itself_a_write_and_not_read_only():
    from yandex_core.risk import RiskClass, RISK_REGISTRY, annotations_for

    assert RISK_REGISTRY[CREATE_TOOL_NAME] is RiskClass.WRITE
    annotations = annotations_for(CREATE_TOOL_NAME)
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is False


def test_the_tool_is_async_like_every_other():
    import inspect

    tool = build_calendar_event_create(_provider())
    assert inspect.iscoroutinefunction(tool)
    assert tool.__name__ == CREATE_TOOL_NAME


def test_recurring_and_attendees_are_not_offered_by_this_tool():
    """Deferred deliberately: inviting sends mail on the operator's behalf."""
    import inspect

    parameters = set(inspect.signature(build_calendar_event_create(_provider())).parameters)
    assert not parameters & {"attendees", "rrule", "recurrence", "invitees"}


# -- review round: the harms the first pass left unguarded -----------------
#
# Everything below was added after review. Each one failed before the change it
# names, and each is named for what goes wrong on a real calendar without it.


def _uid_written(puts):
    """The UID the write actually used, read back off the request it made."""
    return puts[0]["url"].rsplit("/", 1)[-1].removesuffix(".ics")


def _client():
    return CalDAVCalendarClient(url=URL, username="me@yandex.ru", password=PASSWORD)


# 1. the client picks no calendar either


@pytest.mark.parametrize("absent", [None, "", "   "])
def test_the_client_layer_also_refuses_to_choose_a_calendar(monkeypatch, absent):
    """`client/` is documented as usable from a plain script.

    With the rule living only in `tools/`, a script that passed no calendar had
    the first one on the account chosen for it -- and the first calendar on this
    account is the operator's personal one.
    """
    personal = FakeCalendar("Personal", PERSONAL)
    work = FakeCalendar("Work", f"{URL}/calendars/me/work/")
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[personal, work], puts=puts)

    with pytest.raises(ProtocolError) as caught:
        anyio.run(
            lambda: _client().create_event(
                calendar_url=absent, summary="Design review", start=START, end=END
            )
        )

    assert "calendar_url" in str(caught.value)
    assert puts == [], "a write went to a calendar nobody named"
    assert not personal._entries, "the operator's first calendar was written into"


# 2. only 201 is "created"


@pytest.mark.parametrize("status", [200, 204])
def test_a_write_answered_as_a_replacement_is_never_reported_as_created(
    monkeypatch, status
):
    """204 on a PUT means an object at that href was *replaced*.

    That is precisely the outcome `If-None-Match: *` exists to prevent, so a
    server that ignores the guard would have this tool report "created" over a
    destroyed meeting.
    """
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=puts, put_status=status
    )

    with pytest.raises(ProtocolError) as caught:
        create()

    message = str(caught.value)
    assert str(status) in message
    assert "replac" in message.lower(), "the caller is not told what 204 means"
    assert _uid_written(puts) in message


def test_a_created_write_is_the_201_and_only_the_201(monkeypatch):
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[], put_status=201)

    # 201 gets past the status check; the readback then finds nothing, which is
    # reported honestly rather than as a failure of the write.
    created = create()
    assert created.created is True
    assert created.stored is None


@pytest.mark.parametrize("status", [202, 207])
def test_a_two_hundred_that_is_not_a_created_is_not_read_as_success(
    monkeypatch, status
):
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=[], put_status=status
    )
    with pytest.raises(ProtocolError):
        create()


# 3. a rate-limited write is never re-issued


def test_the_write_client_disables_the_librarys_own_retry_of_a_put(monkeypatch):
    """`caldav`'s `DAVClient.request` sleeps on 429/503 and re-issues the request.

    PUT included. If the first attempt landed, the retry answers 412 and this
    code reports "nothing was created" for an event that exists -- so the rule
    "never retry a write blindly" has to be enforced one layer below where it is
    written.
    """
    import caldav

    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])
    base = caldav.DAVClient
    made: list = []

    class Recording(base):
        def __init__(self, **kwargs):
            made.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(caldav, "DAVClient", Recording)

    create()

    assert made, "no client was constructed"
    assert made[-1].get("rate_limit_handle") is False, (
        "the library will sleep and re-issue the PUT by itself"
    )


def test_a_rate_limited_write_names_the_uid_to_check(monkeypatch):
    """A 429 leaves the outcome unknown, and a blind retry makes two meetings."""
    from yandex_core.errors import RateLimited

    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=puts,
        put_raises=caldav_error.RateLimitError(url=PERSONAL, reason="Too Many Requests"),
    )

    with pytest.raises(RateLimited) as caught:
        create()

    message = str(caught.value)
    assert len(puts) == 1, "a rate-limited write was re-issued"
    assert _uid_written(puts) in message, "the caller is not told what to check for"
    assert "calendar_event_get" in message


# 4. every branch of the write-status taxonomy


def test_a_server_failure_during_the_write_is_never_reported_as_created(monkeypatch):
    """A 500 or a 507 must not fall through to the readback and become success."""
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts, put_status=500)

    with pytest.raises(ProtocolError) as caught:
        create()

    message = str(caught.value)
    assert "500" in message
    assert _uid_written(puts) in message
    assert "calendar_event_get" in message


def test_a_forbidden_status_on_the_write_is_not_read_as_success(monkeypatch):
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[], put_status=403)
    with pytest.raises(ProtocolError):
        create()


def test_a_not_found_on_the_write_does_not_deny_a_calendar_that_is_listed(monkeypatch):
    """This path is reached only *after* the URL matched the principal's listing.

    Telling the operator the URL "is not one of the calendars this account
    lists" sends them to `calendar_list`, which shows the calendar right there.
    """
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[], put_status=404)

    with pytest.raises(NotFound) as caught:
        create()

    message = str(caught.value)
    assert "is not one of the calendars this account lists" not in message
    assert PERSONAL in message


def test_a_write_answered_with_no_status_at_all_is_not_success(monkeypatch):
    """"The server said nothing" is not "the event was created"."""
    calendar = FakeCalendar("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(
        monkeypatch, calendars=[calendar], puts=puts, put_status=None
    )

    with pytest.raises(ProtocolError) as caught:
        create()

    assert "no status" in str(caught.value)
    assert _uid_written(puts) in str(caught.value)


# 5. a refused write, told apart from a refused account


def test_a_revoked_password_during_a_write_is_not_called_a_read_only_calendar(
    monkeypatch,
):
    """Sending the operator to fix a calendar permission that is not the problem
    sends them somewhere no fix exists."""
    from yandex_core.errors import AuthError

    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        put_raises=caldav_error.AuthorizationError(url=PERSONAL, reason="Unauthorized"),
    )

    with pytest.raises(AuthError) as caught:
        create()

    assert not isinstance(caught.value, PolicyError)
    message = str(caught.value)
    assert "app password" in message.lower()
    assert PASSWORD not in message


def test_a_refusal_that_cannot_be_classified_is_not_settled_by_a_guess(monkeypatch):
    """Neither status nor reason phrase: both causes are named, neither asserted."""
    from yandex_core.errors import AuthError

    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(
        monkeypatch,
        calendars=[calendar],
        puts=[],
        put_raises=caldav_error.AuthorizationError(url=PERSONAL, reason="None given"),
    )

    with pytest.raises(AuthError) as caught:
        create()

    assert not isinstance(caught.value, PolicyError)
    message = str(caught.value).lower()
    assert "401" in message and "403" in message


# 6. the central promise: the answer reports what the server stored


class SecondDroppingCalendar(FakeCalendar):
    """This server is measured to store an event to the minute."""

    def add(self, href, data):
        super().add(href, data.replace("T060041Z", "T060000Z"))


def test_an_instant_the_server_moved_is_reported_as_moved(monkeypatch):
    """The field this tool was designed around: the server drops the seconds."""
    calendar = SecondDroppingCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    start = START.replace(second=41)
    created = create(start=start, end=start + timedelta(hours=1))

    assert created.stored.start != start
    assert created.differs_from_request is True, (
        "the server stored a different instant and the answer said it matched"
    )
    assert any("start" in line for line in created.differences)


class TimingAnAllDayCalendar(FakeCalendar):
    """A server that turns a whole day into a timed midnight-to-midnight event."""

    def add(self, href, data):
        super().add(
            href,
            data.replace("DTSTART;VALUE=DATE:20260608", "DTSTART:20260608T000000Z")
            .replace("DTEND;VALUE=DATE:20260609", "DTEND:20260609T000000Z"),
        )


def test_an_all_day_event_stored_as_a_timed_one_is_reported_as_changed(monkeypatch):
    """A day turned into 24 hours of UTC is the wrong day for most of the world."""
    calendar = TimingAnAllDayCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create(start="2026-06-08", end="2026-06-09")

    assert created.stored.all_day is False
    assert created.differs_from_request is True
    assert any("all_day" in line or "start" in line for line in created.differences)


# 7. every field the answer exposes is compared


class SeriesMakingCalendar(FakeCalendar):
    def add(self, href, data):
        super().add(href, data.replace("SEQUENCE:0", "SEQUENCE:0\r\nRRULE:FREQ=WEEKLY"))


def test_a_one_off_stored_as_a_series_is_never_called_a_full_match(monkeypatch):
    """`stored.is_series` true while the answer says every value matched is a lie."""
    calendar = SeriesMakingCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert created.stored.is_series is True
    assert created.differs_from_request is True
    assert any("series" in line or "is_series" in line for line in created.differences)


class CancellingCalendar(FakeCalendar):
    def add(self, href, data):
        super().add(href, data.replace("SEQUENCE:0", "SEQUENCE:0\r\nSTATUS:CANCELLED"))


def test_an_event_stored_as_cancelled_is_reported_as_changed(monkeypatch):
    """A meeting created as cancelled is on nobody's calendar in practice."""
    calendar = CancellingCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert created.stored.status == "CANCELLED"
    assert created.differs_from_request is True
    assert any("status" in line for line in created.differences)


def test_a_stored_confirmed_status_is_not_reported_as_a_difference(monkeypatch):
    """A server stating the confirmation the request implied changed nothing."""

    class ConfirmingCalendar(FakeCalendar):
        def add(self, href, data):
            super().add(href, data.replace("SEQUENCE:0", "SEQUENCE:0\r\nSTATUS:CONFIRMED"))

    calendar = ConfirmingCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert created.stored.status == "CONFIRMED"
    assert created.differs_from_request is False
    assert created.differences == []


# 8. the comparison is against what was sent


def test_a_microsecond_this_server_dropped_is_not_blamed_on_yandex(monkeypatch):
    """The composer truncates microseconds before the PUT.

    Reporting that as a value the *server* changed accuses Yandex of an edit
    this code made, and buries a real difference in noise.
    """
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    start = START.replace(microsecond=500_000)
    created = create(start=start, end=start + timedelta(hours=1))

    assert created.differences == [], "a difference this server made was blamed on Yandex"
    assert created.differs_from_request is False


# 9. the answer's own description cap


def test_a_long_stored_description_is_cut_and_the_answer_says_so(monkeypatch):
    from yandex_calendar_mcp.tools.events import MAX_DESCRIPTION_CHARS

    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    body = "x" * (MAX_DESCRIPTION_CHARS + 500)
    created = create(description=body)

    assert len(created.stored.description) == MAX_DESCRIPTION_CHARS
    assert created.stored.description_truncated is True
    # The event on the server is whole, so this is not a difference.
    assert created.differs_from_request is False


# 10. nothing unbounded is composed and PUT


@pytest.mark.parametrize("field", ["summary", "description", "location"])
def test_an_unbounded_field_is_refused_before_anything_is_composed(field):
    """A megabyte description composed and PUT surfaces as "may or may not exist"."""
    with pytest.raises(ProtocolError) as caught:
        create(provider=_refusing_provider(), **{field: "x" * 1_000_000})
    message = str(caught.value)
    assert field in message
    assert "characters" in message


def test_a_field_at_the_bound_is_accepted(monkeypatch):
    from yandex_calendar_mcp.tools.events import MAX_SUMMARY_CHARS

    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])
    created = create(summary="x" * MAX_SUMMARY_CHARS)
    assert created.created is True


# 11. a readback failure keeps its words, and the taxonomy is not swallowed


def test_a_readback_failure_keeps_what_the_server_said(monkeypatch):
    calendar = UnreadableAfterWrite("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert created.stored_note
    assert "the server would not answer the readback" in created.stored_note


class RevokedDuringReadback(FakeCalendar):
    """The credential stops working between the write and the readback."""

    def event_by_url(self, href, data=None):
        raise caldav_error.AuthorizationError(url=str(href), reason="Unauthorized")

    def object_by_uid(self, uid, *args, **kwargs):
        raise caldav_error.AuthorizationError(url=str(self.url), reason="Unauthorized")


def test_a_credential_revoked_before_the_readback_is_not_swallowed(monkeypatch):
    """A rejected password is a fact about the account, not an unexplained note."""
    from yandex_core.errors import AuthError

    calendar = RevokedDuringReadback("Personal", PERSONAL)
    puts: list = []
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=puts)

    with pytest.raises(AuthError) as caught:
        create()

    message = str(caught.value)
    # The event exists. The caller must be told so, or they will make a second.
    assert _uid_written(puts) in message
    assert "created" in message.lower()
    assert PASSWORD not in message


# 13. the timestamps the error message promises are accepted


@pytest.mark.parametrize(
    "text", ["2026-06-08 09:00:00+03:00", "2026-06-08t09:00:00+03:00"]
)
def test_an_iso_timestamp_with_a_separator_other_than_capital_t_is_accepted(
    monkeypatch, text
):
    """`datetime.fromisoformat` accepts both, and the refusal claimed they were
    not ISO 8601 with an offset."""
    calendar = FakeCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create(start=text, end="2026-06-08T10:00:00+03:00")

    assert created.stored.start == START


# 14. the href that is reported is the href that was read


class RenamingCalendar(FakeCalendar):
    """A server that files the object under an href of its own choosing."""

    def add(self, href, data):
        super().add(str(href).replace(".ics", "-server.ics"), data)


def test_the_reported_href_is_the_object_that_was_found(monkeypatch):
    """`href` is documented as the object that was written; the constructed one
    may not be where the object is, and a later update aimed at it would miss."""
    calendar = RenamingCalendar("Personal", PERSONAL)
    install_fake_dav_client(monkeypatch, calendars=[calendar], puts=[])

    created = create()

    assert created.href.endswith("-server.ics")
    assert calendar.holds(created.href)
