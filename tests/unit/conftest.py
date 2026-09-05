"""Unit-test fixtures.

Two things must never happen in this directory: a network call, and a touch of
the operator's real keychain or config. Both are cut off here for every test.
"""

from __future__ import annotations

import pytest
from yandex_core import config as config_module


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point config and the credential fallback file at a temporary directory."""
    monkeypatch.setenv(config_module.CONFIG_DIR_ENV_VAR, str(tmp_path / "config"))
    monkeypatch.delenv(config_module.PROFILE_ENV_VAR, raising=False)
    return tmp_path / "config"


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    """Make the keychain look empty and unwritable, so tests use the fallback."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)

    def refuse(*args, **kwargs):
        raise RuntimeError("no keyring backend in tests")

    monkeypatch.setattr(keyring, "set_password", refuse)
    monkeypatch.setattr(keyring, "delete_password", refuse)


@pytest.fixture(autouse=True)
def no_credential_env(monkeypatch):
    monkeypatch.delenv("YANDEX_MCP_CALENDAR_DEFAULT_PASSWORD", raising=False)


# -- one set of CalDAV fakes, shared -------------------------------------
#
# These stand in for `caldav.DAVClient` so no socket is ever opened. They are
# here rather than copied into each test file because a fake that drifts from
# its twin is a test that passes for a reason the other one does not.


class FakeObject:
    """One fetched CalDAV object, carrying its raw iCalendar text."""

    def __init__(self, data):
        self.data = data


class FakeCalendar:
    """A calendar collection, as `principal.calendars()` hands them over."""

    def __init__(self, name, url, objects=(), raises=None):
        self.name = name
        self.url = url
        self._objects = list(objects)
        self._raises = raises
        self.searched = None

    def search(self, **kwargs):
        self.searched = kwargs
        if self._raises is not None:
            raise self._raises
        return [FakeObject(data) for data in self._objects]


class FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


class FakeAddressedCalendar:
    """What real `caldav` returns from `DAVClient.calendar(url=...)`.

    The real library constructs a `Calendar` from the URL alone: no request is
    made, so it has no display name and cannot know whether the collection
    exists. Both of those only become apparent from the query that follows,
    which is exactly what this fake reproduces -- a fake that pre-set the name,
    or raised "not found" itself, would be testing itself rather than the code.
    """

    name = None

    def __init__(self, url, calendars):
        self.url = url
        self._calendars = calendars

    def search(self, **kwargs):
        from caldav.lib import error as caldav_error

        for calendar in self._calendars:
            if str(calendar.url).rstrip("/") == str(self.url).rstrip("/"):
                return calendar.search(**kwargs)
        raise caldav_error.NotFoundError(url=str(self.url), reason="Not Found")


def install_fake_dav_client(
    monkeypatch, *, calendars=None, raises=None, on_principal=None
):
    """Replace `caldav.DAVClient` with a fake that answers or fails as asked.

    Returns a list that gains an entry every time a client is closed, so a
    leaked TLS pool is visible to the test that cares about it.
    """
    import caldav

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

        def calendar(self, url=None):
            return FakeAddressedCalendar(url, calendars or [])

    monkeypatch.setattr(caldav, "DAVClient", FakeDAVClient)
    return closed
