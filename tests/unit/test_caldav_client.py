"""Every protocol exception is translated before it can leave `client/`.

The fakes stand in for `caldav.DAVClient`, so no socket is opened. They live in
`conftest.py`, shared with the other suites that need them.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import anyio
import caldav
import pytest
from caldav.lib import error as caldav_error
from conftest import FakeCalendar, install_fake_dav_client
from niquests import exceptions as http_error
from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient
from yandex_core.errors import (
    AuthError,
    NotFound,
    PolicyError,
    ProtocolError,
    RateLimited,
    TransportError,
    YandexError,
)

PASSWORD = "hunter2-app-password"
URL = "https://caldav.yandex.ru"


class FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


def make_client() -> CalDAVCalendarClient:
    return CalDAVCalendarClient(url=URL, username="me@yandex.ru", password=PASSWORD)


def test_happy_path_returns_names_and_urls(monkeypatch):
    install_fake_dav_client(
        monkeypatch,
        calendars=[
            FakeCalendar("Personal", f"{URL}/calendars/me/personal/"),
            FakeCalendar("Work", f"{URL}/calendars/me/work/"),
        ],
    )
    refs = anyio.run(make_client().list_calendars)
    assert [(r.name, r.url) for r in refs] == [
        ("Personal", f"{URL}/calendars/me/personal/"),
        ("Work", f"{URL}/calendars/me/work/"),
    ]


def test_calendar_without_a_display_name_falls_back_to_its_url(monkeypatch):
    install_fake_dav_client(
        monkeypatch, calendars=[FakeCalendar(None, f"{URL}/calendars/me/events/")]
    )
    (ref,) = anyio.run(make_client().list_calendars)
    assert ref.name == "events"


def test_revoked_app_password_is_an_auth_error(monkeypatch):
    install_fake_dav_client(
        monkeypatch,
        on_principal=caldav_error.AuthorizationError(url=URL, reason="Unauthorized"),
    )
    with pytest.raises(AuthError) as caught:
        anyio.run(make_client().list_calendars)
    message = str(caught.value)
    assert "app password" in message
    assert PASSWORD not in message


def test_forbidden_is_reported_as_organisation_policy(monkeypatch):
    install_fake_dav_client(
        monkeypatch,
        on_principal=caldav_error.AuthorizationError(url=URL, reason="Forbidden"),
    )
    with pytest.raises(PolicyError) as caught:
        anyio.run(make_client().list_calendars)
    message = str(caught.value)
    assert "organisation policy" in message
    assert PASSWORD not in message


def test_organisation_policy_is_not_an_auth_error(monkeypatch):
    """The two 403/401 cases must be distinguishable by the caller."""
    install_fake_dav_client(
        monkeypatch,
        on_principal=caldav_error.AuthorizationError(url=URL, reason="Forbidden"),
    )
    with pytest.raises(YandexError) as caught:
        anyio.run(make_client().list_calendars)
    assert not isinstance(caught.value, AuthError)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        # The one phrase that actually says 403.
        ("Forbidden", PolicyError),
        ("forbidden", PolicyError),
        # A named 401 is a credential problem and nothing else.
        ("Unauthorized", AuthError),
        # caldav attaches these when the server gave it nothing usable. Neither
        # cause can be told from the other, so the caller is told both.
        ("", AuthError),
        (None, AuthError),
        ("None given", AuthError),
    ],
)
def test_reason_phrases_caldav_really_attaches(monkeypatch, reason, expected):
    install_fake_dav_client(
        monkeypatch,
        on_principal=caldav_error.AuthorizationError(url=URL, reason=reason),
    )
    with pytest.raises(expected) as caught:
        anyio.run(make_client().list_calendars)
    assert PASSWORD not in str(caught.value)


@pytest.mark.parametrize("reason", ["", None, "None given"])
def test_an_undecidable_refusal_names_both_possibilities(monkeypatch, reason):
    """Never guess: say the cause could not be distinguished, and name both."""
    install_fake_dav_client(
        monkeypatch,
        on_principal=caldav_error.AuthorizationError(url=URL, reason=reason),
    )
    with pytest.raises(AuthError) as caught:
        anyio.run(make_client().list_calendars)
    message = str(caught.value)
    assert "did not say" in message
    assert "401" in message and "403" in message
    assert "app password" in message


def test_a_numeric_status_beats_the_reason_phrase(monkeypatch):
    """When the response carries a status, that is what decides it."""

    class Response:
        status_code = 403

    failure = caldav_error.AuthorizationError(url=URL, reason="Unauthorized")
    failure.response = Response()
    install_fake_dav_client(monkeypatch, on_principal=failure)
    with pytest.raises(PolicyError):
        anyio.run(make_client().list_calendars)


def test_the_connection_is_closed_after_every_call(monkeypatch):
    """The client owns a TLS pool; one leaked per call would accumulate."""
    closed = install_fake_dav_client(monkeypatch, calendars=[])
    anyio.run(make_client().list_calendars)
    assert closed, "DAVClient was never closed"


def test_a_failing_name_fetch_is_not_swallowed_as_a_placeholder(monkeypatch):
    """A DAV failure while reading a name must not become the URL fallback."""

    class ExplodingCalendar:
        url = f"{URL}/calendars/me/events/"

        @property
        def name(self):
            raise caldav_error.PropfindError(url=URL, reason="Bad Gateway")

    install_fake_dav_client(monkeypatch, calendars=[ExplodingCalendar()])
    with pytest.raises(ProtocolError):
        anyio.run(make_client().list_calendars)


@pytest.mark.parametrize(
    "failure",
    [
        http_error.ConnectionError("name resolution failed"),
        http_error.ConnectTimeout("timed out"),
        http_error.ReadTimeout("timed out"),
        http_error.SSLError("bad certificate"),
    ],
)
def test_transport_failures_never_escape_as_http_exceptions(monkeypatch, failure):
    install_fake_dav_client(monkeypatch, raises=failure)
    with pytest.raises(TransportError) as caught:
        anyio.run(make_client().list_calendars)
    assert URL in str(caught.value)


def test_not_found_is_translated(monkeypatch):
    install_fake_dav_client(
        monkeypatch, on_principal=caldav_error.NotFoundError(url=URL, reason="Not Found")
    )
    with pytest.raises(NotFound):
        anyio.run(make_client().list_calendars)


def test_rate_limiting_is_translated(monkeypatch):
    install_fake_dav_client(
        monkeypatch, on_principal=caldav_error.RateLimitError(url=URL, reason="Too Many Requests")
    )
    with pytest.raises(RateLimited):
        anyio.run(make_client().list_calendars)


def test_other_dav_errors_become_protocol_errors(monkeypatch):
    install_fake_dav_client(
        monkeypatch, on_principal=caldav_error.PropfindError(url=URL, reason="Bad Gateway")
    )
    with pytest.raises(ProtocolError):
        anyio.run(make_client().list_calendars)


def test_unexpected_exceptions_are_still_wrapped(monkeypatch):
    install_fake_dav_client(monkeypatch, on_principal=ValueError("something odd"))
    with pytest.raises(ProtocolError):
        anyio.run(make_client().list_calendars)


def test_client_is_constructed_directly_with_the_given_credentials(monkeypatch):
    captured = {}

    class FakeDAVClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def principal(self):
            return FakePrincipal([])

    monkeypatch.setattr(caldav, "DAVClient", FakeDAVClient)
    monkeypatch.setenv("CALDAV_URL", "https://not-this-one.example")
    anyio.run(make_client().list_calendars)

    assert captured["url"] == URL
    assert captured["username"] == "me@yandex.ru"
    assert captured["password"] == PASSWORD
    assert captured["timeout"] > 0, "a request without a timeout can hang forever"


# -- the range fetch -------------------------------------------------------

MOSCOW = timezone(timedelta(hours=3))
RANGE_START = datetime(2026, 6, 1, tzinfo=MOSCOW)
RANGE_END = datetime(2026, 6, 30, tzinfo=MOSCOW)

STANDUP = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup-1\r\nSUMMARY:Standup\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T091500\r\n"
    "RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


WEEKLY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:weekly-1\r\nSUMMARY:Weekly\r\n"
    "DTSTART;TZID=Europe/Moscow:20260609T140000\r\n"
    "DTEND;TZID=Europe/Moscow:20260609T150000\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=2\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def fetch(client, **kwargs):
    async def run():
        return await client.list_occurrences(
            start=RANGE_START, end=RANGE_END, **kwargs
        )

    return anyio.run(run)


def test_the_range_fetch_expands_series_locally(monkeypatch):
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP])
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    result = fetch(make_client())

    assert len(result.occurrences) == 3
    assert result.unreadable == 0
    assert result.truncated is False
    assert {o.calendar_name for o in result.occurrences} == {"Personal"}


def test_the_only_server_side_filter_is_the_time_range(monkeypatch):
    """Server-side expansion and text-match are both refused deliberately."""
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP])
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    fetch(make_client())

    assert calendar.searched == {
        "start": RANGE_START,
        "end": RANGE_END,
        "event": True,
        "expand": False,
    }


def test_the_range_fetch_takes_no_text_parameter():
    parameters = set(inspect.signature(CalDAVCalendarClient.list_occurrences).parameters)
    assert not parameters & {"title", "title_contains", "text", "summary", "query"}


def test_every_calendar_is_searched_when_none_is_named(monkeypatch):
    calendars = [
        FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP]),
        FakeCalendar("Work", f"{URL}/c/work/", [STANDUP]),
    ]
    install_fake_dav_client(monkeypatch, calendars=calendars)

    result = fetch(make_client())

    assert len(result.occurrences) == 6
    assert {o.calendar_name for o in result.occurrences} == {"Personal", "Work"}


def test_one_named_calendar_is_searched_alone(monkeypatch):
    calendars = [
        FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP]),
        FakeCalendar("Work", f"{URL}/c/work/", [STANDUP]),
    ]
    install_fake_dav_client(monkeypatch, calendars=calendars)

    result = fetch(make_client(), calendar_url=f"{URL}/c/work/")

    assert {o.calendar_name for o in result.occurrences} == {"Work"}
    assert calendars[0].searched is None


def test_every_occurrence_carries_the_url_of_the_calendar_it_came_from(monkeypatch):
    """Blanking `calendar_url` must not pass: it is how a caller addresses one."""
    calendars = [
        FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP]),
        FakeCalendar("Work", f"{URL}/c/work/", [WEEKLY]),
    ]
    install_fake_dav_client(monkeypatch, calendars=calendars)

    result = fetch(make_client())

    by_url = {}
    for occurrence in result.occurrences:
        by_url.setdefault(occurrence.calendar_url, set()).add(occurrence.uid)
    assert by_url == {
        f"{URL}/c/personal/": {"standup-1"},
        f"{URL}/c/work/": {"weekly-1"},
    }


def test_a_single_calendar_query_labels_occurrences_with_that_calendar(monkeypatch):
    """One calendar asked for by URL must be labelled as the listing labels it.

    A query for one calendar and a query for all of them describe the same
    events; labelling them differently is a difference no caller can explain.
    """
    calendars = [
        FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP]),
        FakeCalendar("Work", f"{URL}/c/work/", [STANDUP]),
    ]
    install_fake_dav_client(monkeypatch, calendars=calendars)

    named = fetch(make_client(), calendar_url=f"{URL}/c/work/")
    everything = fetch(make_client())

    assert {(o.calendar_url, o.calendar_name) for o in named.occurrences} == {
        (f"{URL}/c/work/", "Work")
    }
    assert {(o.calendar_url, o.calendar_name) for o in everything.occurrences} >= {
        (f"{URL}/c/work/", "Work")
    }


def test_an_unknown_calendar_url_is_a_not_found(monkeypatch):
    install_fake_dav_client(monkeypatch, calendars=[])
    with pytest.raises(NotFound):
        fetch(make_client(), calendar_url=f"{URL}/c/gone/")


def test_one_unreadable_calendar_does_not_lose_the_others(monkeypatch):
    """A 403 on one shared calendar is a counted loss, not the end of the query."""
    calendars = [
        FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP]),
        FakeCalendar(
            "Shared",
            f"{URL}/c/shared/",
            raises=caldav_error.AuthorizationError(url=URL, reason="Forbidden"),
        ),
    ]
    install_fake_dav_client(monkeypatch, calendars=calendars)

    result = fetch(make_client())

    assert len(result.occurrences) == 3
    assert {o.calendar_name for o in result.occurrences} == {"Personal"}
    assert result.unreadable_calendars == 1


def test_object_data_arriving_as_bytes_is_decoded_not_stringified(monkeypatch):
    """`str(b"BEGIN:...")` parses as nothing and would lose every event in it."""
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP.encode("utf-8")])
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    result = fetch(make_client())

    assert len(result.occurrences) == 3
    assert result.unreadable == 0


def test_a_failure_during_the_search_is_translated(monkeypatch):
    calendar = FakeCalendar(
        "Personal",
        f"{URL}/c/personal/",
        raises=caldav_error.AuthorizationError(url=URL, reason="Unauthorized"),
    )
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    with pytest.raises(AuthError):
        fetch(make_client())


def test_transport_failures_during_a_range_fetch_are_translated(monkeypatch):
    install_fake_dav_client(monkeypatch, raises=http_error.ConnectionError("no route"))
    with pytest.raises(TransportError):
        fetch(make_client())


def test_no_caldav_exception_escapes_the_range_fetch(monkeypatch):
    calendar = FakeCalendar(
        "Personal",
        f"{URL}/c/personal/",
        raises=caldav_error.PropfindError(url=URL, reason="Bad Gateway"),
    )
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    with pytest.raises(YandexError):
        fetch(make_client())


def test_an_unreadable_object_is_counted_rather_than_failing_the_query(monkeypatch):
    calendar = FakeCalendar(
        "Personal", f"{URL}/c/personal/", [STANDUP, "this is not iCalendar"]
    )
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    result = fetch(make_client())

    assert len(result.occurrences) == 3
    assert result.unreadable == 1


def test_the_connection_is_closed_after_a_range_fetch(monkeypatch):
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP])
    closed = install_fake_dav_client(monkeypatch, calendars=[calendar])

    fetch(make_client())

    assert closed, "the TLS connection pool was left to the garbage collector"


def test_a_query_where_every_calendar_fails_raises_rather_than_returning_nothing(
    monkeypatch,
):
    """An empty page here would read as "your calendar is empty"."""
    calendars = [
        FakeCalendar(
            "Personal",
            f"{URL}/c/personal/",
            raises=caldav_error.AuthorizationError(url=URL, reason="Forbidden"),
        ),
        FakeCalendar(
            "Work",
            f"{URL}/c/work/",
            raises=caldav_error.AuthorizationError(url=URL, reason="Forbidden"),
        ),
    ]
    install_fake_dav_client(monkeypatch, calendars=calendars)

    with pytest.raises(PolicyError):
        fetch(make_client())


def test_a_ceiling_below_one_is_refused_by_name(monkeypatch):
    """Zero or negative would mark every non-empty answer truncated."""
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP])
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    for ceiling in (0, -1):
        with pytest.raises(ProtocolError) as caught:
            fetch(make_client(), ceiling=ceiling)
        assert "ceiling" in str(caught.value)


def test_the_ceiling_applies_after_the_resume_point(monkeypatch):
    """Otherwise everything past the ceiling would be unreachable for ever."""
    from yandex_calendar_mcp.client.recurrence import occurrence_sort_key

    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [STANDUP])
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    first = fetch(make_client(), ceiling=1)
    assert first.truncated is True
    assert len(first.occurrences) == 1

    second = fetch(
        make_client(), ceiling=1, after=occurrence_sort_key(first.occurrences[0])
    )
    assert len(second.occurrences) == 1
    assert second.occurrences[0].start > first.occurrences[0].start


# -- what the busy question needs the client to carry down -----------------

CROSSING = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:crossing-1\r\nSUMMARY:Overnight\r\n"
    "DTSTART;TZID=Europe/Moscow:20260531T230000\r\n"
    "DTEND;TZID=Europe/Moscow:20260601T020000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)

DECLINED_INVITE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:invite-1\r\nSUMMARY:Invitation\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T100000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T110000\r\n"
    "ATTENDEE;PARTSTAT=ACCEPTED:mailto:someone@yandex.ru\r\n"
    "ATTENDEE;PARTSTAT=DECLINED:mailto:me@yandex.ru\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_the_overlap_rule_is_carried_down_into_the_expansion(monkeypatch):
    """Dropped on the way down, every meeting that began before the range
    vanishes -- and the busy answer reports the range's first hours as free."""
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [CROSSING])
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    listing = fetch(make_client())
    assert [o.uid for o in listing.occurrences] == []

    overlapping = fetch(make_client(), overlap=True)
    assert [o.uid for o in overlapping.occurrences] == ["crossing-1"]


def test_the_overlap_rule_has_no_default_on_the_way_down():
    """A default on the private half turns an omission into a quiet wrong
    answer instead of a TypeError."""
    parameter = inspect.signature(
        CalDAVCalendarClient._list_occurrences_blocking
    ).parameters["overlap"]
    assert parameter.default is inspect.Parameter.empty


def test_the_accounts_own_reply_is_read_with_no_caller_supplying_an_address(monkeypatch):
    """Without the operator address, every declined invitation becomes firm busy
    time -- and the address is the client's own, never a caller's."""
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [DECLINED_INVITE])
    install_fake_dav_client(monkeypatch, calendars=[calendar])

    (only,) = fetch(make_client()).occurrences
    assert only.participation_status == "DECLINED"


def test_a_bare_login_is_completed_from_the_account_it_is_connected_to(monkeypatch):
    """A login is routinely written without its domain, and an invitation always
    carries one; a stranger's local part must not stand in for it."""
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [DECLINED_INVITE])
    install_fake_dav_client(monkeypatch, calendars=[calendar])
    client = CalDAVCalendarClient(url=URL, username="me", password=PASSWORD)

    (only,) = fetch(client).occurrences
    assert only.participation_status == "DECLINED"


def test_an_attendee_on_a_domain_the_account_does_not_own_is_a_stranger(monkeypatch):
    """`me@othercorp.com` declining is not this account declining."""
    stranger = DECLINED_INVITE.replace("mailto:me@yandex.ru", "mailto:me@othercorp.com")
    calendar = FakeCalendar("Personal", f"{URL}/c/personal/", [stranger])
    install_fake_dav_client(monkeypatch, calendars=[calendar])
    client = CalDAVCalendarClient(url=URL, username="me", password=PASSWORD)

    (only,) = fetch(client).occurrences
    assert only.participation_status is None
