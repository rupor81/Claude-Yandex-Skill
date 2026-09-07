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
from datetime import date, datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field
from yandex_core.errors import ProtocolError
from yandex_core.paging import checked_limit, encode_position_cursor
from yandex_core.results import Page

from ..client.caldav_client import (
    CalDAVCalendarClient,
    CreatedEvent,
    DeletedEvent,
    UpdatedEvent,
    check_instance_matches_scope,
    checked_delete_etag,
    checked_delete_scope,
    checked_etag,
    checked_scope,
)
from ..client.compose import UNCHANGED, EventEdit, check_event_edit
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
    "MAX_SUMMARY_CHARS",
    "MAX_LOCATION_CHARS",
    "MAX_DESCRIPTION_INPUT_CHARS",
    "build_calendar_event_get",
    "CREATE_TOOL_NAME",
    "StoredEvent",
    "EventCreated",
    "build_calendar_event_create",
    "UPDATE_TOOL_NAME",
    "EventUpdated",
    "build_calendar_event_update",
    "DELETE_TOOL_NAME",
    "EventDeleted",
    "build_calendar_event_delete",
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

#: What a caller may *send*.  Nothing composed here is bounded by the protocol,
#: so without these a megabyte description would be composed and PUT, and the
#: 413 that came back would surface as "the event may or may not exist" -- a
#: write of unknown outcome caused by a value that could have been refused
#: before the connection was opened.  They are deliberately generous: they exist
#: to stop the absurd, not to have an opinion about long invitations.
MAX_SUMMARY_CHARS = 500
MAX_LOCATION_CHARS = 500
MAX_DESCRIPTION_INPUT_CHARS = 20_000

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


# -- creating one event ---------------------------------------------------


class StoredEvent(BaseModel):
    """What the server holds after the write -- never what was asked for.

    This server adjusts values on write, so every field here was read back off
    the server afterwards. A caller comparing these against its request is
    comparing against reality; a caller told its own request back has been told
    nothing.
    """

    summary: str | None = Field(description="Title, as stored.")
    description: str | None = Field(
        description=(
            "Invitation body as stored, or null when it has none. Capped at "
            f"{MAX_DESCRIPTION_CHARS} characters like everywhere else in this "
            "server; `description_truncated` says when it was cut."
        )
    )
    description_truncated: bool = Field(
        default=False,
        description=(
            f"True when `description` was longer than {MAX_DESCRIPTION_CHARS} "
            "characters and this answer carries only that much of it. The event "
            "on the server is whole; this field is about the answer."
        ),
    )
    location: str | None = Field(description="Location as stored, or null.")
    start: datetime | date = Field(
        description=(
            "Start as stored: a timestamp with an explicit offset, or a plain "
            "date for an all-day event."
        )
    )
    end: datetime | date = Field(description="End as stored, in the same form.")
    all_day: bool = Field(
        description="True when the server stored this as an all-day event."
    )
    status: str | None = Field(
        default=None,
        description="CONFIRMED, TENTATIVE or CANCELLED as stored, or null.",
    )
    is_series: bool = Field(
        default=False,
        description=(
            "True if the stored event recurs. This tool creates one-off events "
            "only, so a true here means the server made it something else."
        ),
    )


class EventCreated(BaseModel):
    """One event that now exists, described by what the server holds."""

    created: bool = Field(
        description=(
            "Always true when this answer is returned: the server accepted the "
            "write. A write that did not happen is an error, never this model "
            "with `created: false` -- and a write whose outcome is unknown is an "
            "error that says so and names the UID to check."
        )
    )
    uid: str = Field(
        description=(
            "Identifier of the new event. Pass it to `calendar_event_get` to "
            "read it, and keep it: it is how this event is addressed from now on."
        )
    )
    href: str = Field(
        description=(
            "CalDAV URL of the object that holds the new event, as the readback "
            "found it -- which is not always the address the write was aimed at, "
            "because this server may file an object under an href of its own. "
            "When the readback failed there was nothing to observe and this is "
            "the address written to; `stored_note` says so."
        )
    )
    calendar_url: str = Field(
        description=(
            "The calendar it was written into, as that calendar's own listing "
            "gives its URL -- which is not always the URL it was asked for by."
        )
    )
    calendar_name: str = Field(description="Display name of that calendar.")
    etag: str | None = Field(
        description=(
            "Version of the new object, for use as a precondition when it is "
            "later changed. Null when the server supplied none or it could not "
            "be read -- see `etag_note`; a value is never invented."
        )
    )
    etag_note: str | None = Field(
        default=None,
        description="Why `etag` is null, or null when an ETag was returned.",
    )
    stored: StoredEvent | None = Field(
        default=None,
        description=(
            "What the server holds, read back after the write. Null only when "
            "the readback failed, which does not mean the write did: see "
            "`stored_note`."
        ),
    )
    stored_note: str | None = Field(
        default=None,
        description=(
            "Why `stored` is null, or null when it was read. The event exists "
            "either way -- this says only that its stored values could not be "
            "confirmed."
        ),
    )
    differs_from_request: bool = Field(
        description=(
            "True when at least one stored value is not what was asked for. "
            "False when they match -- or, when the readback failed, when there "
            "was nothing to compare: `difference_note` says which."
        )
    )
    differences: list[str] = Field(
        default_factory=list,
        description=(
            "One line per value the server stored differently, naming the field, "
            "what was asked for and what is there."
        ),
    )
    difference_note: str | None = Field(
        default=None,
        description=(
            "What the comparison established, in words: that the stored values "
            "match the request, that they differ, or that they could not be "
            "compared at all."
        ),
    )


CREATE_TOOL_NAME = "calendar_event_create"

#: What the answer says when the event exists but its stored values are unknown.
READBACK_FAILED_NOTE = (
    "The event was created -- the server accepted the write -- but it could not "
    "be read back, so this answer carries no stored values: {reason}. Do not "
    "create it again; read it with `calendar_event_get` using the `uid` above."
)

#: What the answer says when there was nothing to compare against.
NO_COMPARISON_NOTE = (
    "The stored values could not be read back, so nothing was compared: this is "
    "not a statement that the server stored what was asked for."
)

MATCHES_NOTE = "The server stored every value as it was requested."

DIFFERS_NOTE = (
    "The server stored at least one value differently from the request; "
    "`differences` names each one. The stored values are what exists."
)

#: What the answer says about an ETag on a created object that was not read.
CREATE_ETAG_UNREADABLE_NOTE = (
    "The new object's ETag could not be read, so `etag` is null and nothing was "
    "invented in its place. The event was created. Read it with "
    "`calendar_event_get` to obtain an ETag before changing it."
)


def build_calendar_event_create(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[EventCreated]]:
    """Bind ``calendar_event_create`` to a source of clients."""

    async def calendar_event_create(
        calendar_url: Annotated[
            str,
            Field(
                description=(
                    "Which calendar to create the event in, by the URL "
                    "`calendar_list` returned. Required, and never guessed: "
                    "this account has several calendars, the server marks none "
                    "of them as the default, and a meeting written into the "
                    "wrong one is not obviously recoverable."
                )
            ),
        ],
        summary: Annotated[
            str,
            Field(
                description=(
                    "Title of the event. Required and never blank: an untitled "
                    f"event cannot be found again. At most {MAX_SUMMARY_CHARS} "
                    "characters; a longer one is refused rather than cut."
                )
            ),
        ],
        start: Annotated[
            str,
            Field(
                description=(
                    "When it starts. ISO 8601 with an explicit offset, e.g. "
                    "2026-06-08T09:00:00+03:00 -- or a plain date, e.g. "
                    "2026-06-08, to create an all-day event. A timestamp "
                    "without an offset is refused."
                )
            ),
        ],
        end: Annotated[
            str,
            Field(
                description=(
                    "When it ends, exclusive, in the same form as `start`: both "
                    "timestamps, or both dates. For a single all-day event this "
                    "is the following date. Must be after `start`."
                )
            ),
        ],
        description: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Body of the invitation. Omit it entirely for none; a blank "
                    "string is refused rather than stored as an empty body. At "
                    f"most {MAX_DESCRIPTION_INPUT_CHARS} characters."
                ),
            ),
        ] = None,
        location: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Where it is. Omit it entirely for none; a blank string is "
                    f"refused. At most {MAX_LOCATION_CHARS} characters."
                ),
            ),
        ] = None,
    ) -> EventCreated:
        """Create one event in a named calendar and report what the server stored.

        This is a write: it adds an event to the operator's calendar. It creates
        one-off events only -- no recurrence -- and invites nobody: attendees are
        deliberately not offered, because inviting sends mail on the operator's
        behalf.

        `calendar_url` is required. No calendar is chosen for you: the account
        has several, none is marked default, and an event in the wrong calendar
        is not obviously recoverable. A URL that is not one of this account's
        calendars is an error and nothing is written.

        Nothing existing is ever replaced. The write refuses to overwrite an
        object that is already at the new event's address, and says so rather
        than silently taking its place.

        The answer reports what the *server* holds, read back after the write,
        not what was asked for -- this server adjusts stored values, and
        `differences` names any that came back changed. If the event could not be
        read back afterwards it is still reported as created, with `stored_note`
        explaining: it exists, and creating it again would make two.

        `etag` is the new object's version, for a later conditional change.
        """
        wanted_calendar = _required_calendar_url(calendar_url)
        title = _checked_summary(summary)
        begins = _checked_boundary(start, "start")
        finishes = _checked_boundary(end, "end")
        _check_event_bounds(begins, finishes)
        body = _checked_optional_text(description, "description")
        where = _checked_optional_text(location, "location")

        client = await client_provider()
        created = await client.create_event(
            calendar_url=wanted_calendar,
            summary=title,
            start=begins,
            end=finishes,
            description=body,
            location=where,
        )

        stored = _to_stored(created.record)
        differences = (
            _differences(
                created.record,
                summary=title,
                # What was written, not what was typed: the document carries
                # whole seconds, and a microsecond this server dropped is not a
                # value the server changed.
                start=created.sent_start,
                end=created.sent_end,
                description=body,
                location=where,
            )
            if created.record is not None
            else []
        )
        return EventCreated(
            created=True,
            uid=created.uid,
            href=created.href,
            calendar_url=created.calendar_url,
            calendar_name=created.calendar_name,
            etag=created.etag,
            etag_note=_create_etag_note(created),
            stored=stored,
            stored_note=(
                READBACK_FAILED_NOTE.format(
                    reason=created.readback_error or "the reason was not reported"
                )
                if stored is None
                else None
            ),
            differs_from_request=bool(differences),
            differences=differences,
            difference_note=(
                NO_COMPARISON_NOTE
                if stored is None
                else (DIFFERS_NOTE if differences else MATCHES_NOTE)
            ),
        )

    calendar_event_create.__name__ = CREATE_TOOL_NAME
    return calendar_event_create


def _create_etag_note(created: CreatedEvent) -> str | None:
    """Why a created object has no ETag, or nothing when it has one."""
    if created.etag:
        return None
    if created.record is None or created.etag_unreadable:
        return CREATE_ETAG_UNREADABLE_NOTE
    return NO_ETAG_NOTE


def _to_stored(record: EventRecord | None) -> StoredEvent | None:
    if record is None:
        return None
    description, truncated = _bounded(record.description)
    return StoredEvent(
        summary=record.summary,
        description=description,
        description_truncated=truncated,
        location=record.location,
        start=record.start,
        end=record.end,
        all_day=record.all_day,
        status=record.status,
        is_series=record.is_series,
    )


#: A status the request did not ask for but which changes nothing about the
#: event.  Anything else -- CANCELLED above all, which puts a meeting on nobody's
#: calendar -- is a difference the caller has to be told about.
_UNREMARKABLE_STATUSES = (None, "CONFIRMED")


def _differences(
    record: EventRecord,
    *,
    summary: str,
    start: date | datetime,
    end: date | datetime,
    description: str | None,
    location: str | None,
) -> list[str]:
    """Every value the server stored differently from the request.

    Every field :class:`StoredEvent` exposes is compared. Anything left out
    would let the answer say "the server stored every value as it was requested"
    while showing, in the same object, a value that was not: a one-off stored as
    a recurring series is the case that made this explicit.

    ``start`` and ``end`` are the boundaries as they were *written*, not as the
    caller spelled them: composing the document truncates microseconds, and
    blaming the server for that would accuse Yandex of an edit this server made.

    Instants are compared as instants, not as text: this server is free to
    re-spell a moment -- a different offset, a trailing Z -- and reporting that
    as a change would bury a real one in noise. A date is never equal to a
    timestamp here, so an all-day event turned into a timed one is reported.
    """
    differences: list[str] = []

    def note(field: str, requested: object, stored: object) -> None:
        differences.append(
            f"{field}: requested {requested!r}, stored {stored!r}"
        )

    if (record.summary or "") != summary:
        note("summary", summary, record.summary)
    if not _same_moment(record.start, start):
        note("start", format_instant(start), format_instant(record.start))
    if not _same_moment(record.end, end):
        note("end", format_instant(end), format_instant(record.end))
    if (record.description or None) != description:
        note("description", description, record.description)
    if (record.location or None) != location:
        note("location", location, record.location)
    all_day = not isinstance(start, datetime)
    if record.all_day != all_day:
        note("all_day", all_day, record.all_day)
    if record.is_series:
        # This tool creates one-off events only, so any series at all is the
        # server having made something other than what was asked for.
        note("is_series", False, True)
    if record.status not in _UNREMARKABLE_STATUSES:
        note("status", "CONFIRMED (unstated, so confirmed)", record.status)
    return differences


def _same_moment(stored: date | datetime, requested: date | datetime) -> bool:
    """Whether two boundaries name the same point, spelling aside.

    The type check is not redundant with the equality: a date and a timestamp
    are never the same boundary here, and comparing them directly raises rather
    than answering.  Two timestamps compare as instants, so a re-spelled offset
    is not a difference.
    """
    if isinstance(stored, datetime) != isinstance(requested, datetime):
        return False
    return stored == requested


def _required_calendar_url(calendar_url: object) -> str:
    """The one calendar this event goes into, never guessed."""
    if not isinstance(calendar_url, str):
        raise ProtocolError(
            "`calendar_url` must be the URL of a calendar, not "
            f"{type(calendar_url).__name__}. Use `calendar_list` to get one."
        )
    trimmed = calendar_url.strip()
    if not trimmed:
        raise ProtocolError(
            "`calendar_url` is required: no calendar is chosen for you. This "
            "account has several and the server marks none of them as the "
            "default, so an event written into a guessed one would be somewhere "
            "nobody looks. Use `calendar_list` and name the calendar."
        )
    return trimmed


def _checked_summary(summary: object) -> str:
    """A title that will make the event findable later."""
    if not isinstance(summary, str):
        raise ProtocolError(
            f"`summary` must be a string, not {type(summary).__name__}."
        )
    trimmed = summary.strip()
    if not trimmed:
        raise ProtocolError(
            "`summary` is blank. An event needs a title: an untitled event is "
            "not findable later, and nobody meant to create one."
        )
    _check_length(trimmed, "summary", MAX_SUMMARY_CHARS)
    return trimmed


#: How long each free-text field may be on the way in.
_INPUT_CAPS = {
    "description": MAX_DESCRIPTION_INPUT_CHARS,
    "location": MAX_LOCATION_CHARS,
}


def _check_length(value: str, name: str, cap: int) -> None:
    """Refuse a value too large to be meant, before anything is composed.

    Raised here rather than left to the server: an oversized document is
    answered with a 413 *after* the request has left, and a write whose outcome
    is unknown is a far worse answer than a refusal that names the field.
    """
    if len(value) > cap:
        raise ProtocolError(
            f"`{name}` is {len(value)} characters, beyond the {cap}-character "
            "maximum this server will write. It is not shortened for you: a "
            "silently truncated value would be stored as though it were what "
            "was meant. Shorten it and create the event again."
        )


def _checked_optional_text(value: object, name: str) -> str | None:
    """An optional field: given and meaningful, or omitted entirely."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"`{name}` must be a string, not {type(value).__name__}.")
    trimmed = value.strip()
    if not trimmed:
        raise ProtocolError(
            f"`{name}` is blank. Omit it entirely for none; a blank value would "
            "store an empty field that reads as though something was meant."
        )
    cap = _INPUT_CAPS.get(name)
    if cap is not None:
        _check_length(trimmed, name, cap)
    return trimmed


def _checked_boundary(value: object, name: str) -> date | datetime:
    """One end of a new event: an instant with an offset, or a plain date.

    Refused before any request is made. A timestamp with no offset names a
    different moment to every reader, and an event written from one is wrong on
    somebody's calendar until they notice.
    """
    if isinstance(value, (datetime, date)):
        parsed: date | datetime = value
    else:
        if not isinstance(value, str):
            raise ProtocolError(
                f"`{name}` must be an ISO 8601 timestamp or date, not "
                f"{type(value).__name__}."
            )
        trimmed = value.strip()
        if not trimmed:
            raise ProtocolError(
                f"`{name}` is blank. Give a timestamp with an explicit offset, "
                "for example 2026-06-08T09:00:00+03:00, or a plain date such as "
                "2026-06-08 for an all-day event."
            )
        try:
            parsed = parse_instant(trimmed)
        except ValueError as exc:
            raise ProtocolError(
                f"`{name}` is not an ISO 8601 timestamp with an explicit UTC "
                f"offset, nor a plain date: {value!r}. For example "
                "2026-06-08T09:00:00+03:00, or 2026-06-08 for an all-day event."
            ) from exc
    if isinstance(parsed, datetime) and (
        parsed.tzinfo is None or parsed.utcoffset() is None
    ):
        raise ProtocolError(
            f"`{name}` has no UTC offset. Give an explicit one, for example "
            "2026-06-08T09:00:00+03:00; a naive timestamp names a different "
            "moment to every reader."
        )
    return parsed


def _check_event_bounds(start: date | datetime, end: date | datetime) -> None:
    """Both ends of the same kind, and ordered. Neither is repaired."""
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise ProtocolError(
            "`start` and `end` must both be timestamps, or both be plain dates "
            "for an all-day event. One of each has no reading that is not a "
            "guess about which was meant."
        )
    if _as_moment(end) <= _as_moment(start):
        raise ProtocolError(
            "`end` must be after `start`; an event that ends when it begins "
            "occupies no time. For an all-day event `end` is exclusive, so a "
            "single day ends on the following date."
        )


def _as_moment(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


# -- changing one event ---------------------------------------------------


UPDATE_TOOL_NAME = "calendar_event_update"

#: What the answer says when every named value was already what is stored.
NO_OP_NOTE = (
    "No write was sent: every value named was already what the server holds, so "
    "there was nothing to change. `stored` is the event as it stands, and "
    "`etag` is still the one that was passed in. A write storing the same "
    "values would still bump the object's version and refuse the next "
    "precondition for a change that never happened."
)

CHANGED_NOTE = (
    "The change was written and the server accepted it; `stored` is what it "
    "holds now, read back afterwards."
)

#: What the answer says when the event was changed but could not be re-read.
UPDATE_READBACK_FAILED_NOTE = (
    "The change WAS applied -- the server accepted the write -- but the event "
    "could not be read back, so this answer carries no stored values: {reason}. "
    "Do not repeat the change; read the event with `calendar_event_get` using "
    "the `uid` above."
)

#: What the answer says about an ETag on a changed object that was not read.
UPDATE_ETAG_UNREADABLE_NOTE = (
    "The changed object's new ETag could not be read, so `etag` is null and "
    "nothing was invented in its place. The old one is no longer valid as a "
    "precondition. Read the event with `calendar_event_get` to obtain the "
    "current ETag before changing it again."
)


class EventUpdated(BaseModel):
    """One event as it stands after a change was asked for."""

    changed: bool = Field(
        description=(
            "True when a write was sent and the server accepted it. False when "
            "every value named was already what the server holds: that is a "
            "no-op, not a failure, and no write was sent -- `change_note` says "
            "so. A change that was attempted and did not happen is an error, "
            "never this model."
        )
    )
    change_note: str = Field(
        description="What happened, in words: a write, or nothing to write."
    )
    uid: str = Field(description="Identifier of the event that was changed.")
    scope: str = Field(
        description=(
            f"What was changed. `{SCOPE_OCCURRENCE}`: the one instance named by "
            f"`recurrence_id`, with the rest of the series untouched. "
            f"`{SCOPE_SERIES}`: the event itself, and so every instance of it."
        )
    )
    recurrence_id: str | None = Field(
        default=None,
        description=(
            "Which instance was changed, as an ISO 8601 timestamp, or null when "
            "the series itself was."
        ),
    )
    href: str = Field(
        description="CalDAV URL of the object that holds the event."
    )
    calendar_url: str = Field(
        description="The calendar it lives in, as that calendar's listing gives it."
    )
    calendar_name: str = Field(description="Display name of that calendar.")
    etag: str | None = Field(
        description=(
            "Version of the object now, for use as a precondition next time. "
            "After a write this is the new one -- the ETag passed in is no "
            "longer valid. Null when the server supplied none or it could not "
            "be read; see `etag_note`, and never a value invented here."
        )
    )
    etag_note: str | None = Field(
        default=None,
        description="Why `etag` is null, or null when an ETag was returned.",
    )
    sent: dict[str, str | None] = Field(
        default_factory=dict,
        description=(
            "The values that were written, as they were written -- one entry "
            "per field named in the request, and nothing else. Timestamps are "
            "in UTC, which is the spelling iCalendar uses; an all-day boundary "
            "stays a plain date, and a cleared field is null. This is what the "
            "change did, and it is the only record of it when the readback "
            "failed and `stored` is null."
        ),
    )
    stored: StoredEvent | None = Field(
        default=None,
        description=(
            "What the server holds, read back after the write. Null only when "
            "the readback failed, which does not mean the write did: see "
            "`stored_note`."
        ),
    )
    stored_note: str | None = Field(
        default=None,
        description=(
            "Why `stored` is null, or null when it was read. The change "
            "happened either way -- this says only that the result could not be "
            "confirmed."
        ),
    )
    differs_from_request: bool = Field(
        description=(
            "True when at least one value the server stored is not the value "
            "that was asked for. Only the fields named in the request are "
            "compared; nothing else was meant to change. False also when there "
            "was nothing to compare -- `difference_note` says which."
        )
    )
    differences: list[str] = Field(
        default_factory=list,
        description=(
            "One line per named value the server stored differently, naming the "
            "field, what was asked for and what is there."
        ),
    )
    difference_note: str | None = Field(
        default=None,
        description=(
            "What the comparison established, in words: that the stored values "
            "match the request, that they differ, or that they could not be "
            "compared at all."
        ),
    )


def build_calendar_event_update(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[EventUpdated]]:
    """Bind ``calendar_event_update`` to a source of clients."""

    async def calendar_event_update(
        uid: Annotated[
            str,
            Field(
                description=(
                    "Identifier of the event to change, as "
                    "`calendar_events_list` or `calendar_event_get` returned it."
                )
            ),
        ],
        scope: Annotated[
            str,
            Field(
                description=(
                    f"Which change is meant. `{SCOPE_OCCURRENCE}` changes the "
                    "one instance named by `recurrence_id` and leaves the rest "
                    f"of the series alone; `{SCOPE_SERIES}` changes the event "
                    "itself, and so every instance of it -- and is also what a "
                    "one-off event takes. Required, with no default: on a "
                    "calendar where most meetings recur the two are genuinely "
                    "different changes, and they are not recoverable from each "
                    "other."
                )
            ),
        ],
        etag: Annotated[
            str,
            Field(
                description=(
                    "The `etag` of the event as you last read it, from "
                    "`calendar_event_get`. Required: it is sent as a "
                    "precondition, so a change somebody else made in between is "
                    "refused rather than overwritten. If it is refused, read the "
                    "event again -- never retry with a fresh ETag without "
                    "looking at what changed."
                )
            ),
        ],
        recurrence_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Which instance to change, as `calendar_events_list` "
                    f"returned it. Required with `scope: {SCOPE_OCCURRENCE}`, "
                    f"and refused with `scope: {SCOPE_SERIES}`, which would "
                    "contradict it."
                ),
            ),
        ] = None,
        calendar_url: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Restrict the lookup to one calendar, by the URL "
                    "`calendar_list` or `calendar_events_list` returned. Omit to "
                    "try every calendar until the event is found. This selects "
                    "where the event is looked for; it never moves it."
                ),
            ),
        ] = None,
        summary: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "New title. Omit to leave it alone; a blank string is "
                    f"refused. At most {MAX_SUMMARY_CHARS} characters."
                ),
            ),
        ] = None,
        start: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "New start, ISO 8601 with an explicit offset, or a plain "
                    "date for an all-day event. Must be given together with "
                    "`end`: moving one end alone would stretch or invert the "
                    "event, and which was meant is a guess."
                ),
            ),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "New end, exclusive, in the same form as `start`, and given "
                    "together with it -- pass it unchanged if only the start is "
                    "moving."
                ),
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "New invitation body. Omit to leave it alone; a blank string "
                    f"is refused. At most {MAX_DESCRIPTION_INPUT_CHARS} "
                    "characters."
                ),
            ),
        ] = None,
        location: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "New location. Omit to leave it alone; a blank string is "
                    f"refused. At most {MAX_LOCATION_CHARS} characters."
                ),
            ),
        ] = None,
    ) -> EventUpdated:
        """Change an event, or one instance of it, under an explicit scope.

        This is destructive: it overwrites values that are already there.

        `scope` is required and has no default. `occurrence` changes the one
        instance named by `recurrence_id`; `series` changes the event itself and
        so every instance of it, and is also what a one-off event takes. On a
        calendar where most meetings recur, "change this meeting" means two
        different things, one of them affects everyone else in the series, and
        the two are not recoverable from each other -- so neither is guessed.

        `etag` is required and is sent as a precondition. A change somebody else
        made since you read the event is refused, and nothing is written; read
        the event again and decide whether your change still applies. Never
        repeat a refused change with a fresh ETag without looking -- that is the
        overwrite the precondition just prevented.

        One race the precondition does not close, measured on this server: it
        does not evaluate `If-Match` for an object that no longer exists. An
        event **deleted** between your read and this write is therefore created
        again from the values you sent -- resurrected, not refused -- and the
        answer reports an ordinary change. If it matters that the meeting is
        still there, read it with `calendar_event_get` immediately before
        changing it; nothing this tool can send makes the server refuse.

        Only what you name is changed. The stored event is read, edited and
        written back whole, so everything unmentioned survives -- including an
        instance of a series that somebody had already moved, which lives in the
        same object as the series itself.

        `start` and `end` go together. Some changes are deliberately not
        offered: moving an event to another calendar, changing how a series
        recurs, adding or removing attendees -- the last because it sends mail
        on the operator's behalf -- and clearing a text field, since omitting
        one means "leave it alone" and there is no second way to say "make it
        empty".

        If every value you name is already what the server holds, nothing is
        written and the answer says so. Otherwise the event is read back and the
        answer reports the stored values and the new `etag`; `differences` names
        anything the server stored differently from the request.
        """
        wanted_uid = _checked_uid(uid)
        wanted_scope = checked_scope(scope)
        instance = _checked_recurrence_id(recurrence_id)
        check_instance_matches_scope(wanted_scope, instance)
        precondition = checked_etag(etag)
        wanted_calendar = checked_calendar_url(calendar_url)
        edit = EventEdit(
            summary=(
                UNCHANGED if summary is None else _checked_summary(summary)
            ),
            start=(
                UNCHANGED if start is None else _checked_boundary(start, "start")
            ),
            end=UNCHANGED if end is None else _checked_boundary(end, "end"),
            description=(
                UNCHANGED
                if description is None
                else _checked_optional_text(description, "description")
            ),
            location=(
                UNCHANGED
                if location is None
                else _checked_optional_text(location, "location")
            ),
        )
        # Refused here as well as one layer down, so a change that cannot be
        # written is refused before a connection is opened at all.
        check_event_edit(edit)

        client = await client_provider()
        updated = await client.update_event(
            uid=wanted_uid,
            scope=wanted_scope,
            etag=precondition,
            edit=edit,
            recurrence_id=instance,
            calendar_url=wanted_calendar,
        )

        stored = _to_stored(updated.record)
        differences = (
            _update_differences(updated.record, updated.sent)
            if updated.record is not None
            else []
        )
        return EventUpdated(
            changed=updated.changed,
            change_note=CHANGED_NOTE if updated.changed else NO_OP_NOTE,
            uid=updated.uid,
            scope=updated.scope,
            recurrence_id=(
                format_instant(updated.recurrence_id)
                if updated.recurrence_id is not None
                else None
            ),
            href=updated.href,
            calendar_url=updated.calendar_url,
            calendar_name=updated.calendar_name,
            etag=updated.etag,
            etag_note=_update_etag_note(updated),
            sent=_written_values(updated.sent),
            stored=stored,
            stored_note=(
                UPDATE_READBACK_FAILED_NOTE.format(
                    reason=updated.readback_error or "the reason was not reported"
                )
                if stored is None
                else None
            ),
            differs_from_request=bool(differences),
            differences=differences,
            difference_note=(
                NO_COMPARISON_NOTE
                if stored is None
                else (DIFFERS_NOTE if differences else MATCHES_NOTE)
            ),
        )

    calendar_event_update.__name__ = UPDATE_TOOL_NAME
    return calendar_event_update


def _written_values(sent: dict) -> dict[str, str | None]:
    """The values that were written, spelled for an answer rather than a document.

    Surfaced because in the one case where it matters most -- the write landed
    and the readback did not -- the answer says the change WAS applied while
    carrying no stored values at all. Without this the caller is told a change
    happened and never told what it wrote.
    """
    written: dict[str, str | None] = {}
    for field, value in sent.items():
        if value is None:
            written[field] = None
        elif isinstance(value, datetime):
            written[field] = format_instant(value)
        elif isinstance(value, date):
            written[field] = value.isoformat()
        else:
            written[field] = str(value)
    return written


def _update_etag_note(updated: UpdatedEvent) -> str | None:
    """Why a changed object has no ETag, or nothing when it has one."""
    if updated.etag:
        return None
    if updated.record is None or updated.etag_unreadable:
        return UPDATE_ETAG_UNREADABLE_NOTE
    return NO_ETAG_NOTE


def _update_differences(record: EventRecord, sent: dict) -> list[str]:
    """Every named value the server stored differently from the request.

    Only the fields the caller named are compared, which is the difference from
    a creation: everything else was meant to stay as it was, and reporting an
    untouched value as a difference would say the server changed something
    nobody asked it about.

    The values compared against are the ones that were *written*, not the ones
    the caller spelled: composing truncates microseconds, and blaming the
    server for that would bury a real difference in noise.
    """
    differences: list[str] = []

    def note(field: str, requested: object, stored: object) -> None:
        differences.append(f"{field}: requested {requested!r}, stored {stored!r}")

    if "summary" in sent and (record.summary or "") != sent["summary"]:
        note("summary", sent["summary"], record.summary)
    if "start" in sent and not _same_moment(record.start, sent["start"]):
        note("start", format_instant(sent["start"]), format_instant(record.start))
    if "end" in sent and not _same_moment(record.end, sent["end"]):
        note("end", format_instant(sent["end"]), format_instant(record.end))
    if "description" in sent and (record.description or None) != sent["description"]:
        note("description", sent["description"], record.description)
    if "location" in sent and (record.location or None) != sent["location"]:
        note("location", sent["location"], record.location)
    if "start" in sent:
        all_day = not isinstance(sent["start"], datetime)
        if record.all_day != all_day:
            note("all_day", all_day, record.all_day)
    return differences


# -- removing one event ---------------------------------------------------


DELETE_TOOL_NAME = "calendar_event_delete"

#: What the answer says when one instance was cancelled.
CANCELLED_NOTE = (
    "The instance was cancelled: an exclusion was written into the series and "
    "the server accepted it. The series itself is untouched and every other "
    "instance keeps its own time."
)

#: The same, when the cancelled instance had been moved.
CANCELLED_WITH_OVERRIDE_NOTE = (
    "The instance was cancelled: an exclusion was written into the series and "
    "the separate entry holding this instance's own values was removed in the "
    "same write, because an exclusion and an entry for one moment contradict "
    "each other. The series itself is untouched and every other instance keeps "
    "its own time."
)

#: What the answer says when there was nothing to cancel.
ALREADY_GONE_NOTE = (
    "That instance was already cancelled, so no write was sent and nothing "
    "changed. It is not on the calendar, which is what was asked for; `etag` is "
    "still the one that was passed in, because a write storing the same "
    "exclusion again would bump the object's version and refuse the next "
    "precondition for a change that never happened."
)

#: What the answer says when the event itself was removed.
DELETED_NOTE = (
    "The event was deleted: the object holding it was removed from the "
    "calendar, and every instance of it went with it. This cannot be undone "
    "from here -- there is no undelete on this server."
)

#: What the answer says about a delete's one honest limitation.
DELETE_PRECONDITION_NOTE = (
    "The `etag` was compared against the version stored immediately before the "
    "delete, and the delete was sent only because the two matched. That is a "
    "check, not a guarantee: measured on this server, a delete carrying a "
    "stale precondition is answered as success and removes the object anyway, "
    "so `If-Match` cannot protect a deletion here. The comparison narrows the "
    "window between reading the event and removing it; it cannot close it, and "
    "a change made inside that window would not have been caught."
)

#: The same, when even the comparison could not be made.
DELETE_UNCHECKED_NOTE = (
    "The `etag` could NOT be compared before the delete: the server supplied no "
    "version for the object when it was read again immediately beforehand. The "
    "delete went ahead with no check of any kind -- this server ignores "
    "`If-Match` on a delete, measured, so nothing else was protecting it. If "
    "somebody changed the event after you read it, that change was removed with "
    "the event."
)

#: What the answer says when the series has nothing left in it.
SERIES_EMPTY_NOTE = (
    "The series now has no occurrences left: every instance of it is "
    "cancelled. The event object is still on the calendar -- nothing here "
    "removed it, and this server does not remove one as a side effect of "
    "cancelling its last instance. Delete it with `scope: series` if it should "
    "be gone."
)

#: What the answer says when the series still happens.
SERIES_REMAINS_NOTE = (
    "The series still has occurrences after this one: the rest of it is on the "
    "calendar exactly as it was."
)

#: The same, when the write existed only to repair a contradiction.
REPAIRED_CONTRADICTION_NOTE = (
    "That instance was already excluded from the series, and a separate entry "
    "holding its own values was still sitting beside the exclusion -- a "
    "contradiction different calendars resolve differently, which is why some "
    "readers were still showing the meeting. The entry was removed and the "
    "exclusion left as it was, so the instance is now off everywhere. The "
    "series itself is untouched and every other instance keeps its own time."
)

#: What the answer says when a cancellation was accepted but not seen to work.
CANCELLED_UNCONFIRMED_NOTE = (
    "The server accepted the cancellation of that instance, but this server "
    "could not see afterwards that it had taken effect -- "
    "`confirmation_note` says what was seen instead. It is not reported as "
    "cancelled on evidence nobody has. Read the event with "
    "`calendar_event_get` before doing anything else with it, and do not "
    "repeat the request first: it may already have been applied."
)

#: The same, for the removal of an event itself.
DELETE_UNCONFIRMED_NOTE = (
    "The server accepted the delete of this event, but reading the calendar "
    "afterwards did not show that the object is gone -- `confirmation_note` "
    "says what was seen instead. It is not reported as deleted on evidence "
    "nobody has. Read the event with `calendar_event_get` to see what is "
    "there, and do not send the delete again first: it may already have been "
    "applied, and a repeat would remove whatever now occupies that address."
)

#: What the answer says when the outcome could not be verified.
NOT_CONFIRMED_NOTE = (
    "The server accepted the request, but what it now holds could not be "
    "confirmed: {reason}. Read the event with `calendar_event_get` to see what "
    "is there -- and do not repeat the request until you have, because it may "
    "already have taken effect."
)

CONFIRMED_NOTE = (
    "Confirmed by reading the event back afterwards, not claimed from the "
    "server's answer to the request."
)

#: The same claim, for the one path where nothing was sent at all.  Saying
#: "read back afterwards" here would name a request that was never made and a
#: read that never happened.
ALREADY_GONE_CONFIRMED_NOTE = (
    "Confirmed by the read taken before anything would have been sent: that "
    "read is what showed the instance was already cancelled, which is why no "
    "write was sent and nothing was read after one. The evidence is that "
    "earlier read, not a later one."
)

#: A cancellation that *was* confirmed, on a readback that then broke off.
CONFIRMED_BUT_SERIES_UNREAD_NOTE = (
    "The cancellation itself was confirmed: the instance was read back from "
    "the server and it is off. What became of the rest of the series could "
    "not be read: {reason}. Do not repeat the cancellation -- it has taken "
    "effect. Read the event with `calendar_event_get` to see the series as it "
    "now stands."
)

#: Why `etag` is null after a cancellation the server accepted.
UPDATE_ETAG_AFTER_FAILED_READBACK_NOTE = (
    "`etag` is null because the object could not be read back after the write, "
    "not because the server supplied no version for it -- "
    "`confirmation_note` says what went wrong. Nothing was invented in its "
    "place, and the ETag passed in is spent either way: read the event with "
    "`calendar_event_get` for the current one."
)

#: What the answer says when the search for a surviving occurrence was cut short.
SERIES_UNDECIDED_NOTE = (
    "Whether the series still has an occurrence left could not be decided: "
    "every instance examined was cancelled and the recurrence rule has no end, "
    "so the search was stopped rather than run forever. This says nothing "
    "against the series -- the object is still on the calendar and may well "
    "still happen. Read it with `calendar_event_get`, or list the range you "
    "care about with `calendar_events_list`."
)

#: What the answer says about the ETag of an object that no longer exists.
DELETED_ETAG_NOTE = (
    "`etag` is null because the object is gone: a version is a precondition for "
    "a resource, and there is no resource. Nothing was invented in its place."
)


class EventDeleted(BaseModel):
    """One event, or one instance of one, as it stands after a removal."""

    deleted: bool = Field(
        description=(
            "True when this call removed something and the server accepted it. "
            "False only when there was nothing to remove because the instance "
            "was already cancelled -- `already_gone` says so, and it is not on "
            "the calendar either way. A removal that was attempted and did not "
            "happen is an error, never this model."
        )
    )
    already_gone: bool = Field(
        default=False,
        description=(
            "True when no request was sent because the instance was already "
            "cancelled. Never true for `scope: series`: an event that is not "
            "there is a not-found, not a quiet success."
        ),
    )
    delete_note: str = Field(
        description="What happened, in words: what was removed, and what was not."
    )
    uid: str = Field(description="Identifier of the event this call was about.")
    scope: str = Field(
        description=(
            f"What was removed. `{SCOPE_OCCURRENCE}`: the one instance named by "
            "`recurrence_id`, cancelled by an exclusion written into the "
            f"series, which is otherwise untouched. `{SCOPE_SERIES}`: the event "
            "object itself, and so every instance of it."
        )
    )
    recurrence_id: str | None = Field(
        default=None,
        description=(
            "Which instance was cancelled, as an ISO 8601 timestamp, or null "
            "when the event itself was deleted."
        ),
    )
    href: str = Field(
        description=(
            "CalDAV URL of the object this call acted on. For `scope: series` "
            "it is the address the delete was sent to."
        )
    )
    calendar_url: str = Field(
        description="The calendar it was in, as that calendar's listing gives it."
    )
    calendar_name: str = Field(description="Display name of that calendar.")
    etag: str | None = Field(
        default=None,
        description=(
            "Version of the object now, for use as a precondition next time. "
            "After a cancellation this is the new one -- the ETag passed in is "
            "no longer valid. Always null for `scope: series`, where the object "
            "no longer exists; see `etag_note`, and never a value invented here."
        ),
    )
    etag_note: str | None = Field(
        default=None,
        description="Why `etag` is null, or null when an ETag was returned.",
    )
    precondition_note: str | None = Field(
        default=None,
        description=(
            "For `scope: series` only: what the ETag check was worth. This "
            "server ignores `If-Match` on a delete, so the check is a "
            "comparison made immediately beforehand rather than a precondition "
            "the server enforced. Null for a cancellation, which is an ordinary "
            "conditional write and is genuinely protected."
        ),
    )
    occurrences_remaining: bool | None = Field(
        default=None,
        description=(
            "For `scope: occurrence`: whether the series still has any "
            "occurrence left after this one was cancelled. False means the "
            "object is still on the calendar holding a series that never "
            "happens again -- see `series_note`. Null for `scope: series`, "
            "which has nothing left to have occurrences, and null when the "
            "question could not be answered -- an endless rule whose every "
            "instance is cancelled is not walked forever. Null is never "
            "\"none left\": `series_note` says which of the two it is."
        ),
    )
    series_note: str | None = Field(
        default=None,
        description=(
            "What became of the series the cancelled instance belonged to, in "
            "words. Null when the event itself was deleted."
        ),
    )
    confirmed: bool = Field(
        description=(
            "True when the outcome was verified by reading the event back: for "
            "an instance, that it is cancelled; for a series, that the object "
            "is gone. Also true when nothing was sent because the instance was "
            "already cancelled: there the evidence is the read taken before, "
            "and `confirmation_note` says so rather than claiming a readback. "
            "False means it could not be verified, which is NOT the same as "
            "its having failed -- `confirmation_note` says what was seen."
        )
    )
    confirmation_note: str | None = Field(
        default=None,
        description=(
            "How the outcome was confirmed, or why it could not be. Never null."
        ),
    )
    stored: StoredEvent | None = Field(
        default=None,
        description=(
            "The series as the server holds it now -- the series, not the "
            "instance that was cancelled -- so a caller can see the rest of it "
            "survived. Read back after the write; when the instance was "
            "already cancelled and nothing was sent, it is the read that "
            "established that. Null for `scope: series`, where there is "
            "nothing left to read, and null when the read failed."
        ),
    )


def build_calendar_event_delete(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[EventDeleted]]:
    """Bind ``calendar_event_delete`` to a source of clients."""

    async def calendar_event_delete(
        uid: Annotated[
            str,
            Field(
                description=(
                    "Identifier of the event to remove, as "
                    "`calendar_events_list` or `calendar_event_get` returned "
                    "it. There is no deletion by title or by time: this "
                    "server's UID search returns the whole calendar, so a "
                    "lookup by anything else could delete the wrong meeting."
                )
            ),
        ],
        scope: Annotated[
            str,
            Field(
                description=(
                    f"What to remove. `{SCOPE_OCCURRENCE}` cancels the one "
                    "instance named by `recurrence_id` and leaves the rest of "
                    f"the series alone; `{SCOPE_SERIES}` deletes the event "
                    "itself, and so every instance of it -- and is also what a "
                    "one-off event takes. Required, with no default: the two "
                    "are not recoverable from each other and one of them is "
                    "irreversible."
                )
            ),
        ],
        etag: Annotated[
            str,
            Field(
                description=(
                    "The `etag` of the event as you last read it, from "
                    "`calendar_event_get`. Required for both scopes. Cancelling "
                    "an instance sends it as a precondition, which this server "
                    "honours. Deleting a series compares it against the stored "
                    "version immediately before removing the object, because "
                    "this server ignores `If-Match` on a delete -- a narrower "
                    "window, not a guarantee."
                )
            ),
        ],
        recurrence_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Which instance to cancel, as `calendar_events_list` "
                    f"returned it. Required with `scope: {SCOPE_OCCURRENCE}`, "
                    f"and refused with `scope: {SCOPE_SERIES}`, which would "
                    "contradict it."
                ),
            ),
        ] = None,
        calendar_url: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Restrict the lookup to one calendar, by the URL "
                    "`calendar_list` or `calendar_events_list` returned. Omit "
                    "to search every calendar on the account -- all of them, "
                    "not up to the first hit: a UID that is in more than one "
                    "is refused and names them, rather than being removed from "
                    "whichever calendar happens to be listed first. This "
                    "selects where the event is looked for; it never decides "
                    "what is removed."
                ),
            ),
        ] = None,
    ) -> EventDeleted:
        """Cancel one instance of an event, or delete the event itself.

        This is destructive, and `scope: series` is the least recoverable thing
        this server can do: the object is removed and there is no undelete.

        `scope` is required and has no default. `occurrence` cancels the one
        instance named by `recurrence_id`: an exclusion is written into the
        series, the series and every other instance are left exactly as they
        are, and if that instance had been moved, the entry holding its own
        values is removed in the same write -- an exclusion beside such an entry
        is a contradiction different calendars resolve differently. `series`
        deletes the event object, and every instance of it goes with it. On a
        calendar where most meetings recur the two are different acts, one of
        them destroys a year of history, and neither is guessed.

        `etag` is required, and is worth different things on the two paths.
        Cancelling an instance is an ordinary conditional write and this server
        honours the precondition: a change somebody else made in between is
        refused and nothing is written. **A deletion cannot be made conditional
        on this server** -- measured, a delete carrying a stale ETag was
        answered as success and removed the object anyway. So the ETag is
        compared against the stored version immediately before the delete
        instead. That narrows the window between your read and the removal; it
        does not close it -- it is a check and not a guarantee -- and this tool
        will not pretend otherwise.

        Ask the operator first before deleting every event in a calendar, or a
        calendar itself -- neither of which this tool does -- and before
        deleting anything you have not read: a title or a time is a search, and
        this server's search returns the whole calendar.

        Nothing beyond what you name is removed: not another instance, not
        another component, not another event that happens to share the object
        -- an object holding a second event is refused outright with
        `scope: series`, because a delete removes the object and would take
        that event too. A UID found in more than one calendar is refused the
        same way: pass `calendar_url` to say which one you mean.
        What the answer reports is read back from the server afterwards -- for
        an instance, that it is cancelled and the others survived; for a series,
        that the object is gone. When cancelling leaves a series with no
        occurrences at all, the answer says so and says the object is still
        there: this server does not remove one as a side effect.
        """
        wanted_uid = _checked_uid(uid)
        wanted_scope = checked_delete_scope(scope)
        instance = _checked_recurrence_id(recurrence_id)
        check_instance_matches_scope(wanted_scope, instance)
        precondition = checked_delete_etag(etag, scope=wanted_scope)
        wanted_calendar = checked_calendar_url(calendar_url)

        client = await client_provider()
        removed = await client.delete_event(
            uid=wanted_uid,
            scope=wanted_scope,
            etag=precondition,
            recurrence_id=instance,
            calendar_url=wanted_calendar,
        )

        return EventDeleted(
            deleted=removed.deleted,
            already_gone=removed.already_gone,
            delete_note=_delete_note(removed),
            uid=removed.uid,
            scope=removed.scope,
            recurrence_id=(
                format_instant(removed.recurrence_id)
                if removed.recurrence_id is not None
                else None
            ),
            href=removed.href,
            calendar_url=removed.calendar_url,
            calendar_name=removed.calendar_name,
            etag=removed.etag,
            etag_note=_delete_etag_note(removed),
            precondition_note=_precondition_note(removed),
            occurrences_remaining=removed.occurrences_remaining,
            series_note=_series_note(removed),
            confirmed=removed.confirmed,
            confirmation_note=_confirmation_note(removed),
            stored=_to_stored(removed.record) if removed.record is not None else None,
        )

    calendar_event_delete.__name__ = DELETE_TOOL_NAME
    return calendar_event_delete


def _delete_note(removed: DeletedEvent) -> str:
    """What happened, in the words a caller reads first.

    The unconfirmed case comes before the removed one on purpose. A caller
    reads this sentence and stops; telling them the event was deleted when
    nothing was seen to confirm it is the one thing this tool must not say.
    """
    if removed.scope == SCOPE_SERIES:
        return DELETED_NOTE if removed.confirmed else DELETE_UNCONFIRMED_NOTE
    if removed.already_gone:
        return ALREADY_GONE_NOTE
    if not removed.confirmed:
        return CANCELLED_UNCONFIRMED_NOTE
    if not removed.exclusion_added:
        # The exclusion was already there; the write removed the entry that
        # contradicted it. Reporting that as a cancellation would describe an
        # act nobody performed.
        return REPAIRED_CONTRADICTION_NOTE
    return CANCELLED_WITH_OVERRIDE_NOTE if removed.override_removed else CANCELLED_NOTE


def _delete_etag_note(removed: DeletedEvent) -> str | None:
    """Why there is no ETag, or nothing when there is one.

    The failed readback is asked about first: it is the reason the ETag is
    missing whenever it happened, and the alternatives -- "the server supplied
    none" and "the property could not be read" -- are both statements about a
    read that did not get that far.
    """
    if removed.etag:
        return None
    if removed.scope == SCOPE_SERIES:
        return DELETED_ETAG_NOTE
    if removed.confirmation_error:
        return UPDATE_ETAG_AFTER_FAILED_READBACK_NOTE
    if removed.record is None or removed.etag_unreadable:
        return UPDATE_ETAG_UNREADABLE_NOTE
    return NO_ETAG_NOTE


def _precondition_note(removed: DeletedEvent) -> str | None:
    """What the ETag check was worth on a path the server does not protect."""
    if removed.scope != SCOPE_SERIES:
        return None
    return (
        DELETE_PRECONDITION_NOTE
        if removed.precondition_rechecked
        else DELETE_UNCHECKED_NOTE
    )


def _series_note(removed: DeletedEvent) -> str | None:
    """What became of the series a cancelled instance belonged to."""
    if removed.scope == SCOPE_SERIES:
        return None
    if removed.occurrences_remaining is None:
        # Not silence: "we did not find out" is a different answer from "there
        # are none left", and the caller has to be able to tell them apart.
        return SERIES_UNDECIDED_NOTE
    return SERIES_REMAINS_NOTE if removed.occurrences_remaining else SERIES_EMPTY_NOTE


def _confirmation_note(removed: DeletedEvent) -> str:
    """How the outcome was established, or why it could not be.

    Four different things, and the wording of each names the evidence it
    actually has -- a readback after the write, a read taken before one that
    was never sent, a confirmed cancellation whose follow-up reads failed, or
    nothing.
    """
    if removed.already_gone:
        return ALREADY_GONE_CONFIRMED_NOTE
    if removed.confirmed and removed.confirmation_error:
        return CONFIRMED_BUT_SERIES_UNREAD_NOTE.format(
            reason=removed.confirmation_error
        )
    if removed.confirmed:
        return CONFIRMED_NOTE
    return NOT_CONFIRMED_NOTE.format(
        reason=removed.confirmation_error or "the reason was not reported"
    )
