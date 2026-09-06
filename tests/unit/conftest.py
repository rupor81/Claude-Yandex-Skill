"""Unit-test fixtures.

Two things must never happen in this directory: a network call, and a touch of
the operator's real keychain or config. Both are cut off here for every test.
"""

from __future__ import annotations

import re
import urllib.parse

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


#: Distinguishes "the keyword was not passed" from "it was passed as None".
_UNSET = object()


class FakeObject:
    """One fetched CalDAV object, carrying its raw iCalendar text.

    The three spellings of an ETag are kept apart deliberately, because on the
    real account they disagree: the library's cached ``etag`` attribute is
    empty, the GET response header carries a ``--gzip`` suffix, and only the
    DAV property gives the bare value story 1.7 must send back. A fake that
    made all three the same would let code read the wrong one and still pass.
    """

    def __init__(self, data, *, url=None, etag=None, cached_etag=None,
                 header_etag=None, property_error=None, legacy_property=False):
        self.data = data
        self.url = url
        #: What `caldav` caches on the object; empty on the real account.
        self.etag = cached_etag
        self.headers = {"Etag": header_etag} if header_etag is not None else {}
        self._property_etag = etag
        self._property_error = property_error
        #: An older `caldav` whose `get_property` has no `use_cached` keyword.
        self._legacy_property = legacy_property

    def get_property(self, prop, use_cached=_UNSET, **kwargs):
        """Stand in for a PROPFIND of one DAV property.

        Only the property actually asked for is answered.  A fake that returned
        the ETag whatever it was asked would let code PROPFIND the wrong DAV
        property and still pass, which is precisely the mistake this object
        exists to make visible.
        """
        if self._legacy_property and use_cached is not _UNSET:
            raise TypeError(
                "get_property() got an unexpected keyword argument 'use_cached'"
            )
        if self._property_error is not None:
            raise self._property_error
        from caldav.elements import dav

        if getattr(prop, "tag", None) != dav.GetEtag().tag:
            return None
        return self._property_etag


class FakeCalendar:
    """A calendar collection, as `principal.calendars()` hands them over.

    Objects are addressed the way the real server names them -- one href per
    UID, `<calendar>/<uid>.ics` -- so a caller that tries to *search* for a UID
    instead of addressing it is visible in `searched`.

    An entry of `objects` is either the raw iCalendar text -- stored under the
    href the client would construct for its UID -- or an explicit
    `(href, text)` pair.  The pair form is what makes a mismatch between the
    constructed href and the one the server really used *possible* to test: a
    fake that always derived the href the same way the code does would only
    ever validate the code against itself.
    """

    def __init__(
        self,
        name,
        url,
        objects=(),
        raises=None,
        fetch_raises=None,
        etags=None,
        cached_etags=None,
        header_etags=None,
        property_error=None,
        legacy_property=False,
    ):
        self.name = name
        self.url = url
        self._entries = [
            entry if isinstance(entry, tuple) else (None, entry) for entry in objects
        ]
        self._raises = raises
        self._fetch_raises = fetch_raises
        self._etags = dict(etags or {})
        self._cached_etags = dict(cached_etags or {})
        self._header_etags = dict(header_etags or {})
        self._property_error = property_error
        self._legacy_property = legacy_property
        self.searched = None
        self.fetched = []
        self.asked_by_uid = []

    @property
    def _objects(self):
        return [data for _, data in self._entries]

    def holds(self, href):
        """Whether this collection already has an object at that href."""
        return any(
            self._href_of(entry) is not None and _same_href(href, self._href_of(entry))
            for entry in self._entries
        )

    def add(self, href, data):
        """Store one object, replacing whatever was at that href.

        Replacing rather than appending is deliberate: an unguarded PUT onto an
        occupied href *does* destroy what was there, and a fake that quietly
        kept both copies would let a missing collision guard pass this suite.
        """
        for index, entry in enumerate(self._entries):
            stored = self._href_of(entry)
            if stored is not None and _same_href(href, stored):
                self._entries[index] = (str(href), data)
                return
        self._entries.append((str(href), data))

    def search(self, **kwargs):
        self.searched = kwargs
        if self._raises is not None:
            raise self._raises
        return [FakeObject(data) for data in self._objects]

    def href_for(self, uid):
        base = str(self.url)
        if not base.endswith("/"):
            base += "/"
        return base + urllib.parse.quote(uid, safe="") + ".ics"

    def _href_of(self, entry):
        href, data = entry
        if href is not None:
            return href
        uid = uid_of(data)
        return self.href_for(uid) if uid is not None else None

    def _wrap(self, href, data):
        uid = uid_of(data)
        return FakeObject(
            data,
            url=str(href),
            etag=self._etags.get(uid, f"etag-{uid}"),
            cached_etag=self._cached_etags.get(uid),
            header_etag=self._header_etags.get(uid),
            property_error=self._property_error,
            legacy_property=self._legacy_property,
        )

    def event_by_url(self, href, data=None):
        self.fetched.append(str(href))
        if self._fetch_raises is not None:
            raise self._fetch_raises
        from caldav.lib import error as caldav_error

        for entry in self._entries:
            stored = self._href_of(entry)
            if stored is not None and _same_href(href, stored):
                return self._wrap(href, entry[1])
        raise caldav_error.NotFoundError(url=str(href), reason="Not Found")

    def object_by_uid(self, uid, *args, **kwargs):
        """What `caldav` does when the constructed href misses.

        The library verifies the UID client-side, so this never answers with an
        object holding some other event -- which is why it is not the forbidden
        "search fallback".
        """
        self.asked_by_uid.append(uid)
        if self._fetch_raises is not None:
            raise self._fetch_raises
        from caldav.lib import error as caldav_error

        for entry in self._entries:
            if uid_of(entry[1]) == uid:
                return self._wrap(self._href_of(entry), entry[1])
        raise caldav_error.NotFoundError(url=str(self.url), reason="Not Found")


def uid_of(document):
    """The first UID in an iCalendar document, as text, or None."""
    match = re.search(r"^UID:(.*)$", document, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _same_href(left, right):
    """Compared exactly, byte for byte, as a real server compares a request URL.

    Unquoting both sides first would make every encoding choice the client makes
    correct by construction: the fake would agree with the code because it was
    written from the same assumption, and a href the server never used would
    still be "found".
    """
    return str(left) == str(right)


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

    def event_by_url(self, href, data=None):
        from caldav.lib import error as caldav_error

        for calendar in self._calendars:
            if str(calendar.url).rstrip("/") == str(self.url).rstrip("/"):
                return calendar.event_by_url(href, data)
        raise caldav_error.NotFoundError(url=str(href), reason="Not Found")

    def object_by_uid(self, uid, *args, **kwargs):
        from caldav.lib import error as caldav_error

        for calendar in self._calendars:
            if str(calendar.url).rstrip("/") == str(self.url).rstrip("/"):
                return calendar.object_by_uid(uid, *args, **kwargs)
        raise caldav_error.NotFoundError(url=str(self.url), reason="Not Found")


class FakeResponse:
    """What `DAVClient.put` hands back: a status code, and nothing inferred.

    The status is the only thing the code may read to decide whether a write
    happened; an ETag from a PUT response header is deliberately absent, because
    on this server it is spelled differently from the DAV property a later
    update must send back.
    """

    def __init__(self, status, headers=None):
        self.status = status
        self.headers = dict(headers or {})


def install_fake_dav_client(
    monkeypatch,
    *,
    calendars=None,
    raises=None,
    on_principal=None,
    puts=None,
    put_raises=None,
    put_status=_UNSET,
):
    """Replace `caldav.DAVClient` with a fake that answers or fails as asked.

    Returns a list that gains an entry every time a client is closed, so a
    leaked TLS pool is visible to the test that cares about it.

    Args:
        puts: a list the fake appends one entry to per PUT, so a test can see
            the href, body and headers a write really used -- and see that a
            refused write sent none at all.
        put_raises: raised instead of answering, for a connection lost mid-write
            or a server that refuses the method outright.
        put_status: answered instead of storing anything, for a server that
            refuses this particular write.  Passing it as ``None`` explicitly is
            a response carrying no status at all -- distinct from not passing
            it, which lets the fake answer the write normally.
    """
    import caldav

    closed = []

    def _collection_for(url):
        for calendar in calendars or []:
            base = str(calendar.url)
            if not base.endswith("/"):
                base += "/"
            if str(url).startswith(base):
                return calendar
        return None

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

        def put(self, url, body, headers=None):
            """A PUT, honouring `If-None-Match: *` the way the real server does.

            Without the guard header an occupied href is overwritten and
            answered 204, which is exactly the outcome the guard exists to
            prevent -- so a test can tell the two apart.
            """
            sent = dict(headers or {})
            if puts is not None:
                puts.append({"url": str(url), "body": body, "headers": sent})
            if put_raises is not None:
                raise put_raises
            if put_status is not _UNSET:
                return FakeResponse(put_status)
            collection = _collection_for(url)
            if collection is None:
                return FakeResponse(404)
            if collection.holds(url):
                if sent.get("If-None-Match") == "*":
                    return FakeResponse(412)
                collection.add(url, body)
                return FakeResponse(204)
            collection.add(url, body)
            return FakeResponse(201)

    monkeypatch.setattr(caldav, "DAVClient", FakeDAVClient)
    return closed
