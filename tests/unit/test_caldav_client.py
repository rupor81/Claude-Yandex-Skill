"""Every protocol exception is translated before it can leave `client/`.

The fakes stand in for `caldav.DAVClient`, so no socket is opened.
"""

from __future__ import annotations

import anyio
import caldav
import pytest
from caldav.lib import error as caldav_error
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


class FakeCalendar:
    def __init__(self, name, url):
        self.name = name
        self.url = url


class FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


def install_fake_dav_client(monkeypatch, *, calendars=None, raises=None, on_principal=None):
    """Replace `caldav.DAVClient` with a fake that answers or fails as asked."""

    closed = []

    class FakeDAVClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            if raises is not None:
                raise raises

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            closed.append(True)
            return False

        def principal(self):
            if on_principal is not None:
                raise on_principal
            return FakePrincipal(calendars or [])

    monkeypatch.setattr(caldav, "DAVClient", FakeDAVClient)
    return closed


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
