"""The ``calendar_freebusy_query`` tool contract: when the account is busy.

Finding a free slot by listing events means reading every meeting's contents to
answer a question about time -- and it gets the answer wrong, because having an
event is not the same as being busy.

Four decisions carry this module:

* **The protocol's own free-busy report is never attempted.**  Measured against
  the live account, every calendar answers it with 400 Bad Request.  Attempting
  it and reporting the failure as "no busy time" would be the worst possible
  outcome -- an empty answer that reads as a free week -- so intervals are
  computed from the occurrences this server already expands.
* **Uncertain commitment stays uncertain.**  A large share of the invitations
  on a working account carry a tentative reply or none at all -- the spec for
  this story counts them against the live account.  A boolean busy/free answer
  would silently resolve every one of them in one direction, so tentative and
  unanswered time is reported as its own kind and never merged into certainty.
* **An event that does not consume time produces no interval.**  A transparent
  event, a declined invitation and one delegated to somebody else are all on
  the calendar and none of them takes up the hour.
* **Clipping is stated, never silent.**  An interval that begins at the range's
  edge is a different fact from a meeting that genuinely begins then, and a
  caller planning around it must be able to tell the two apart.

Nothing about a *meeting* leaves this module: no title, no description, no
attendee, no UID.  This tool answers about time only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from yandex_core.errors import ProtocolError
from yandex_core.paging import checked_limit, encode_position_cursor
from yandex_core.results import Page

from ..client.caldav_client import CalDAVCalendarClient
from ..client.recurrence import TRANSPARENCY_TRANSPARENT, Occurrence
from .timerange import (
    MAX_RANGE_DAYS,
    MIN_LIMIT,
    MORE_PAGES,
    RANGE_TRUNCATED,
    UNREADABLE_DATA,
    IncompleteReason,
    check_range,
    checked_calendar_url,
    checked_instant,
    decoded_position,
    incomplete_reasons,
    query_stamp,
)

__all__ = [
    "TOOL_NAME",
    "BUSY",
    "BUSY_TENTATIVE",
    "BUSY_UNANSWERED",
    "KINDS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "MAX_RANGE_DAYS",
    "MORE_PAGES",
    "RANGE_TRUNCATED",
    "UNREADABLE_DATA",
    "CLIPPED_START_NOTE",
    "CLIPPED_END_NOTE",
    "CLIPPED_BOTH_NOTE",
    "IncompleteReason",
    "Kind",
    "BusyInterval",
    "FreeBusyPage",
    "merge_intervals",
    "build_calendar_freebusy_query",
]

TOOL_NAME = "calendar_freebusy_query"

#: How firm a commitment is.  Spelled as a literal as well as constants so the
#: permitted values reach the JSON schema an MCP client actually reads.
Kind = Literal["busy", "busy-tentative", "busy-unanswered"]

#: Time the account is committed to: an opaque event it has accepted, or one it
#: owns and was never asked to reply to.
BUSY: Kind = "busy"

#: Time the account has said *maybe* to.  Reported apart from ``busy`` because
#: it is a different fact, and folding it either way decides for the caller.
BUSY_TENTATIVE: Kind = "busy-tentative"

#: Time the account has been invited to and has not answered.  Distinct from
#: ``busy-tentative``: "I might" and "I have not looked" are not the same thing,
#: and the second is the one a caller can still do something about.
BUSY_UNANSWERED: Kind = "busy-unanswered"

#: Every kind this tool can report, most committed first.
KINDS: tuple[Kind, ...] = (BUSY, BUSY_TENTATIVE, BUSY_UNANSWERED)

#: How the caller's own reply maps to a kind.  Anything not named here -- an
#: absent reply on an event the operator created, ``ACCEPTED``, or a status this
#: server has never seen -- is committed time: the conservative reading is the
#: one that never quietly frees up an hour that is taken.
_STATUS_KINDS = {
    "TENTATIVE": BUSY_TENTATIVE,
    "NEEDS-ACTION": BUSY_UNANSWERED,
}

#: Replies that mean the account is not attending, and so is not busy.
#: ``DELEGATED`` belongs here beside ``DECLINED``: it is the reply that says the
#: invitation has been handed to somebody else, so the hour is no longer this
#: account's.  Letting it fall through to plain busy would block time the
#: operator gave away.
_NOT_ATTENDING = frozenset({"DECLINED", "DELEGATED"})

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

CLIPPED_START_NOTE = (
    "This interval began before `start` and has been cut to it; the busy time "
    "extends earlier than the range asked about."
)
CLIPPED_END_NOTE = (
    "This interval runs past `end` and has been cut to it; the busy time "
    "extends later than the range asked about."
)
CLIPPED_BOTH_NOTE = (
    "This interval both began before `start` and runs past `end`; it has been "
    "cut to the range at both ends and the busy time extends beyond it."
)

#: The fields a cursor carries to name one interval and the query it answered.
_CURSOR_FIELDS = ("start", "kind", "query")

ClientProvider = Callable[[], Awaitable[CalDAVCalendarClient]]


class BusyInterval(BaseModel):
    """One stretch of time the account is committed to, and how firmly."""

    start: datetime = Field(
        description=(
            "When this busy stretch begins: a timestamp with an explicit UTC "
            "offset, never a bare date. An all-day event occupies its whole day "
            "in the offset of the `start` this query was asked with."
        )
    )
    end: datetime = Field(
        description="When it ends, exclusive, in the same form as `start`."
    )
    kind: Kind = Field(
        description=(
            f"How firm this commitment is. `{BUSY}`: the account is committed -- "
            "an event it owns, or an invitation it accepted. "
            f"`{BUSY_TENTATIVE}`: it replied *maybe*. "
            f"`{BUSY_UNANSWERED}`: it was invited and has not replied. The three "
            "are never merged into one another, because deciding which way an "
            "uncertain commitment falls is the caller's decision to make, not "
            "this server's. Intervals of different kinds may overlap."
        )
    )
    clipped_start: bool = Field(
        default=False,
        description=(
            "True when the busy time began before `start` and this interval was "
            "cut to the range. An interval that merely begins at the edge is a "
            "different fact from a meeting that genuinely begins then."
        ),
    )
    clipped_end: bool = Field(
        default=False,
        description=(
            "True when the busy time runs past `end` and this interval was cut "
            "to the range."
        ),
    )
    clipping_note: str | None = Field(
        default=None,
        description=(
            "The clipping said in words, or null when this interval lies wholly "
            "inside the range. Never true silently: whenever `clipped_start` or "
            "`clipped_end` is set, this says so as well."
        ),
    )


class FreeBusyPage(Page[BusyInterval]):
    """Merged busy intervals, honest about anything the range could not report."""

    unreadable: int = Field(
        default=0,
        description=(
            "How many events in the range could not be read at all. Their time "
            "is missing from these intervals -- the account may be busier than "
            "this answer shows -- and no cursor can retrieve them, because the "
            "data itself is malformed."
        ),
    )
    unreadable_calendars: int = Field(
        default=0,
        description=(
            "How many calendars could not be read at all. Everything in them is "
            "missing from these intervals and no cursor can retrieve it. Above "
            "zero, an empty answer must never be read as a free range."
        ),
    )
    incomplete_reasons: list[IncompleteReason] = Field(
        default_factory=list,
        description=(
            "Every way this answer falls short, or empty when `complete` is "
            "true. More than one can apply at once, and each is listed: told "
            "only the first, a caller that stops paging would never learn the "
            "rest. "
            f"`{MORE_PAGES}`: more intervals match; pass `next_cursor` back. "
            f"`{RANGE_TRUNCATED}`: the range holds more occurrences than one "
            "expansion may carry, so intervals are missing from its later part; "
            "no cursor fixes that -- ask about a narrower range. "
            f"`{UNREADABLE_DATA}`: some events or calendars could not be read; "
            "see `unreadable` and `unreadable_calendars`. No cursor fixes this "
            "one either."
        ),
    )


def build_calendar_freebusy_query(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[FreeBusyPage]]:
    """Bind ``calendar_freebusy_query`` to a source of clients."""

    async def calendar_freebusy_query(
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
                    "returned. Omit, or pass an empty string, to use every "
                    "calendar on the account."
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
                    f"Maximum intervals to return (default {DEFAULT_LIMIT}, "
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
                    "to the range it was issued for; pass that back unchanged "
                    "with it."
                ),
            ),
        ] = None,
    ) -> FreeBusyPage:
        """Report when the configured account is busy over a date range.

        Answers about *time*, not about meetings: no title, description,
        attendee or event id is returned. Use `calendar_events_list` when you
        need to know what a commitment actually is.

        Intervals are merged, so touching or overlapping commitments of the same
        kind come back as one stretch. `kind` says how firm each stretch is --
        `busy`, `busy-tentative` or `busy-unanswered` -- and the three are never
        merged into one another: a quarter of a typical account is tentative or
        unanswered, and collapsing that into a yes or a no would decide for you.
        Intervals of different kinds may therefore overlap.

        An event that does not consume time produces no interval at all: one
        marked transparent (free), an invitation this account declined or
        delegated to somebody else, and an event of zero length. Everything
        else is busy, including an event this account created and was never
        asked to reply to.

        A meeting that began before `start`, or runs past `end`, is included and
        clipped to the range, and `clipped_start` / `clipped_end` say so. An
        all-day event occupies its whole day in the offset of the `start` you
        asked with.

        Both `start` and `end` are required and must carry an explicit UTC
        offset. A range wider than the documented maximum is refused by name
        rather than narrowed. An empty range is an empty, complete answer -- not
        an error. If `complete` is false, read `incomplete_reasons`: an empty or
        short answer with events or calendars this server could not read must
        never be taken as free time.
        """
        window_start = checked_instant(start, "start")
        window_end = checked_instant(end, "end")
        check_range(window_start, window_end)
        limit_used = checked_limit(limit, minimum=MIN_LIMIT, maximum=MAX_LIMIT)
        wanted_calendar = checked_calendar_url(calendar_url)

        query = query_stamp(
            start=window_start, end=window_end, calendar_url=wanted_calendar
        )
        after = _position_from(cursor, query=query)

        client = await client_provider()
        expansion = await client.list_occurrences(
            start=window_start,
            end=window_end,
            calendar_url=wanted_calendar,
            # A question about busy time needs the meeting that started last
            # night and is still running, which the listing rule excludes.
            overlap=True,
        )

        collected: list[BusyInterval] = []
        corrupt = 0
        for occurrence in expansion.occurrences:
            if _ends_before_it_begins(occurrence):
                # No interval comes of it, and saying nothing would report the
                # hour as free. Counted with the events that would not parse:
                # the time is missing either way.
                corrupt += 1
                continue
            interval = _interval_for(
                occurrence, start=window_start, end=window_end
            )
            if interval is not None:
                collected.append(interval)
        intervals = merge_intervals(collected)
        unreadable = expansion.unreadable + corrupt

        remaining = (
            [i for i in intervals if _position(i) > after]
            if after is not None
            else intervals
        )
        page = remaining[:limit_used]
        more_remain = len(remaining) > len(page)
        lost = bool(unreadable or expansion.unreadable_calendars)
        reasons = incomplete_reasons(
            more_remain=more_remain, truncated=expansion.truncated, lost=lost
        )

        return FreeBusyPage(
            items=page,
            complete=not reasons,
            next_cursor=(
                encode_position_cursor(
                    {
                        "start": page[-1].start.isoformat(),
                        "kind": page[-1].kind,
                        "query": query,
                    },
                    tool=TOOL_NAME,
                )
                if more_remain
                else None
            ),
            unreadable=unreadable,
            unreadable_calendars=expansion.unreadable_calendars,
            incomplete_reasons=list(reasons),
        )

    calendar_freebusy_query.__name__ = TOOL_NAME
    return calendar_freebusy_query


def _interval_for(
    occurrence: Occurrence, *, start: datetime, end: datetime
) -> BusyInterval | None:
    """The busy interval one occurrence contributes, or ``None`` for no time.

    Three ways an occurrence consumes nothing, and each is a deliberate
    omission rather than an oversight: it is marked transparent, this account
    declined or delegated it, or it has no duration at all.  An interval of zero length
    reports no busy time, so returning one would only invite a caller to treat
    an instant as an obstacle.
    """
    kind = _kind_of(occurrence)
    if kind is None:
        return None

    offset = start.tzinfo
    begins = _as_moment(occurrence.start, offset)
    finishes = _as_moment(occurrence.end, offset)
    if occurrence.all_day and finishes <= begins:
        # An all-day event with no DTEND, or one whose DTEND repeats DTSTART,
        # still occupies its day: iCalendar's end date is exclusive, so a
        # single-day event ends on the following midnight.
        finishes = begins + timedelta(days=1)
    if finishes <= begins:
        return None

    clipped_start = begins < start
    clipped_end = finishes > end
    begins = max(begins, start)
    finishes = min(finishes, end)
    if finishes <= begins:
        return None

    return BusyInterval(
        start=begins,
        end=finishes,
        kind=kind,
        clipped_start=clipped_start,
        clipped_end=clipped_end,
        clipping_note=_clipping_note(clipped_start, clipped_end),
    )


def _clipping_note(clipped_start: bool, clipped_end: bool) -> str | None:
    if clipped_start and clipped_end:
        return CLIPPED_BOTH_NOTE
    if clipped_start:
        return CLIPPED_START_NOTE
    if clipped_end:
        return CLIPPED_END_NOTE
    return None


def _ends_before_it_begins(occurrence: Occurrence) -> bool:
    """Whether this occurrence finishes before it starts: corrupt, not merely odd.

    An event of zero length is legitimate and simply consumes no time.  One that
    ends *before* it begins cannot be read as an interval at all, so it is
    counted rather than dropped.
    """
    begins, finishes = occurrence.start, occurrence.end
    if isinstance(begins, datetime) != isinstance(finishes, datetime):
        return False
    return finishes < begins


def _kind_of(occurrence: Occurrence) -> Kind | None:
    """How firm this occurrence's commitment is, or ``None`` for none at all."""
    if occurrence.transparency == TRANSPARENCY_TRANSPARENT:
        return None
    status = (occurrence.participation_status or "").strip().upper()
    if status in _NOT_ATTENDING:
        return None
    return _STATUS_KINDS.get(status, BUSY)


def _as_moment(value: date | datetime, offset) -> datetime:
    """A bound as an instant, giving an all-day date its whole day.

    A date has no offset of its own, and reading it as midnight UTC would block
    the wrong 24 hours for everybody east or west of it.  The offset used is the
    one the caller asked the range in, which is the only statement about which
    day they mean that this server actually has.
    """
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=offset)


def merge_intervals(intervals: list[BusyInterval]) -> list[BusyInterval]:
    """Merge touching and overlapping intervals, but only within one kind.

    Two intervals of the same kind reported with no gap between them read as a
    gap that is not there.  Two intervals of *different* kinds must stay apart,
    however they overlap: merging them would decide whether an unanswered
    invitation counts, which is the caller's decision.

    The result is ordered by start, then by kind in descending order of
    commitment, so the firmest reading of any moment comes first.
    """
    merged: list[BusyInterval] = []
    for kind in KINDS:
        same_kind = sorted(
            (i for i in intervals if i.kind == kind), key=lambda i: (i.start, i.end)
        )
        current: BusyInterval | None = None
        for interval in same_kind:
            if current is None:
                current = interval
                continue
            if interval.start <= current.end:
                # Clipping survives a merge in both directions. An absorbed
                # interval that reaches the range's edge says the busy time
                # continues past it, and that stays true whether or not it
                # happens to extend the merged end.
                clipped_start = current.clipped_start or interval.clipped_start
                clipped_end = current.clipped_end or interval.clipped_end
                current = current.model_copy(
                    update={
                        "end": max(current.end, interval.end),
                        "clipped_start": clipped_start,
                        "clipped_end": clipped_end,
                        "clipping_note": _clipping_note(clipped_start, clipped_end),
                    }
                )
                continue
            merged.append(current)
            current = interval
        if current is not None:
            merged.append(current)
    merged.sort(key=_position)
    return merged


def _position(interval: BusyInterval) -> tuple[datetime, int]:
    """The total order a cursor resumes from: start, then firmest kind first."""
    return (interval.start, KINDS.index(interval.kind))


def _position_from(cursor: str | None, *, query: str) -> tuple[datetime, int] | None:
    """The position of the last interval a previous page returned.

    Intervals are derived from the same events every time, so the range is
    simply asked for again and everything at or before this position dropped.
    That costs a refetch and buys determinism: the alternative, resuming at an
    occurrence, would split a merge across a page boundary and report a gap
    between two halves of one meeting.
    """
    if cursor is None:
        return None
    position = decoded_position(
        cursor,
        tool=TOOL_NAME,
        fields=_CURSOR_FIELDS,
        query=query,
        restate="`start`, `end` and `calendar_url`",
    )
    raw_start = position["start"]
    kind = position["kind"]
    if not isinstance(raw_start, str) or kind not in KINDS:
        raise ProtocolError("Cursor is not a cursor this server issued.")
    try:
        moment = datetime.fromisoformat(raw_start)
    except (ValueError, OverflowError, OSError) as exc:
        raise ProtocolError("Cursor is not a cursor this server issued.") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ProtocolError("Cursor is not a cursor this server issued.")
    return (moment, KINDS.index(kind))
