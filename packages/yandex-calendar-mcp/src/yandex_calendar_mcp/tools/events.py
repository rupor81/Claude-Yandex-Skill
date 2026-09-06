"""The ``calendar_events_list`` tool contract.

Like every module under ``tools/``, this one imports no protocol library.  It
owns what the caller is allowed to ask for, what order the answer comes back in,
where a page stops, and -- crucially -- whether the answer may call itself
complete.  Everything about ``RRULE`` lives a layer down.

The decisions worth reading twice:

* The client is never given a text parameter.  ``title_contains`` is applied
  here, over occurrences already fetched, so a filter can never turn a short
  answer into a confident one: filtering a truncated source yields
  ``complete: false``.
* The cursor names the last occurrence returned, not how many were skipped.
  Occurrences are derived rather than stored, and the set shifts whenever a
  meeting is booked; an index into it would silently skip or repeat one.
* The cursor is also carried down to the fetch, so the expansion ceiling applies
  to what is left *after* it.  A page may be short, but it may never be short
  and unresumable at the same time: an empty page with ``complete: false`` and
  no cursor is a state this contract does not have.
* The cursor is bound to the question it answered.  Range and filters are
  hashed into it, so replaying page one's cursor against a different range is
  refused rather than quietly continuing a different question.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, Field
from yandex_core.errors import ProtocolError
from yandex_core.paging import checked_limit, encode_position_cursor
from yandex_core.results import Page

from ..client.caldav_client import CalDAVCalendarClient
from ..client.recurrence import (
    SCOPE_OCCURRENCE,
    SCOPE_SERIES,
    SCOPE_SINGLE,
    EventRecord,
    Occurrence,
    Participant,
    format_instant,
    parse_instant,
    position_sort_key,
)
from .timerange import (
    MAX_RANGE_DAYS,
    MIN_LIMIT,
    MORE_PAGES,
    RANGE_TRUNCATED,
    UNREADABLE_DATA,
    check_range,
    checked_calendar_url,
    checked_instant,
    decoded_position,
    incomplete_reasons,
    query_stamp,
)

__all__ = [
    "EventOccurrence",
    "EventPage",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "MAX_RANGE_DAYS",
    "Attendee",
    "EventDetail",
    "TOOL_NAME",
    "GET_TOOL_NAME",
    "SCOPE_SINGLE",
    "SCOPE_SERIES",
    "SCOPE_OCCURRENCE",
    "NO_ETAG_NOTE",
    "ETAG_UNREADABLE_NOTE",
    "DESCRIPTION_TRUNCATED_NOTE",
    "MAX_DESCRIPTION_CHARS",
    "build_calendar_event_get",
    "MORE_PAGES",
    "RANGE_TRUNCATED",
    "UNREADABLE_DATA",
    "build_calendar_events_list",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: How much of an invitation body one answer may carry.  A meeting description
#: is routinely thousands of characters of quoted mail; every other listing in
#: this server is bounded and says so, and an unbounded one here would be the
#: single place a caller could not predict the size of an answer.
MAX_DESCRIPTION_CHARS = 2000

TOOL_NAME = "calendar_events_list"
GET_TOOL_NAME = "calendar_event_get"

#: What the answer says when the server supplied no ETag.  The field is null and
#: this note explains the consequence, because an invented value would make a
#: later conditional update overwrite somebody else's edit.
NO_ETAG_NOTE = (
    "The server supplied no ETag for this object, so `etag` is null. Nothing "
    "was invented in its place: a later update cannot use this read as a "
    "precondition, and would have to be made without one."
)

#: What the answer says when the ETag property existed but could not be read.
#: Deliberately not the same sentence: "the server sent none" is settled, while
#: this one may well succeed on a retry, and the event itself was read fine.
ETAG_UNREADABLE_NOTE = (
    "The ETag of this object could not be read -- which is not the same as the "
    "server supplying none -- so `etag` is null and nothing was invented in its "
    "place. The event itself was read in full. Retry to obtain an ETag; until "
    "one is read, a later update cannot use this read as a precondition."
)

#: What the answer says when the invitation body was longer than the cap.
DESCRIPTION_TRUNCATED_NOTE = (
    f"`description` was longer than {MAX_DESCRIPTION_CHARS} characters and has "
    "been cut to that length; the rest is not in this answer."
)

#: The fields a cursor carries to name one occurrence and the query it answered.
_CURSOR_FIELDS = ("start", "calendar_url", "uid", "recurrence_id", "query")

ClientProvider = Callable[[], Awaitable[CalDAVCalendarClient]]


class EventOccurrence(BaseModel):
    """One concrete instance of an event, with its own start and end."""

    uid: str = Field(
        description=(
            "Identifier of the event, shared by every occurrence of a series. "
            "Together with `recurrence_id` and `calendar_url` it addresses this "
            "occurrence: the same `uid` can appear in two calendars."
        )
    )
    recurrence_id: str | None = Field(
        description=(
            "The occurrence's place in its series, as an ISO 8601 timestamp, or "
            "null when the event is a one-off rather than part of a series."
        )
    )
    summary: str | None = Field(description="Event title, or null when it has none.")
    start: datetime | date = Field(
        description=(
            "Start of this occurrence: a timestamp with an explicit offset, or a "
            "plain date when `all_day` is true."
        )
    )
    end: datetime | date = Field(
        description="End of this occurrence, in the same form as `start`."
    )
    all_day: bool = Field(
        description=(
            "True when this occurrence is an all-day event, whose `start` and "
            "`end` are dates rather than timestamps."
        )
    )
    calendar_url: str = Field(description="CalDAV URL of the calendar it lives in.")
    calendar_name: str = Field(description="Display name of that calendar.")


class EventPage(Page[EventOccurrence]):
    """A page of occurrences, which can also be short for a reason paging cannot fix."""

    unreadable: int = Field(
        default=0,
        description=(
            "How many events in the range could not be read at all. An event "
            "with malformed components counts once; a whole calendar object "
            "that will not parse counts once however many events it held, "
            "because how many that was is precisely what could not be read -- "
            "so this is a lower bound on the loss. When it is above zero the "
            "answer is missing something and `complete` is false, and no cursor "
            "can retrieve it: the data itself is malformed."
        ),
    )
    unreadable_calendars: int = Field(
        default=0,
        description=(
            "How many calendars could not be read at all -- a shared calendar "
            "the account may no longer open, say. Everything in them is missing "
            "from this answer and no cursor can retrieve it."
        ),
    )
    incomplete_reason: str | None = Field(
        default=None,
        description=(
            "Why `complete` is false, or null when it is true. "
            f"`{MORE_PAGES}`: more occurrences match; pass `next_cursor` back. "
            f"`{RANGE_TRUNCATED}`: the range holds more occurrences than one "
            "expansion may carry; `next_cursor` still continues, but a narrower "
            "range answers faster. "
            f"`{UNREADABLE_DATA}`: some events or calendars could not be read; "
            "see `unreadable` and `unreadable_calendars`. No cursor fixes this "
            "one. When more than one applies, the resumable reasons are named "
            "first, because they are the ones with an action attached."
        ),
    )


def build_calendar_events_list(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[EventPage]]:
    """Bind ``calendar_events_list`` to a source of clients."""

    async def calendar_events_list(
        start: Annotated[
            datetime,
            Field(
                description=(
                    "Start of the range, inclusive. ISO 8601 with an explicit "
                    "offset, e.g. 2026-06-01T00:00:00+03:00. Required: no window "
                    "is ever guessed."
                )
            ),
        ],
        end: Annotated[
            datetime,
            Field(
                description=(
                    "End of the range, exclusive. ISO 8601 with an explicit "
                    f"offset. At most {MAX_RANGE_DAYS} days after `start`."
                )
            ),
        ],
        calendar_url: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Restrict to one calendar, by the URL `calendar_list` "
                    "returned. Omit, or pass an empty string, to search every "
                    "calendar on the account."
                ),
            ),
        ] = None,
        title_contains: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Keep only occurrences whose title contains this text, "
                    "case-insensitively. Applied after fetching the range. "
                    "Omit it to filter nothing; a blank string is refused "
                    "rather than treated as no filter."
                ),
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_LIMIT,
                ge=MIN_LIMIT,
                le=MAX_LIMIT,
                description=(
                    f"Maximum occurrences to return (default {DEFAULT_LIMIT}, "
                    f"maximum {MAX_LIMIT})."
                ),
            ),
        ] = DEFAULT_LIMIT,
        cursor: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Opaque cursor from a previous incomplete call. It is bound "
                    "to the range and filters it was issued for; pass those back "
                    "unchanged with it."
                ),
            ),
        ] = None,
    ) -> EventPage:
        """List concrete event occurrences in a date range.

        Recurring series are expanded into individual occurrences, each with its
        own start and end: no recurrence rule is ever returned. Cancelled
        instances are absent and modified instances appear once, in their
        modified form.

        An occurrence belongs to this range when its own `start` is at or after
        `start` and strictly before `end`. A meeting that began earlier and runs
        into the range is therefore not returned, and one starting exactly at
        `end` belongs to the next range, not this one -- so adjacent ranges
        never repeat an occurrence between them.

        Both `start` and `end` are required and must carry an explicit UTC
        offset. If `complete` is false the answer was cut short and
        `incomplete_reason` says why: pass `next_cursor` back for the rest, or
        read `unreadable` and `unreadable_calendars` when the loss is data this
        server could not read at all.
        """
        window_start = checked_instant(start, "start")
        window_end = checked_instant(end, "end")
        check_range(window_start, window_end)
        limit_used = checked_limit(limit, minimum=MIN_LIMIT, maximum=MAX_LIMIT)
        wanted_calendar = checked_calendar_url(calendar_url)
        needle = _checked_title(title_contains)

        query = query_stamp(
            start=window_start,
            end=window_end,
            calendar_url=wanted_calendar,
            extra=(needle,),
        )
        after = _position_from(cursor, query=query)

        client = await client_provider()
        expansion = await client.list_occurrences(
            start=window_start,
            end=window_end,
            calendar_url=wanted_calendar,
            after=after,
        )

        # The client applies `after` before its own ceiling, which is what makes
        # a truncated range pageable at all. Re-applying it here costs nothing
        # and keeps this contract true of any client, not only that one.
        fetched = [
            o
            for o in expansion.occurrences
            if after is None or _key(o) > after
        ]
        matching = (
            [o for o in fetched if needle in (o.summary or "").casefold()]
            if needle is not None
            else fetched
        )

        window = matching[:limit_used]
        more_remain = len(matching) > len(window)
        lost = bool(expansion.unreadable or expansion.unreadable_calendars)

        # Where the next page must resume from. When the filter left this page
        # empty but the fetch itself was cut short, the resume point is the last
        # occurrence *considered*, not the last returned -- otherwise a filtered
        # truncation would offer no way to continue.
        if more_remain:
            resume_from: Occurrence | None = window[-1]
        elif expansion.truncated and fetched:
            resume_from = fetched[-1]
        else:
            resume_from = None

        complete = resume_from is None and not lost
        # This tool's contract names one reason; the shared helper lists every
        # shortfall in the order a caller can act on them, so the first is the
        # one to name.
        reasons = incomplete_reasons(
            more_remain=more_remain, truncated=expansion.truncated, lost=lost
        )
        reason = reasons[0] if reasons else None

        return EventPage(
            items=[_to_model(o) for o in window],
            complete=complete,
            next_cursor=(
                encode_position_cursor(
                    _cursor_position(resume_from, query=query), tool=TOOL_NAME
                )
                if resume_from is not None
                else None
            ),
            unreadable=expansion.unreadable,
            unreadable_calendars=expansion.unreadable_calendars,
            incomplete_reason=reason,
        )

    calendar_events_list.__name__ = TOOL_NAME
    return calendar_events_list


def _key(occurrence: Occurrence):
    return position_sort_key(
        occurrence.start,
        occurrence.calendar_url,
        occurrence.uid,
        occurrence.recurrence_id,
    )


def _to_model(occurrence: Occurrence) -> EventOccurrence:
    return EventOccurrence(
        uid=occurrence.uid,
        recurrence_id=occurrence.recurrence_id,
        summary=occurrence.summary,
        start=occurrence.start,
        end=occurrence.end,
        all_day=occurrence.all_day,
        calendar_url=occurrence.calendar_url,
        calendar_name=occurrence.calendar_name,
    )


def _cursor_position(
    occurrence: Occurrence, *, query: str
) -> dict[str, str | None]:
    return {
        "start": format_instant(occurrence.start),
        "calendar_url": occurrence.calendar_url,
        "uid": occurrence.uid,
        "recurrence_id": occurrence.recurrence_id,
        "query": query,
    }


def _position_from(cursor: str | None, *, query: str):
    """The sort key of the last occurrence a previous page returned."""
    if cursor is None:
        return None
    position = decoded_position(
        cursor,
        tool=TOOL_NAME,
        fields=_CURSOR_FIELDS,
        query=query,
        restate="`start`, `end`, `calendar_url` and `title_contains`",
    )
    raw_start = position["start"]
    calendar_url = position["calendar_url"]
    uid = position["uid"]
    if (
        not isinstance(raw_start, str)
        or not isinstance(calendar_url, str)
        or not isinstance(uid, str)
    ):
        raise ProtocolError("Cursor is not a cursor this server issued.")
    try:
        start = parse_instant(raw_start)
        return position_sort_key(start, calendar_url, uid, position["recurrence_id"])
    except (ValueError, OverflowError, OSError) as exc:
        # A timestamp that parses but names a moment no clock can hold is still
        # not a position this server ever issued.
        raise ProtocolError("Cursor is not a cursor this server issued.") from exc


def _checked_title(title_contains: object) -> str | None:
    """The case-folded needle, or ``None`` for no filter at all.

    A blank needle is refused rather than silently ignored: reporting an
    unfiltered result as filtered is the kind of quiet mismatch that makes a
    caller trust a wrong answer.
    """
    if title_contains is None:
        return None
    if not isinstance(title_contains, str):
        raise ProtocolError(
            f"`title_contains` must be a string, not {type(title_contains).__name__}."
        )
    trimmed = title_contains.strip()
    if not trimmed:
        raise ProtocolError(
            "`title_contains` is blank. Omit it entirely to filter nothing; a "
            "blank filter would report an unfiltered answer as a filtered one."
        )
    return trimmed.casefold()


# -- one event, in full ---------------------------------------------------


class Attendee(BaseModel):
    """One person on an event, as the invitation records them."""

    email: str | None = Field(
        description=(
            "Address of the participant, or null when the line carried none."
        )
    )
    name: str | None = Field(
        description=(
            "Display name, or null when the invitation gave only an address. "
            "Never derived from the address: a name nobody chose is worse than "
            "no name."
        )
    )
    response_status: str | None = Field(
        description=(
            "Whether they have replied, as iCalendar spells it -- ACCEPTED, "
            "DECLINED, TENTATIVE, NEEDS-ACTION -- or null when unstated."
        )
    )
    role: str | None = Field(
        description=(
            "REQ-PARTICIPANT, OPT-PARTICIPANT, CHAIR, and so on, or null when "
            "unstated."
        )
    )


class EventDetail(BaseModel):
    """One event -- a series, or one instance of one -- with everything on it.

    Everything the invitation records that this server can read: who is coming
    and how they replied, where it is and how to join it, how a series recurs,
    and the version of the object for a later conditional update. The one bound
    is `description`, which is capped and says so when it was cut.
    """

    uid: str = Field(
        description=(
            "Identifier of the event, shared by every instance of a series."
        )
    )
    recurrence_id: str | None = Field(
        description=(
            "Which instance this is, as an ISO 8601 timestamp, or null when "
            "this is the series itself or a one-off event."
        )
    )
    is_series: bool = Field(
        description="True when this event recurs; false for a one-off event."
    )
    scope: str = Field(
        description=(
            f"What was returned. `{SCOPE_SINGLE}`: a one-off event. "
            f"`{SCOPE_SERIES}`: the series itself, not one of its instances -- "
            "its `start` and `end` are the series' own, and the other instances "
            "are elsewhere in the series. "
            f"`{SCOPE_OCCURRENCE}`: exactly the instance named by "
            "`recurrence_id`, with any override applied."
        )
    )
    cancelled: bool = Field(
        description=(
            "True when this event or instance is cancelled and is not "
            "happening. A cancelled instance is reported, not hidden, and never "
            "returned as a live meeting."
        )
    )
    status: str | None = Field(
        description=(
            "CONFIRMED, TENTATIVE or CANCELLED as the event states it, or null "
            "when it states nothing."
        )
    )
    summary: str | None = Field(description="Title, or null when it has none.")
    description: str | None = Field(
        description=(
            "Body of the invitation, or null when it has none. Capped at "
            f"{MAX_DESCRIPTION_CHARS} characters -- a meeting body is routinely "
            "thousands of characters of quoted mail. When it was cut, "
            "`description_truncated` is true and `description_note` says so; "
            "the rest is not retrievable through this tool."
        )
    )
    description_truncated: bool = Field(
        default=False,
        description=(
            f"True when `description` was longer than {MAX_DESCRIPTION_CHARS} "
            "characters and has been cut to that length. Never true silently: "
            "`description_note` carries the same statement in words."
        ),
    )
    description_note: str | None = Field(
        default=None,
        description="Why `description` is short, or null when it is whole.",
    )
    location: str | None = Field(description="Location, or null when unset.")
    join_url: str | None = Field(
        default=None,
        description=(
            "Where to join this meeting, read from the invitation's conference "
            "property or, failing that, the first link in its location or body. "
            "Null when the invitation carried none; never assembled from "
            "anything the invitation did not contain."
        ),
    )
    recurrence_summary: str | None = Field(
        default=None,
        description=(
            "How this series repeats, in a sentence -- for example \"Repeats "
            "daily, 5 times in all.\" Null for a one-off event. The recurrence "
            "rule itself is never returned; use `calendar_events_list` to see "
            "the concrete instances."
        ),
    )
    organizer: Attendee | None = Field(
        description=(
            "Who called the meeting, or null when unstated. When an invitation "
            "carries more than one organizer line this is the first; all of them "
            "are in `organizers`, and none is dropped."
        )
    )
    organizers: list[Attendee] = Field(
        default_factory=list,
        description=(
            "Every organizer line on the invitation, in the order it recorded "
            "them. Usually one; more than one is unusual but is reported rather "
            "than silently reduced to the first."
        ),
    )
    attendees: list[Attendee] = Field(
        description="Everyone invited, with their response status."
    )
    start: datetime | date = Field(
        description=(
            "Start: a timestamp with an explicit offset, or a plain date when "
            "`all_day` is true. For an instance, that instance's own start."
        )
    )
    end: datetime | date = Field(description="End, in the same form as `start`.")
    all_day: bool = Field(
        description="True when this is an all-day event, whose bounds are dates."
    )
    etag: str | None = Field(
        description=(
            "Version of this object on the server, for use as a precondition "
            "when it is later changed. Null when the server supplied none -- "
            "see `etag_note`; a value is never invented."
        )
    )
    etag_note: str | None = Field(
        default=None,
        description=(
            "Why `etag` is null, or null when an ETag was returned. It "
            "distinguishes the two reasons, which need different responses: the "
            "server supplied no ETag for this object, or the ETag property "
            "could not be read -- the latter may succeed on a retry."
        ),
    )
    calendar_url: str = Field(
        description=(
            "CalDAV URL of the calendar it lives in. The same `uid` can exist "
            "in more than one calendar; when no `calendar_url` was given, this "
            "is the first calendar in `calendar_list` order that held it, and "
            "the remaining calendars were not searched. Pass `calendar_url` to "
            "choose which one answers."
        )
    )
    calendar_name: str = Field(description="Display name of that calendar.")


def build_calendar_event_get(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[EventDetail]]:
    """Bind ``calendar_event_get`` to a source of clients."""

    async def calendar_event_get(
        uid: Annotated[
            str,
            Field(
                description=(
                    "Identifier of the event, as `calendar_events_list` "
                    "returned it. The event is addressed by this UID, never "
                    "searched for."
                )
            ),
        ],
        recurrence_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Which instance of a series to read, as "
                    "`calendar_events_list` returned it: an ISO 8601 timestamp "
                    "with an explicit offset, or a plain date for an all-day "
                    "series. Omit it to read the series itself."
                ),
            ),
        ] = None,
        calendar_url: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Restrict the lookup to one calendar, by the URL "
                    "`calendar_list` or `calendar_events_list` returned. Omit, "
                    "or pass an empty string, to try every calendar until the "
                    "event is found."
                ),
            ),
        ] = None,
    ) -> EventDetail:
        """Read one event in full: attendees, description, location and its ETag.

        The event is addressed by `uid`, never searched for. Give
        `recurrence_id` to read one instance of a series -- with any override
        applied -- or omit it to read the series itself; `scope` says which you
        got. A cancelled instance is returned with `cancelled: true` rather than
        as a live meeting.

        The same `uid` can exist in more than one calendar -- a routine
        duplicate on a shared account. Without `calendar_url` the calendars are
        tried in `calendar_list` order and the first one holding the `uid`
        answers; the rest are not searched, and `calendar_url` in the answer
        says which one it was. Pass `calendar_url` to choose.

        A `uid` nothing on the account holds is an error naming it, never an
        empty result, and an instance that is not in the series is a different
        error from a `uid` that is not there. If a calendar could not be read
        while the account was searched, a miss says the search was incomplete
        rather than claiming the event does not exist, and a `calendar_url` that
        is not a calendar on this account is reported as that rather than as a
        missing event.

        `description` is capped and declares it; `join_url` and, for a series,
        `recurrence_summary` are the two things a caller usually needs next.

        `etag` is this version of the object, for a later conditional update. It
        is null when the server supplied none or when it could not be read, and
        `etag_note` says which; no value is ever invented.
        """
        wanted_uid = _checked_uid(uid)
        instance = _checked_recurrence_id(recurrence_id)
        wanted_calendar = checked_calendar_url(calendar_url)

        client = await client_provider()
        fetched = await client.get_event(
            uid=wanted_uid,
            recurrence_id=instance,
            calendar_url=wanted_calendar,
        )
        return _to_detail(
            fetched.record,
            etag=fetched.etag,
            etag_unreadable=fetched.etag_unreadable,
        )

    calendar_event_get.__name__ = GET_TOOL_NAME
    return calendar_event_get


def _to_detail(
    record: EventRecord, *, etag: str | None, etag_unreadable: bool = False
) -> EventDetail:
    description, truncated = _bounded(record.description)
    return EventDetail(
        uid=record.uid,
        recurrence_id=record.recurrence_id,
        is_series=record.is_series,
        scope=record.scope,
        cancelled=record.cancelled,
        status=record.status,
        summary=record.summary,
        description=description,
        description_truncated=truncated,
        description_note=DESCRIPTION_TRUNCATED_NOTE if truncated else None,
        location=record.location,
        join_url=record.join_url,
        recurrence_summary=record.recurrence_summary,
        organizer=_to_attendee(record.organizer),
        organizers=[_to_attendee(person) for person in record.organizers],
        attendees=[_to_attendee(person) for person in record.attendees],
        start=record.start,
        end=record.end,
        all_day=record.all_day,
        etag=etag,
        etag_note=_etag_note(etag, etag_unreadable),
        calendar_url=record.calendar_url,
        calendar_name=record.calendar_name,
    )


def _etag_note(etag: str | None, unreadable: bool) -> str | None:
    """Which of the two reasons `etag` is null, or nothing when it is not."""
    if etag:
        return None
    return ETAG_UNREADABLE_NOTE if unreadable else NO_ETAG_NOTE


def _bounded(description: str | None) -> tuple[str | None, bool]:
    """The invitation body, cut to the cap, and whether cutting was needed."""
    if description is None or len(description) <= MAX_DESCRIPTION_CHARS:
        return description, False
    return description[:MAX_DESCRIPTION_CHARS], True


def _to_attendee(person: Participant | None) -> Attendee | None:
    if person is None:
        return None
    return Attendee(
        email=person.email,
        name=person.name,
        response_status=person.response_status,
        role=person.role,
    )


def _checked_uid(uid: object) -> str:
    """A UID that actually names something.

    A blank UID would be addressed as a real href and answered with a not-found
    about the empty string, which tells the caller nothing about their mistake.
    """
    if not isinstance(uid, str):
        raise ProtocolError(f"`uid` must be a string, not {type(uid).__name__}.")
    trimmed = uid.strip()
    if not trimmed:
        raise ProtocolError(
            "`uid` is blank. Give the `uid` of an event, as "
            "`calendar_events_list` returned it."
        )
    return trimmed


def _checked_recurrence_id(recurrence_id: object) -> date | datetime | None:
    """Which instance, validated before anything is sent.

    A blank string is refused rather than read as "the series": the two are
    different questions, and answering the wrong one silently is exactly the
    quiet mismatch this project refuses. A naive timestamp is refused for the
    same reason it is everywhere else -- a moment with no offset names a
    different instance to every reader.
    """
    if recurrence_id is None:
        return None
    if isinstance(recurrence_id, (datetime, date)):
        value: date | datetime = recurrence_id
    else:
        if not isinstance(recurrence_id, str):
            raise ProtocolError(
                "`recurrence_id` must be an ISO 8601 timestamp, not "
                f"{type(recurrence_id).__name__}."
            )
        trimmed = recurrence_id.strip()
        if not trimmed:
            raise ProtocolError(
                "`recurrence_id` is blank. Omit it entirely to read the series "
                "itself; a blank value would answer a different question from "
                "the one asked."
            )
        try:
            value = parse_instant(trimmed)
        except ValueError as exc:
            raise ProtocolError(
                f"`recurrence_id` is not an ISO 8601 timestamp with an explicit "
                f"UTC offset: {recurrence_id!r}. Pass back the `recurrence_id` "
                "`calendar_events_list` returned, for example "
                "2026-06-09T09:00:00+03:00."
            ) from exc
    if isinstance(value, datetime) and (
        value.tzinfo is None or value.utcoffset() is None
    ):
        raise ProtocolError(
            "`recurrence_id` has no UTC offset. Give an explicit one, for "
            "example 2026-06-09T09:00:00+03:00; a naive timestamp names a "
            "different instance to every reader."
        )
    return value
