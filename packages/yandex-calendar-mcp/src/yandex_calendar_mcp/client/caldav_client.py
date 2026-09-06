"""CalDAV access, and the only place CalDAV exceptions exist.

This module imports no ``mcp`` and is usable from a plain script.  The ``caldav``
library is blocking, so the single thread hop lives here -- ``tools/`` sees only
awaitables.

Two translation hazards are handled deliberately:

* ``caldav`` raises ``AuthorizationError`` for 401 *and* 403.  Yandex 360 answers
  403 when an administrator has disabled app passwords, which is an organisation
  policy problem and not a wrong password.  The status code on the attached
  response separates them when there is one; the reason phrase is the fallback,
  and when neither can settle it the caller is told so rather than guessed at.
* ``caldav`` does **not** wrap transport failures.  Connection resets, DNS
  failures and timeouts arrive as ``niquests`` exceptions and escape this module
  unless caught explicitly.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime

import anyio.to_thread
import caldav
from caldav.elements import dav
from caldav.lib import error as caldav_error
from yandex_core.errors import (
    AuthError,
    Conflict,
    NotFound,
    PolicyError,
    ProtocolError,
    RateLimited,
    TransportError,
    YandexError,
)

try:  # pragma: no cover - import shape depends on the installed caldav
    from niquests import exceptions as http_error
except ImportError:  # pragma: no cover
    from requests import exceptions as http_error  # type: ignore[no-redef]

from .recurrence import (
    CalendarSource,
    EventNotInDocument,
    EventRecord,
    Expansion,
    InstanceNotInSeries,
    SortKey,
)
from .recurrence import DEFAULT_CEILING as EXPANSION_CEILING
from .recurrence import expand as expand_occurrences
from .recurrence import read_event
from .recurrence import with_unreadable_calendars
from .compose import EventDraft, build_event_document, new_uid, written_boundary

__all__ = [
    "CalendarRef",
    "CalDAVCalendarClient",
    "CreatedEvent",
    "FetchedEvent",
    "EXPANSION_CEILING",
]

_APP_PASSWORD_HINT = (
    "Yandex CalDAV rejects OAuth tokens; the credential must be an app password "
    "created in Yandex ID."
)


@dataclass(frozen=True, slots=True)
class CalendarRef:
    """One calendar as the wire describes it: a display name and its URL."""

    name: str
    url: str


@dataclass(frozen=True, slots=True)
class FetchedEvent:
    """One event as the server holds it, and the ETag that version has.

    ``etag`` is read as a DAV property in its own right.  It is deliberately
    *not* the library's cached attribute (empty on this server) and *not* the
    GET response header (which carries a ``--gzip`` suffix the property does
    not).  A later conditional update sends this value back, so the two
    spellings must never be mixed: they would make the precondition fail on an
    event nobody had touched.  ``None`` means the server supplied none, and is
    never filled in with a guess.

    When the event was read from more than one CalDAV object, the ETag is the
    one belonging to the object addressed by its UID -- the object a later
    conditional update would be sent to.
    """

    record: EventRecord
    etag: str | None

    etag_unreadable: bool = False
    """True when the ETag property existed to be read and reading it failed.

    Different from ``etag is None`` alone: "the server supplied none" and "this
    server could not read it" call for different next steps, and a failure to
    read one must never turn a fetched event into a missing one.
    """


@dataclass(frozen=True, slots=True)
class CreatedEvent:
    """One event that now exists on the server, and what is known about it.

    ``record`` is what the server *holds*, read back after the write rather than
    echoed from the request: this server adjusts stored values, and a caller
    told what it asked for has been told nothing.  It is ``None`` only when the
    readback itself failed, which is a smaller loss than it looks: the event
    exists either way, and ``readback_error`` says what could not be confirmed.

    ``href`` is the object the readback actually found.  It is the constructed
    ``<calendar>/<uid>.ics`` when that is where the object is, and the href the
    server really filed it under when the by-UID fallback found it elsewhere --
    which is what a later conditional update must be aimed at.  When the
    readback failed there was nothing to observe, so it is the constructed one
    and ``readback_error`` says the object was never seen.

    ``sent_start`` and ``sent_end`` are the boundaries as they were *written*,
    not as they were asked for: composing a document truncates microseconds, and
    comparing the stored values against the caller's originals would blame the
    server for an edit this code made.
    """

    uid: str
    href: str
    calendar_url: str
    calendar_name: str
    etag: str | None
    sent_start: date | datetime
    sent_end: date | datetime
    etag_unreadable: bool = False
    record: EventRecord | None = None
    readback_error: str | None = None


class CalDAVCalendarClient:
    """A blocking CalDAV connection, exposed as async methods.

    ``DAVClient`` is constructed directly rather than via ``get_davclient()``:
    the factory consults ``CALDAV_*`` environment variables and a config file of
    its own, can return ``None``, and would read credentials outside
    ``yandex_core.credentials``.
    """

    def __init__(
        self,
        *,
        url: str,
        username: str,
        password: str,
        credential_name: str = "calendar app password",
        timeout: int = 30,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._credential_name = credential_name
        self._timeout = timeout

    async def list_calendars(self) -> list[CalendarRef]:
        """Every calendar on the principal, in the order the server returns them."""
        return await anyio.to_thread.run_sync(self._list_calendars_blocking)

    async def list_occurrences(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_url: str | None = None,
        ceiling: int = EXPANSION_CEILING,
        after: SortKey | None = None,
        overlap: bool = False,
    ) -> Expansion:
        """Concrete occurrences between ``start`` and ``end``.

        The only server-side filter used is CalDAV's ``time-range``: it is the
        one Yandex can be relied on for.  There is deliberately no text
        parameter -- ``text-match`` cannot be shown never to under-return, and a
        short answer that looks complete is the failure mode this project exists
        to avoid.  Series are expanded here rather than by the server, because
        an occurrence the server declines to expand is simply invisible.

        Args:
            start: inclusive, timezone-aware.
            end: exclusive, timezone-aware.
            calendar_url: one calendar, or every calendar when omitted.
            ceiling: most occurrences to return before reporting truncation,
                counted over what remains after ``after``.
            after: resume strictly after this sort key, so a caller paging into
                a truncated tail can always make progress.
            overlap: return occurrences that merely overlap the range as well as
                those starting inside it.  A question about busy time needs the
                meeting that began last night and is still running.

        Each occurrence also carries whether it consumes time at all and the
        configured account's own reply to it.  The address that decides "own"
        is the profile's login, which this client was constructed with, so no
        caller has to supply -- or could substitute -- somebody else's.

        A calendar that cannot be read -- a 403 on one shared collection, say --
        is counted in ``unreadable_calendars`` and the others are still
        returned.  When *every* calendar fails there is nothing to return, and
        the failure is raised rather than dressed up as an empty success.
        """

        def run() -> Expansion:
            return self._list_occurrences_blocking(
                start=start,
                end=end,
                calendar_url=calendar_url,
                ceiling=ceiling,
                after=after,
                overlap=overlap,
            )

        return await anyio.to_thread.run_sync(run)

    async def get_event(
        self,
        *,
        uid: str,
        recurrence_id: date | datetime | None = None,
        calendar_url: str | None = None,
    ) -> FetchedEvent:
        """One event, in full, addressed by its UID.

        The object is *addressed*, never searched for.  Measured against the
        live account, a CalDAV UID search answers with every object in the
        calendar -- 1759 of them for one UID -- while looking like a filtered
        query, so a search-based lookup would confidently return the wrong
        meeting.  The href is built the way the server names it,
        ``<calendar>/<uid>.ics``, and a 404 is an honest miss.

        Args:
            uid: the event to fetch.
            recurrence_id: which instance of a series, or ``None`` for the
                series itself.
            calendar_url: one calendar, or every calendar until it is found.

        Raises:
            NotFound: nothing on the account holds that UID -- or the UID was
                found and the series has no such instance, which the message
                distinguishes.  When a calendar could not be read during the
                search, the message says the search was incomplete rather than
                asserting the event is not there.
        """

        def run() -> FetchedEvent:
            return self._get_event_blocking(
                uid=uid, recurrence_id=recurrence_id, calendar_url=calendar_url
            )

        return await anyio.to_thread.run_sync(run)

    async def create_event(
        self,
        *,
        calendar_url: str,
        summary: str,
        start: date | datetime,
        end: date | datetime,
        description: str | None = None,
        location: str | None = None,
    ) -> CreatedEvent:
        """Write one new event into one named calendar, and read it back.

        The calendar is named by the caller and never chosen here -- required by
        this layer in its own right, not only by the tool above it.  This module
        is usable from a plain script, and a script that named no calendar had
        the account's first one picked for it, which on this account is the
        operator's personal calendar.  The URL that
        names it is only a *selector*: the address written to is the calendar's
        own href as the principal's listing gives it.  Measured on the live
        account, a URL this server returns from creating a calendar is not that
        calendar's address -- writes aimed at it went elsewhere and a delete
        aimed at it answered success while removing nothing -- so a URL the
        listing does not know is a not-found here, before anything is written.

        The write carries ``If-None-Match: *``: the server itself refuses to
        replace an object that is already at that href.  A guard made of a
        prior read would have a gap between the read and the write, and this one
        does not.

        Args:
            calendar_url: which calendar, from ``list_calendars``.
            summary: the event's title; an untitled event is refused.
            start: timezone-aware, or a date for an all-day event.
            end: exclusive, in the same form as ``start``.
            description: invitation body, or ``None``.
            location: where it is, or ``None``.

        Returns:
            What was created, with the values the *server* now holds and the
            ETag of the stored object.  When the write succeeded but the event
            could not be read back, the record is ``None`` and
            ``readback_error`` says why: an event that exists is never reported
            as a failure.

        Raises:
            ProtocolError: ``calendar_url`` is missing or blank, or the composed
                event is not one that can be written. Nothing was written.
            NotFound: ``calendar_url`` is not a calendar on this account.
                Nothing was written.
            Conflict: an object already exists at the event's href; nothing was
                replaced.
            PolicyError: the calendar refused the write.
            TransportError: the connection failed. When it failed *during* the
                write, the message says the outcome is unknown and names the
                UID to check, because a blind retry could create the event a
                second time.
        """
        if not isinstance(calendar_url, str) or not calendar_url.strip():
            raise ProtocolError(
                "`calendar_url` is required: no calendar is chosen for you. This "
                "account has several and the server marks none of them as the "
                "default, so an event written into a guessed one would be "
                "somewhere nobody looks. Take the URL from `list_calendars`."
            )

        draft = EventDraft(
            uid=new_uid(),
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
        )
        # Composed before the connection is opened, so a draft that cannot be
        # written is refused without a request being made at all.
        document = build_event_document(draft)

        def run() -> CreatedEvent:
            return self._create_event_blocking(
                draft=draft, document=document, calendar_url=calendar_url
            )

        return await anyio.to_thread.run_sync(run)

    # -- blocking half -----------------------------------------------------

    def _list_calendars_blocking(self) -> list[CalendarRef]:
        with self._translated():
            # The client owns a TLS connection pool, so it is closed here rather
            # than left to the garbage collector once per call.
            with caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
                timeout=self._timeout,
            ) as client:
                principal = client.principal()
                return [
                    CalendarRef(name=_display_name(calendar), url=str(calendar.url))
                    for calendar in principal.calendars()
                ]

    def _list_occurrences_blocking(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_url: str | None,
        ceiling: int,
        after: SortKey | None,
        overlap: bool,
    ) -> Expansion:
        sources: list[CalendarSource] = []
        unreadable_calendars = 0

        with self._translated():
            with caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
                timeout=self._timeout,
            ) as client:
                calendars, _ = self._calendars_for(client, calendar_url)

                first_failure: Exception | None = None
                for calendar in calendars:
                    url = str(getattr(calendar, "url", "") or calendar_url or "")
                    try:
                        name = _display_name(calendar)
                        objects = list(
                            calendar.search(
                                start=start, end=end, event=True, expand=False
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        # One unreadable calendar is a counted loss, exactly as
                        # one unreadable document is. Aborting the whole fetch
                        # for it would throw away every other calendar's answer.
                        unreadable_calendars += 1
                        if first_failure is None:
                            first_failure = exc
                        continue
                    for obj in objects:
                        sources.append(
                            CalendarSource(
                                ics=_object_data(obj), calendar_url=url, calendar_name=name
                            )
                        )

                if calendars and unreadable_calendars == len(calendars):
                    # Nothing survived. An empty page here would read as "your
                    # calendar is empty", which is the one answer we refuse.
                    assert first_failure is not None
                    raise first_failure

        # Expansion is pure and needs no connection, so it happens after the
        # client is closed -- but still inside client/, so no RRULE escapes.
        expansion = expand_occurrences(
            sources,
            start=start,
            end=end,
            ceiling=ceiling,
            after=after,
            operator=self._username,
            operator_domains=self._account_domains(),
            overlap=overlap,
        )
        return with_unreadable_calendars(expansion, unreadable_calendars)

    def _account_domains(self) -> tuple[str, ...]:
        """The mail domains this account owns, for a login written without one.

        A login is routinely just ``me``, while an invitation always names a
        full address, so the missing half has to come from somewhere.  It comes
        from the account this client is connected to -- the login's own domain
        when it has one, and otherwise the host being talked to, with a leading
        service label dropped so ``caldav.example.com`` yields ``example.com``.
        Nothing is hard-coded: an account on a custom domain resolves to its own
        domain rather than to somebody else's, and an attendee outside these
        domains stays a stranger whose reply is not this account's.
        """
        login = (self._username or "").strip().casefold()
        _, _, own_domain = login.partition("@")
        if own_domain:
            return (own_domain,)
        host = urllib.parse.urlsplit(self._url).hostname or ""
        host = host.strip().casefold().strip(".")
        labels = host.split(".")
        if len(labels) > 2:
            labels = labels[1:]
        derived = ".".join(labels)
        return (derived,) if "." in derived else ()

    def _get_event_blocking(
        self,
        *,
        uid: str,
        recurrence_id: date | datetime | None,
        calendar_url: str | None,
    ) -> FetchedEvent:
        unreadable_calendars = 0
        tried = 0
        first_calendar_failure: Exception | None = None
        # A document that will not parse, or an event that is not usable, is a
        # fault in the data rather than a missing event. It is remembered and
        # raised only if nothing else answered -- otherwise one corrupt invite
        # in the first calendar would hide a perfectly good event in the second.
        first_document_failure: ProtocolError | None = None

        with self._translated():
            with caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
                timeout=self._timeout,
            ) as client:
                calendars, unlisted = self._calendars_for(client, calendar_url)
                for calendar in calendars:
                    tried += 1
                    url = str(getattr(calendar, "url", "") or calendar_url or "")
                    try:
                        sources, etag, etag_unreadable, _found_at = _fetch_sources(
                            calendar,
                            url=url,
                            uid=uid,
                            # An override is frequently an object of its own, so
                            # the second address is worth a request whenever the
                            # answer depends on one.
                            gather_overrides=recurrence_id is not None,
                        )
                    except YandexError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        if _is_transport_failure(exc) or _is_credential_failure(exc):
                            # Not a property of this one calendar: every other
                            # calendar would fail the same way, and calling the
                            # event missing would send the caller looking for an
                            # event that is really there.
                            raise
                        unreadable_calendars += 1
                        if first_calendar_failure is None:
                            first_calendar_failure = exc
                        continue

                    if not sources:
                        # This calendar does not hold it. That is a miss, not a
                        # failure: the next calendar may.
                        continue

                    try:
                        record = read_event(
                            sources, uid=uid, recurrence_id=recurrence_id
                        )
                    except EventNotInDocument:
                        # An href can land on a document holding some other
                        # event. Still a miss for this UID.
                        continue
                    except InstanceNotInSeries:
                        raise NotFound(_no_such_instance(uid, recurrence_id)) from None
                    except ProtocolError as exc:
                        unreadable_calendars += 1
                        if first_document_failure is None:
                            first_document_failure = exc
                        continue
                    return FetchedEvent(
                        record=record, etag=etag, etag_unreadable=etag_unreadable
                    )

                if tried and unreadable_calendars == tried:
                    # Nothing survived, so nothing was learned about the event.
                    # Reporting it missing would be an assertion never verified
                    # -- the single named calendar that answered 403 being the
                    # case that matters most.
                    if first_document_failure is not None:
                        raise first_document_failure
                    assert first_calendar_failure is not None
                    raise first_calendar_failure

        if first_document_failure is not None:
            raise first_document_failure
        raise NotFound(
            _no_such_event(
                uid,
                tried=tried,
                unreadable_calendars=unreadable_calendars,
                calendar_url=calendar_url,
                unlisted=unlisted,
            )
        )

    def _create_event_blocking(
        self,
        *,
        draft: EventDraft,
        document: str,
        calendar_url: str,
    ) -> CreatedEvent:
        with self._translated():
            with caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
                timeout=self._timeout,
                # ``DAVClient.request`` catches 429 and 503, sleeps, and
                # re-issues the same request -- PUT included. If the first
                # attempt landed, the retry meets the guard and answers 412, and
                # this code would report "nothing was created" for an event that
                # exists. The rule is "never retry a write blindly", so the
                # retry is turned off here rather than trusted not to fire.
                rate_limit_handle=False,
            ) as client:
                calendars, unlisted = self._calendars_for(client, calendar_url)
                if unlisted or not calendars:
                    # Nothing is written to a URL the account does not list. A
                    # write aimed at a URL that is not a calendar does not fail
                    # loudly on this server; it goes somewhere nobody can find.
                    raise NotFound(_not_a_calendar(calendar_url))
                calendar = calendars[0]
                # The listing's own URL, never the caller's string: the two
                # differ on this server, and the difference is where a write
                # goes missing.
                real_url = str(getattr(calendar, "url", "") or calendar_url)
                name = _display_name(calendar)
                href = _object_href(real_url, draft.uid)

                try:
                    response = client.put(  # type: ignore[attr-defined]
                        href,
                        document,
                        {
                            "Content-Type": "text/calendar; charset=utf-8",
                            # The guard is the write's own, so there is no gap
                            # between checking and writing for anybody to slip
                            # through.
                            "If-None-Match": "*",
                        },
                    )
                except http_error.RequestException as exc:
                    # The request left this process. Whether the server acted on
                    # it is unknown, and a retry could create it twice.
                    raise TransportError(
                        _write_outcome_unknown(draft.uid, calendar_url=real_url, exc=exc)
                    ) from exc
                except caldav_error.RateLimitError as exc:
                    # The library's own retry is off, so this reaches here on
                    # the first refusal. A 429 usually means nothing was stored,
                    # but "usually" is not knowledge, and the wrong guess here
                    # is a second copy of somebody's meeting.
                    raise RateLimited(
                        _write_outcome_unknown(
                            draft.uid,
                            calendar_url=real_url,
                            exc=exc,
                            what="Yandex answered the write by rate limiting it",
                        )
                    ) from exc
                except caldav_error.AuthorizationError as exc:
                    raise self._write_refused(exc, calendar=real_url, name=name) from exc

                _check_write_status(
                    _status_of(response), uid=draft.uid, href=href, calendar=real_url
                )

                # Success is not claimed from the write's own answer. It is
                # confirmed by reading the object back -- through the same
                # reader every other event goes through, so a created event and
                # a read one cannot describe themselves differently.
                record: EventRecord | None = None
                etag: str | None = None
                etag_unreadable = False
                readback_error: str | None = None
                try:
                    sources, etag, etag_unreadable, found_at = _fetch_sources(
                        calendar, url=real_url, uid=draft.uid, gather_overrides=False
                    )
                    if found_at:
                        # Where the object really is, which is not always the
                        # href this code built: a later conditional update has
                        # to be aimed at the one the server used.
                        href = found_at
                    if not sources:
                        readback_error = (
                            "the server accepted the write but did not return the "
                            "object when it was read back"
                        )
                    else:
                        record = read_event(sources, uid=draft.uid)
                except Exception as exc:  # noqa: BLE001 - reported, or re-raised
                    if _is_transport_failure(exc) or isinstance(
                        exc, caldav_error.AuthorizationError
                    ):
                        # Not a fact about the readback: the account itself has
                        # become unusable, and every later call fails the same
                        # way. Reporting it as an unexplained note under a
                        # successful create hides the one thing that needs
                        # fixing -- so it is raised as itself, saying plainly
                        # that the event was nonetheless created.
                        raise self._readback_broke_off(
                            exc, uid=draft.uid, calendar=real_url
                        ) from exc
                    # Otherwise: the event exists. The server said so and
                    # nothing here can unsay it. Failing now would send somebody
                    # to create it a second time.
                    record = None
                    etag, etag_unreadable = None, False
                    readback_error = f"the readback failed ({type(exc).__name__}: {exc})"

                return CreatedEvent(
                    uid=draft.uid,
                    href=href,
                    calendar_url=real_url,
                    calendar_name=name,
                    etag=etag,
                    sent_start=written_boundary(draft.start),
                    sent_end=written_boundary(draft.end),
                    etag_unreadable=etag_unreadable,
                    record=record,
                    readback_error=readback_error,
                )

    def _readback_broke_off(
        self, exc: BaseException, *, uid: str, calendar: str
    ) -> Exception:
        """A readback that failed for a reason bigger than the readback.

        The taxonomy class is the one the failure really is -- a rejected
        credential is an ``AuthError``, an unreachable host a ``TransportError``
        -- so a caller that branches on the type is not told the wrong thing.
        The message carries the one fact that must not be lost with it: the
        event was created, and creating it again would make two.
        """
        translated = self._translated()._translate(exc)
        return type(translated)(
            f"Event {uid!r} WAS created in {calendar} -- the server accepted the "
            f"write -- but reading it back failed: {translated} Its stored "
            "values are therefore unknown. Do not create it again; read it with "
            f"`calendar_event_get` for uid {uid!r} once the cause is fixed."
        )

    def _write_refused(
        self, exc: BaseException, *, calendar: str, name: str
    ) -> Exception:
        """A refused write, told apart from a refused account.

        A 403 on one PUT is a calendar this account may read and not write --
        a subscribed or shared collection. Reporting it as the organisation
        policy that disables app passwords would send an operator to an
        administrator who can do nothing, so this one names the calendar
        instead. A 401, and a refusal that cannot be classified, stay what they
        are: the translator says the credential may be the cause, and that must
        not be softened into a fact about one collection.
        """
        try:
            forbidden = _is_forbidden(exc)
        except _Undecidable:
            return exc
        if not forbidden:
            return exc
        return PolicyError(
            f"The calendar {name!r} at {calendar} refused the write with 403. "
            "The credential was accepted, so this is a permission on that "
            "calendar -- a shared or subscribed collection this account may "
            "read but not write to. Nothing was created. Create the event in a "
            "calendar this account owns, from `calendar_list`."
        )

    def _calendars_for(
        self, client: object, calendar_url: str | None
    ) -> tuple[list, bool]:
        """The calendars one query covers, named or all of them.

        A named calendar is looked up in the principal's own listing so that it
        carries the same display name an all-calendars query would give it;
        labelling the same events differently depending on how they were asked
        for is a difference the caller cannot explain. A URL the listing does
        not know is still addressed directly, so the failure comes from the
        query against the server rather than from a guess made here.

        Returns:
            the calendars to query, and whether the named URL was absent from
            the principal's own listing -- which is what separates "that URL is
            not a calendar on this account" from "the event is not in it".
        """
        principal_calendars = list(client.principal().calendars())  # type: ignore[attr-defined]
        if calendar_url is None:
            return principal_calendars, False
        wanted = _normalised_url(calendar_url)
        for calendar in principal_calendars:
            if _normalised_url(str(getattr(calendar, "url", ""))) == wanted:
                return [calendar], False
        return [client.calendar(url=calendar_url)], True  # type: ignore[attr-defined]

    def _translated(self) -> "_Translator":
        return _Translator(self._credential_name, self._url)


def _object_href(calendar_url: str, uid: str) -> str:
    """The URL this server names an object by: ``<calendar>/<uid>.ics``.

    Building the href is what makes this a fetch rather than a search.  A UID
    that the server happens to store under some other href is a miss here, and
    a miss is the right answer: the alternative is a UID search, which on this
    server returns the whole calendar and would answer with the wrong event.
    """
    base = calendar_url if calendar_url.endswith("/") else calendar_url + "/"
    return base + urllib.parse.quote(uid, safe="") + ".ics"


def _fetch_sources(
    calendar: object,
    *,
    url: str,
    uid: str,
    gather_overrides: bool,
) -> tuple[list[CalendarSource], str | None, bool, str | None]:
    """Every object in one calendar that can be addressed for this ``UID``.

    The object is *addressed*, never searched for: the href the server names it
    by is built, and a 404 is an honest miss.  Two things make one request
    insufficient:

    * The constructed href encodes the UID, and a UID containing ``@`` -- common
      on this server -- may be stored under an href that does not match.  On a
      miss the library's ``object_by_uid`` is asked instead.  It verifies the
      UID client-side (it raises ``NotFoundError`` for a fabricated one), so it
      never answers with an unverified event, which is what makes it different
      from the UID *search* this module refuses: that search returns the entire
      calendar on this server while looking like a filtered query.
    * A ``RECURRENCE-ID`` override is frequently an object of its own.  When the
      answer depends on one, both addresses are asked and the documents are
      read together, so a moved instance is not returned at the series' time.

    Returns:
        the documents found, the ETag of the addressed object, whether reading
        that ETag failed, and the href that object was actually found at --
        which is the constructed one when the constructed one answered, and the
        server's own when the by-UID fallback did.
    """
    sources: list[CalendarSource] = []
    hrefs: set[str] = set()
    etag: str | None = None
    etag_unreadable = False
    found_at: str | None = None
    name: str | None = None

    def keep(obj: object) -> None:
        # The display name is read only once something was found: on this server
        # it can cost a request of its own, and a calendar that does not hold
        # the event should not be asked for its name during a scan.
        nonlocal name
        href = str(getattr(obj, "url", "") or "")
        if href and href in hrefs:
            return
        hrefs.add(href)
        if name is None:
            name = _display_name(calendar)
        sources.append(
            CalendarSource(
                ics=_object_data(obj), calendar_url=url, calendar_name=name
            )
        )

    try:
        addressed = calendar.event_by_url(_object_href(url, uid))  # type: ignore[attr-defined]
    except caldav_error.NotFoundError:
        addressed = None
    if addressed is not None:
        keep(addressed)
        etag, etag_unreadable = _etag_of(addressed)
        found_at = str(getattr(addressed, "url", "") or "") or _object_href(url, uid)

    if addressed is None or gather_overrides:
        other = _object_by_uid(calendar, uid)
        if other is not None:
            before = len(sources)
            keep(other)
            if addressed is None and len(sources) > before:
                etag, etag_unreadable = _etag_of(other)
                found_at = str(getattr(other, "url", "") or "") or None

    return sources, etag, etag_unreadable, found_at


def _object_by_uid(calendar: object, uid: str) -> object | None:
    """The library's UID lookup, which verifies the UID before answering.

    A failure here is never fatal: it is a second address for an object the
    caller may already have, so an unsupported or unhappy lookup simply yields
    nothing.  A rejected credential or an unreachable host still escapes, since
    neither is a fact about this UID.
    """
    finder = getattr(calendar, "object_by_uid", None)
    if finder is None:
        return None
    try:
        return finder(uid)
    except caldav_error.NotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        if _is_transport_failure(exc) or _is_credential_failure(exc):
            raise
        return None


def _etag_of(obj: object) -> tuple[str | None, bool]:
    """The ETag as a DAV property, and whether reading it failed.

    Read with ``use_cached=False`` on purpose.  The library's cached attribute
    is empty on this server, and the GET response header carries a ``--gzip``
    suffix the property does not; a later update sends this value back as a
    precondition, so reading either of the other two would make that check fail
    on an event nobody had touched.  A blank value is no value: it is reported
    as absent rather than passed on as though it were a version.

    A failure to read the property is *not* a missing event.  The event has
    already been fetched; it is returned with no ETag and the answer says the
    ETag could not be read, which is a different fact from the server having
    supplied none.
    """
    try:
        value = obj.get_property(dav.GetEtag(), use_cached=False)  # type: ignore[attr-defined]
    except TypeError:
        # An installed caldav whose `get_property` has no `use_cached` keyword.
        # Losing the event over a signature change would be absurd.
        try:
            value = obj.get_property(dav.GetEtag())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None, True
    except Exception:  # noqa: BLE001 - including a missing method entirely
        return None, True
    if value is None:
        return None, False
    text = str(value).strip()
    return (text or None), False


def _is_transport_failure(exc: BaseException) -> bool:
    return isinstance(exc, http_error.RequestException)


def _is_credential_failure(exc: BaseException) -> bool:
    """Whether this failure is about the account rather than one collection.

    A 401 answers the same way for every calendar, so counting it as one
    unreadable collection and carrying on would turn a wrong password into
    "that event does not exist".  A 403 is left as a per-calendar loss: one
    shared collection the account may no longer open is exactly the case the
    search is meant to survive.
    """
    if not isinstance(exc, caldav_error.AuthorizationError):
        return False
    try:
        return not _is_forbidden(exc)
    except _Undecidable:
        # Undecidable means it may be a rejected password, and that must not be
        # reported as a missing event.
        return True


def _no_such_event(
    uid: str,
    *,
    tried: int,
    unreadable_calendars: int,
    calendar_url: str | None,
    unlisted: bool = False,
) -> str:
    """The message for a UID nothing on the account holds.

    A miss after a partial search is not the same as a miss.  If a calendar
    errored while the account was scanned, the event may be in the one that
    failed, and saying "not found" would assert something never verified.  A URL
    that is not one of the account's calendars is not a miss at all, and telling
    the caller their event does not exist would send them hunting for a meeting
    that is really there under a URL they mistyped.
    """
    if unlisted:
        return (
            f"{calendar_url!r} is not one of the calendars this account lists, "
            "and addressing it directly returned nothing, so it may not name a "
            "calendar at all. Nothing was "
            f"established about event {uid!r}. Use `calendar_list` to get the "
            "URL of a calendar on this account, or omit `calendar_url` to try "
            "them all."
        )
    where = (
        f"the calendar at {calendar_url}"
        if calendar_url is not None
        else f"any of the {tried} calendars on this account"
    )
    if unreadable_calendars:
        return (
            f"No event with UID {uid!r} was found, but the search was "
            f"incomplete: {unreadable_calendars} of {tried} calendars could not "
            "be read, so the event may be in one of those. Retry, or name the "
            "calendar with `calendar_url`."
        )
    return f"No event with UID {uid!r} exists in {where}."


def _not_a_calendar(calendar_url: str) -> str:
    """The message for a write aimed at a URL the account does not list."""
    return (
        f"{calendar_url!r} is not one of the calendars this account lists, so "
        "nothing was created. A URL this server hands back is not always the "
        "address of the thing it names, and a write aimed at one that is not a "
        "calendar is not reliably refused -- it simply goes somewhere nothing "
        "will find it. Use `calendar_list` to get the URL of a calendar on this "
        "account."
    )


def _write_outcome_unknown(
    uid: str,
    *,
    calendar_url: str,
    exc: BaseException,
    what: str = "The connection failed",
) -> str:
    """The message for a write whose fate nobody knows.

    The one thing that must not happen next is a blind retry: if the server did
    act on the request, retrying creates the meeting twice, and two identical
    meetings on somebody's calendar is a mess a caller cannot undo without being
    told which one is which.
    """
    return (
        f"{what} while creating event {uid!r} in {calendar_url} "
        f"({type(exc).__name__}), so the outcome is unknown: the event may or "
        "may not have been created. Do not retry blindly -- check first with "
        f"`calendar_event_get` for uid {uid!r}, and create it again only if it "
        "is not there."
    )


def _status_of(response: object) -> int | None:
    """The numeric status of a write response, or ``None`` when it gave none."""
    for field in ("status", "status_code"):
        value = getattr(response, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _check_write_status(
    status: int | None, *, uid: str, href: str, calendar: str
) -> None:
    """Turn a write's status into either silence or the taxonomy.

    Only 201 is a creation.  A 2xx is *not* good enough: on a PUT, 200 and 204
    both mean an object already at that href was **replaced** -- exactly the
    outcome ``If-None-Match: *`` exists to prevent -- so a server that ignored
    the guard would otherwise have this code report "created" over a meeting it
    had just destroyed.  There is no 403 branch: ``caldav`` raises
    ``AuthorizationError`` for 401 and 403 before a response is ever returned,
    so a second, weaker message here could only compete with the one
    :meth:`CalDAVCalendarClient._write_refused` gives.

    Raises:
        Conflict: 412 or 409 -- the guard held and something is already there.
        NotFound: 404 from a calendar the principal listed a moment ago.
        ProtocolError: 200 or 204 (a replacement), and any other answer,
            including one with no status at all: "the server said nothing" is
            not "the event was created".
    """
    if status == 201:
        return
    if status in (409, 412):
        raise Conflict(
            f"An object already exists at {href}, so event {uid!r} was not "
            f"created and nothing was replaced (the write carried a guard that "
            "refuses to overwrite). Create it again to be given a new "
            "identifier."
        )
    if status in (200, 204):
        raise ProtocolError(
            f"Yandex answered the write of event {uid!r} with {status}, which on "
            "a PUT means an object already at that href was REPLACED, not "
            "created. The write carried a guard forbidding exactly that, so the "
            "server ignored it: something that was at "
            f"{href} may have been destroyed, and this server will not report "
            f"that as a creation. Read {href} with `calendar_event_get` for uid "
            f"{uid!r} to see what is there now."
        )
    if status == 404:
        raise NotFound(
            f"Yandex answered the write of event {uid!r} with 404, so nothing "
            f"was created. The calendar at {calendar} was in this account's own "
            "listing a moment before the write, so this is not a URL that was "
            "never a calendar: it has most likely been removed or renamed since "
            "it was listed. Re-read `calendar_list` and create the event in a "
            "calendar that is still there."
        )
    raise ProtocolError(
        f"Yandex answered the write of event {uid!r} with "
        f"{status if status is not None else 'no status at all'}, which this "
        "server cannot read as success. The event may or may not exist: check "
        f"with `calendar_event_get` for uid {uid!r} before trying again."
    )


def _no_such_instance(uid: str, recurrence_id: date | datetime | None) -> str:
    """The message for a UID that exists without the instance that was asked for."""
    return (
        f"Event {uid!r} exists, but it has no instance at "
        f"{recurrence_id.isoformat() if recurrence_id is not None else 'that time'}. "
        "The event was found; the instance was not. Use `calendar_events_list` "
        "to see which instances the series actually has, or omit "
        "`recurrence_id` to read the series itself."
    )


def _normalised_url(url: str) -> str:
    """A collection URL compared without caring about one trailing slash."""
    return url.rstrip("/")


def _display_name(calendar: object) -> str:
    """A calendar's human name, falling back to the last URL segment."""
    try:
        name = calendar.name  # type: ignore[attr-defined]
    except AttributeError:
        # Only a genuinely absent attribute is a fallback. A DAV or HTTP failure
        # while fetching the name must reach the translation boundary, not be
        # dressed up as a calendar called after its own URL.
        name = None
    if name:
        return str(name)
    return str(getattr(calendar, "url", "")).rstrip("/").rsplit("/", 1)[-1] or "(unnamed)"


def _object_data(obj: object) -> str:
    """The raw iCalendar text of one fetched object.

    Returned as text rather than as a parsed component: a document this server
    cannot parse must be counted as unreadable by the expansion, not raised as a
    failure of the whole query.
    """
    data = getattr(obj, "data", None)
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        # str() on bytes yields "b'BEGIN:VCALENDAR...'", which parses as
        # nothing at all and would count every event in it unreadable.
        return bytes(data).decode("utf-8", errors="replace")
    return str(data)


class _Translator:
    """Context manager turning protocol exceptions into the core taxonomy."""

    def __init__(self, credential_name: str, url: str) -> None:
        self._credential_name = credential_name
        self._url = url

    def __enter__(self) -> "_Translator":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        if exc is None:
            return False
        raise self._translate(exc) from exc

    def _translate(self, exc: BaseException) -> Exception:
        if isinstance(exc, YandexError):
            # Already in the taxonomy -- a deliberate NotFound raised inside the
            # boundary, say. Re-wrapping it as a protocol failure would hide the
            # one thing the caller needed to know.
            return exc

        # Transport first: caldav does not wrap these, so they would otherwise
        # escape client/ as a raw HTTP-library exception.
        if isinstance(exc, http_error.RequestException):
            return TransportError(
                f"Could not reach {self._url}: the network or the host is unavailable "
                f"({type(exc).__name__})."
            )

        if isinstance(exc, caldav_error.AuthorizationError):
            try:
                forbidden = _is_forbidden(exc)
            except _Undecidable:
                # Guessing here would either accuse a correct password or send an
                # operator to an administrator who can do nothing. Say both.
                return AuthError(
                    f"Yandex refused the connection and did not say whether the "
                    f"cause was a rejected {self._credential_name} (401) or "
                    "organisation policy disabling app passwords (403): it "
                    "returned neither a status code nor a usable reason phrase. "
                    f"Check the {self._credential_name} first, then whether an "
                    f"administrator has disabled app passwords. {_APP_PASSWORD_HINT}"
                )
            if forbidden:
                return PolicyError(
                    "Yandex refused the connection with 403. On Yandex 360 this means "
                    "organisation policy has disabled app passwords; an administrator "
                    "must re-enable them."
                )
            return AuthError(
                f"Yandex rejected the {self._credential_name}: it is wrong or has been "
                f"revoked. {_APP_PASSWORD_HINT}"
            )

        if isinstance(exc, caldav_error.NotFoundError):
            return NotFound(f"Nothing found at {self._url}.")

        if isinstance(exc, caldav_error.RateLimitError):
            return RateLimited("Yandex is rate limiting this account; retry later.")

        if isinstance(exc, caldav_error.DAVError):
            return ProtocolError(
                f"Yandex answered {self._url} in a way this server cannot honour "
                f"({type(exc).__name__})."
            )

        if isinstance(exc, Exception):
            return ProtocolError(
                f"Unexpected failure talking to {self._url} ({type(exc).__name__})."
            )
        return exc  # BaseException (KeyboardInterrupt, Cancelled) passes through.


#: The exact reason phrase for 403; anything else is not treated as one.
_FORBIDDEN_PHRASE = "forbidden"

#: Reason phrases caldav attaches when the server gave it nothing usable.
_EMPTY_REASONS = frozenset({"", "none", "none given", "no reason"})


class _Undecidable(Exception):
    """Neither a status code nor a usable reason phrase was available."""


def _is_forbidden(exc: BaseException) -> bool:
    """Distinguish 403 (policy) from 401 (bad password) on a caldav auth error.

    ``AuthorizationError`` covers both, and carries no status of its own -- the
    number, when there is one, is on the response attached to it. The reason
    phrase is the fallback, matched exactly rather than by substring so that a
    phrase merely containing the word cannot decide the question.

    Raises:
        _Undecidable: when neither source can settle it.
    """
    status = _status_from(exc)
    if status is not None:
        return status == 403

    reason = str(getattr(exc, "reason", "") or "").strip().lower()
    if reason == _FORBIDDEN_PHRASE:
        return True
    if reason and reason not in _EMPTY_REASONS:
        return False
    raise _Undecidable


def _status_from(exc: BaseException) -> int | None:
    """The numeric HTTP status from the response caldav attached, if any."""
    for attribute in ("response", "resp", "reason_code", "status_code", "status"):
        candidate = getattr(exc, attribute, None)
        if isinstance(candidate, bool) or candidate is None:
            continue
        if isinstance(candidate, int):
            return candidate
        for field in ("status_code", "status"):
            nested = getattr(candidate, field, None)
            if isinstance(nested, int) and not isinstance(nested, bool):
                return nested
    return None
