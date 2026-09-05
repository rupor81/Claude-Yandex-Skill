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

import hashlib
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from pydantic import BaseModel, Field
from yandex_core.errors import ProtocolError
from yandex_core.paging import (
    checked_limit,
    decode_position_cursor,
    encode_position_cursor,
)
from yandex_core.results import Page

from ..client.caldav_client import CalDAVCalendarClient
from ..client.recurrence import (
    Occurrence,
    format_instant,
    parse_instant,
    position_sort_key,
)

__all__ = [
    "EventOccurrence",
    "EventPage",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "MAX_RANGE_DAYS",
    "TOOL_NAME",
    "MORE_PAGES",
    "RANGE_TRUNCATED",
    "UNREADABLE_DATA",
    "build_calendar_events_list",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MIN_LIMIT = 1

#: The widest span one call may ask for.  A range beyond this is refused by
#: name rather than narrowed: a quietly shortened window returns an answer that
#: looks complete for a question nobody asked.
MAX_RANGE_DAYS = 366

TOOL_NAME = "calendar_events_list"

#: The three reasons a page may not be the whole answer.
MORE_PAGES = "more_pages"
RANGE_TRUNCATED = "range_truncated"
UNREADABLE_DATA = "unreadable_data"

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
        window_start = _checked_instant(start, "start")
        window_end = _checked_instant(end, "end")
        _check_range(window_start, window_end)
        limit_used = checked_limit(limit, minimum=MIN_LIMIT, maximum=MAX_LIMIT)
        wanted_calendar = _checked_calendar_url(calendar_url)
        needle = _checked_title(title_contains)

        query = _query_stamp(
            start=window_start,
            end=window_end,
            calendar_url=wanted_calendar,
            title_contains=needle,
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
        reason = _incomplete_reason(
            more_remain=more_remain, truncated=expansion.truncated, lost=lost
        )

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


def _incomplete_reason(
    *, more_remain: bool, truncated: bool, lost: bool
) -> str | None:
    """Which of the three shortfalls to name, resumable ones first."""
    if more_remain:
        return MORE_PAGES
    if truncated:
        return RANGE_TRUNCATED
    if lost:
        return UNREADABLE_DATA
    return None


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


def _query_stamp(
    *,
    start: datetime,
    end: datetime,
    calendar_url: str | None,
    title_contains: str | None,
) -> str:
    """A short fingerprint of the question a cursor was issued for.

    Without it, page one's cursor replayed with a different range decodes
    cleanly and resumes a different question -- the same quiet wrong answer the
    tool-name stamp already refuses for a different tool's cursor.
    """
    parts = (
        start.astimezone(timezone.utc).isoformat(),
        end.astimezone(timezone.utc).isoformat(),
        calendar_url or "",
        title_contains or "",
    )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def _position_from(cursor: str | None, *, query: str):
    """The sort key of the last occurrence a previous page returned."""
    if cursor is None:
        return None
    position = decode_position_cursor(cursor, tool=TOOL_NAME, fields=_CURSOR_FIELDS)
    raw_start = position["start"]
    calendar_url = position["calendar_url"]
    uid = position["uid"]
    if (
        not isinstance(raw_start, str)
        or not isinstance(calendar_url, str)
        or not isinstance(uid, str)
    ):
        raise ProtocolError("Cursor is not a cursor this server issued.")
    if position["query"] != query:
        raise ProtocolError(
            "This cursor was issued for a different question. A cursor resumes "
            "one range with one set of filters; pass `start`, `end`, "
            "`calendar_url` and `title_contains` back exactly as they were, or "
            "start again without a cursor."
        )
    try:
        start = parse_instant(raw_start)
        return position_sort_key(start, calendar_url, uid, position["recurrence_id"])
    except (ValueError, OverflowError, OSError) as exc:
        # A timestamp that parses but names a moment no clock can hold is still
        # not a position this server ever issued.
        raise ProtocolError("Cursor is not a cursor this server issued.") from exc


def _checked_instant(value: object, name: str) -> datetime:
    """A required, timezone-aware moment.

    Strings are accepted so the function behaves the same when called directly
    as it does through the protocol, where pydantic parses them. A naive value
    is refused here, before any request is made: a moment with no offset means
    something different to everyone who reads it.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ProtocolError(
                f"`{name}` is not an ISO 8601 timestamp: {value!r}."
            ) from exc
    if not isinstance(value, datetime):
        raise ProtocolError(
            f"`{name}` must be an ISO 8601 timestamp with an explicit UTC offset, "
            f"not {type(value).__name__}."
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolError(
            f"`{name}` has no UTC offset. Give an explicit one, for example "
            f"2026-06-01T00:00:00+03:00; a naive timestamp means a different "
            "moment to every reader."
        )
    return value


def _checked_calendar_url(calendar_url: object) -> str | None:
    """One calendar, or every calendar.

    A blank or whitespace-only string is the caller saying "no restriction", not
    a query against a calendar whose URL is the empty string.
    """
    if calendar_url is None:
        return None
    if not isinstance(calendar_url, str):
        raise ProtocolError(
            f"`calendar_url` must be a string, not {type(calendar_url).__name__}."
        )
    trimmed = calendar_url.strip()
    return trimmed or None


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


def _check_range(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ProtocolError(
            f"`end` must be after `start`; got start={start.isoformat()} and "
            f"end={end.isoformat()}."
        )
    span = end - start
    if span > timedelta(days=MAX_RANGE_DAYS):
        raise ProtocolError(
            f"The range spans {span.days} days, beyond the {MAX_RANGE_DAYS}-day "
            "maximum for one query. Ask for a narrower range; it is not narrowed "
            "for you, because a shortened window would answer a different question."
        )
