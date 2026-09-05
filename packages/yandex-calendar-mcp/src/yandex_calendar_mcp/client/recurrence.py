"""Turning fetched calendar data into concrete occurrences.

This is the only module in the project that knows what an ``RRULE`` is.  Above
it, ``tools/`` sees plain occurrence records with their own start and end; below
it, ``caldav_client`` hands over raw iCalendar text exactly as the server sent
it.

Five deliberate choices:

* Expansion is delegated to ``recurring-ical-events`` (which arrives with
  ``caldav``) rather than hand-rolled.  ``RRULE``/``RDATE`` growth, ``EXDATE``
  cancellations and ``RECURRENCE-ID`` overrides are each an off-by-one-instance
  bug waiting to happen at a DST boundary.
* Components are grouped by ``(calendar, UID)`` across *every* fetched document
  before anything is expanded.  A ``RECURRENCE-ID`` override is frequently
  stored as a CalDAV object of its own; grouping per document would expand it
  standalone and return the instance it replaces twice.  The calendar is part of
  the key because the same ``UID`` in two calendars is a routine duplicate on a
  shared account, and the two are different occurrences.
* Each group is expanded on its own, so one malformed series costs one reported
  ``unreadable`` rather than the whole answer.  Nothing is ever dropped without
  being counted -- but a broken series that provably cannot intersect the
  requested window is not counted either, or one bad invite from years ago would
  make every future query report a loss it did not suffer.
* A naive timestamp is refused, not decorated.  A floating ``DTSTART`` has no
  offset to report, and inventing one moves the event for everybody at a
  different offset -- so its series is counted unreadable and said so.
* The window is closed at the start and open at the end, and it is the
  occurrence's *start* that must fall inside it.  An occurrence therefore
  belongs to exactly one of two adjacent ranges, never both and never neither.
  An event that began before the window and runs into it is not returned:
  reporting it would make the same meeting appear in every range it spans.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

import icalendar
import recurring_ical_events
from yandex_core.errors import ProtocolError

__all__ = [
    "CalendarSource",
    "Occurrence",
    "Expansion",
    "DEFAULT_CEILING",
    "expand",
    "occurrence_sort_key",
    "position_sort_key",
    "format_instant",
    "parse_instant",
]


@dataclass(frozen=True, slots=True)
class CalendarSource:
    """One iCalendar document as the server sent it, and where it came from."""

    ics: str
    calendar_url: str
    calendar_name: str


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One concrete instance of an event, with its own start and end.

    ``recurrence_id`` is set only when the occurrence belongs to a series; for a
    one-off event it stays ``None`` so the caller can tell the two apart without
    consulting anything else.
    """

    uid: str
    recurrence_id: str | None
    summary: str | None
    start: date | datetime
    end: date | datetime
    all_day: bool
    calendar_url: str
    calendar_name: str


@dataclass(frozen=True, slots=True)
class Expansion:
    """The occurrences in a range, and an honest account of what was left out."""

    occurrences: tuple[Occurrence, ...]

    unreadable: int = 0
    """How many events could not be read.

    An event whose own components are malformed counts once.  A whole document
    that will not parse at all counts once however many events it held, because
    the count of what is inside it is exactly what could not be read -- so this
    is a lower bound on the loss, never an overstatement.  Never a silent drop.
    """

    truncated: bool = False
    """True when the ceiling cut the list short; the answer is then partial."""

    unreadable_calendars: int = 0
    """How many calendars could not be read at all (a 403 on a shared one, say).

    Their occurrences are missing entirely and no cursor can retrieve them.
    """


#: How many occurrences one expansion may return before it is cut short.
#: A safety valve, not a page size: the tool's own ``limit`` is far smaller.
DEFAULT_CEILING = 2000

#: The order key a cursor position is expressed in.
SortKey = tuple[datetime, str, str, str]


def expand(
    sources: Iterable[CalendarSource],
    *,
    start: datetime,
    end: datetime,
    ceiling: int = DEFAULT_CEILING,
    after: SortKey | None = None,
) -> Expansion:
    """Expand fetched calendar documents into ordered occurrences in a range.

    The range filter is applied here, locally: Yandex's search is documented to
    answer with events outside the window, so nothing the server said about the
    range is load-bearing.  An occurrence is in the window when its own start is
    at or after ``start`` and strictly before ``end``.

    The ceiling is applied to what remains *after* ``after``, not to the whole
    ordered set.  Applying it first would make everything past it unreachable:
    a caller paging into the truncated tail would get an empty page with no
    cursor, which is the one answer this tool's contract says cannot happen.

    Args:
        sources: one entry per calendar object fetched.
        start: inclusive range start; must be timezone-aware.
        end: exclusive range end; must be timezone-aware.
        ceiling: most occurrences to keep, in order, before reporting truncation.
        after: resume strictly after this sort key, as a previous page's cursor
            named it.  ``None`` starts from the beginning of the range.

    Raises:
        ProtocolError: if ``ceiling`` is below one, which would mark every
            non-empty answer truncated and return none of it.
    """
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
        raise ProtocolError(
            f"`ceiling` must be a positive integer; got {ceiling!r}. A ceiling "
            "below one would report every answer as cut short while returning "
            "none of it."
        )

    parsed: list[tuple[icalendar.Calendar, CalendarSource]] = []
    unreadable = 0

    for source in sources:
        try:
            parsed.append((icalendar.Calendar.from_ical(source.ics), source))
        except Exception:  # noqa: BLE001 - any parse failure is one unreadable object
            unreadable += 1

    collected: list[Occurrence] = []

    for group in _group_by_calendar_and_uid(parsed):
        if group.uid is None:
            # An event with no UID cannot be identified, addressed, or
            # deduplicated against its own overrides. Reported, not dropped --
            # unless it provably could not have appeared in this window anyway.
            if _may_intersect(group.components, start=start, end=end):
                unreadable += len(group.components)
            continue
        try:
            occurrences = list(
                _expand_one_series(group, uid=group.uid, start=start, end=end)
            )
        except Exception:  # noqa: BLE001 - one bad series, one reported failure
            if _may_intersect(group.components, start=start, end=end):
                unreadable += len(group.components)
            continue
        collected.extend(occurrences)

    collected.sort(key=occurrence_sort_key)
    if after is not None:
        collected = [o for o in collected if occurrence_sort_key(o) > after]

    truncated = len(collected) > ceiling
    if truncated:
        collected = collected[:ceiling]

    return Expansion(
        occurrences=tuple(collected), unreadable=unreadable, truncated=truncated
    )


def with_unreadable_calendars(expansion: Expansion, count: int) -> Expansion:
    """The same expansion, told how many calendars could not be read at all."""
    return replace(expansion, unreadable_calendars=count)


class _NaiveTimestamp(Exception):
    """A floating timestamp, which has no offset this boundary could report."""


@dataclass(frozen=True, slots=True)
class _Group:
    """Every component sharing one ``UID`` inside one calendar."""

    calendar_url: str
    uid: str | None
    components: tuple[icalendar.Event, ...]
    documents: tuple[icalendar.Calendar, ...]
    source: CalendarSource


def _group_by_calendar_and_uid(
    parsed: Sequence[tuple[icalendar.Calendar, CalendarSource]],
) -> list[_Group]:
    """Every ``VEVENT`` fetched, grouped by calendar and ``UID``, order preserved.

    A series and its ``RECURRENCE-ID`` overrides share a ``UID`` and must be
    expanded together even when the server stored them as separate objects, or
    an override is expanded as an event of its own and the instance it replaces
    is returned twice.
    """
    order: list[tuple[str, str | None]] = []
    components: dict[tuple[str, str | None], list[icalendar.Event]] = {}
    documents: dict[tuple[str, str | None], list[icalendar.Calendar]] = {}
    sources: dict[tuple[str, str | None], CalendarSource] = {}

    for document, source in parsed:
        for component in document.walk("VEVENT"):
            raw_uid = component.get("UID")
            uid = str(raw_uid) if raw_uid is not None else None
            if uid is not None and not uid.strip():
                uid = None
            key = (source.calendar_url, uid)
            if key not in components:
                order.append(key)
                components[key] = []
                documents[key] = []
                sources[key] = source
            components[key].append(component)
            if document not in documents[key]:
                documents[key].append(document)

    return [
        _Group(
            calendar_url=key[0],
            uid=key[1],
            components=tuple(components[key]),
            documents=tuple(documents[key]),
            source=sources[key],
        )
        for key in order
    ]


def _expand_one_series(
    group: _Group, *, uid: str, start: datetime, end: datetime
) -> Iterator[Occurrence]:
    """Expand the components sharing one ``UID`` into occurrences in the range."""
    subset = _calendar_with(group)
    is_series = _is_series(group.components)
    source = group.source

    for expanded in recurring_ical_events.of(subset, components=["VEVENT"]).between(
        start, end
    ):
        if _is_cancelled(expanded):
            # A cancelled instance is not a meeting. This covers both shapes:
            # a whole event marked STATUS:CANCELLED, and a RECURRENCE-ID
            # override that cancels one instance of a live series.
            continue

        occurrence_start = _instant(expanded, "DTSTART")
        if occurrence_start is None:
            raise ValueError(f"event {uid!r} has no DTSTART")
        if not _in_window(occurrence_start, start=start, end=end):
            # The library answers with anything overlapping the window; the
            # contract is about where the occurrence *starts*.
            continue

        occurrence_end = _instant(expanded, "DTEND")
        if occurrence_end is None:
            # No DTEND and no usable DURATION: a zero-length occurrence is the
            # only honest reading, and is never guessed wider.
            occurrence_end = occurrence_start

        summary = expanded.get("SUMMARY")
        yield Occurrence(
            uid=uid,
            recurrence_id=(
                format_instant(_instant(expanded, "RECURRENCE-ID") or occurrence_start)
                if is_series
                else None
            ),
            summary=str(summary) if summary is not None else None,
            start=occurrence_start,
            end=occurrence_end,
            all_day=_is_all_day(occurrence_start),
            calendar_url=source.calendar_url,
            calendar_name=source.calendar_name,
        )


def _in_window(value: date | datetime, *, start: datetime, end: datetime) -> bool:
    """Start-inclusive, end-exclusive, judged on the occurrence's own start."""
    instant = _as_instant(value)
    return _as_instant(start) <= instant < _as_instant(end)


def _is_cancelled(component: icalendar.Event) -> bool:
    """Whether this component says it is a cancelled instance."""
    status = component.get("STATUS")
    return status is not None and str(status).strip().upper() == "CANCELLED"


def _calendar_with(group: _Group) -> icalendar.Calendar:
    """A copy of the documents carrying only one series' components.

    Timezone definitions and calendar-level properties are carried across from
    every document the group came from, or a ``TZID`` the events refer to would
    no longer resolve.
    """
    subset = icalendar.Calendar()
    seen_properties: set[str] = set()
    for document in group.documents:
        for name, value in document.property_items(recursive=False):
            if name in ("BEGIN", "END") or name in seen_properties:
                continue
            seen_properties.add(name)
            subset.add(name, value, encode=False)
        for child in document.subcomponents:
            if child.name != "VEVENT":
                subset.add_component(child)
    for component in group.components:
        subset.add_component(component)
    return subset


def _is_series(components: Sequence[icalendar.Event]) -> bool:
    """Whether these components describe a recurring series rather than one event.

    More than one component sharing a ``UID`` means at least one is a
    ``RECURRENCE-ID`` override, which only a series can have.
    """
    if len(components) > 1:
        return True
    component = components[0]
    return any(
        component.get(name) is not None for name in ("RRULE", "RDATE", "RECURRENCE-ID")
    )


def _may_intersect(
    components: Sequence[icalendar.Event], *, start: datetime, end: datetime
) -> bool:
    """Whether a series that failed to expand could have landed in the window.

    A broken event is only a loss if it might have been part of the answer.
    Without this, one malformed invite from years ago would make every future
    query report ``unreadable >= 1`` for ever, with no range narrow enough to
    escape it.  The check is deliberately generous: anything that cannot be
    ruled out counts.
    """
    return any(
        _component_may_intersect(component, start=start, end=end)
        for component in components
    )


def _component_may_intersect(
    component: icalendar.Event, *, start: datetime, end: datetime
) -> bool:
    begins = _raw_instant(component, "DTSTART")
    if begins is None:
        return True  # Unknown: cannot be ruled out.
    if begins >= _as_instant(end):
        # Nothing recurs before its own DTSTART.
        return False

    recurs = any(component.get(name) is not None for name in ("RRULE", "RDATE"))
    if not recurs:
        return begins >= _as_instant(start)

    until = _rrule_until(component)
    if until is not None and until < _as_instant(start):
        return False
    return True


def _raw_instant(component: icalendar.Event, name: str) -> datetime | None:
    """A comparable instant for a property, tolerating what ``_instant`` refuses.

    Used only to decide whether a broken event could have been in range, so a
    floating timestamp is read as UTC here rather than refused: the question is
    "could this be within a day of the window", not "what offset do we report".
    """
    try:
        field = component.get(name)
        if field is None:
            return None
        value = getattr(field, "dt", field)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
            value = getattr(value, "dt", value)
        if value is None:
            return None
        return _as_instant(value)
    except Exception:  # noqa: BLE001 - an unreadable property rules nothing out
        return None


def _rrule_until(component: icalendar.Event) -> datetime | None:
    """The ``UNTIL`` of an ``RRULE``, when it has a readable one."""
    try:
        rule = component.get("RRULE")
        if rule is None:
            return None
        until = rule.get("UNTIL") if hasattr(rule, "get") else None
        if not until:
            return None
        value = until[0] if isinstance(until, (list, tuple)) else until
        value = getattr(value, "dt", value)
        return _as_instant(value)
    except Exception:  # noqa: BLE001 - an unreadable rule rules nothing out
        return None


def _instant(component: icalendar.Event, name: str) -> date | datetime | None:
    """One date-or-datetime property, refusing anything without an offset."""
    field = component.get(name)
    if field is None:
        return None
    value = getattr(field, "dt", field)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise _NaiveTimestamp(
                f"{name} has no timezone; a floating time cannot be reported "
                "with an explicit offset."
            )
        return value
    if isinstance(value, date):
        return value
    raise ValueError(f"{name} is not a date or datetime")


def _is_all_day(value: date | datetime) -> bool:
    """An all-day value is a date. ``datetime`` subclasses ``date``, so order matters."""
    return not isinstance(value, datetime)


def occurrence_sort_key(occurrence: Occurrence) -> SortKey:
    """Deterministic total order: start, calendar, series UID, recurrence id.

    The calendar belongs in the key: the same ``UID`` in two calendars is a
    routine duplicate, and without it the two occurrences share a key, so a
    cursor resuming strictly after one would drop the other for good.
    """
    return position_sort_key(
        occurrence.start,
        occurrence.calendar_url,
        occurrence.uid,
        occurrence.recurrence_id,
    )


def position_sort_key(
    start: date | datetime,
    calendar_url: str,
    uid: str,
    recurrence_id: str | None,
) -> SortKey:
    """The same order, expressed over the four fields a cursor carries."""
    return (_as_instant(start), calendar_url, uid, recurrence_id or "")


def _as_instant(value: date | datetime) -> datetime:
    """A comparable instant in UTC.

    An all-day date sorts at midnight UTC, and is never stored that way.  A
    ``datetime`` is returned rather than a POSIX timestamp so that a year far
    outside the epoch orders correctly instead of raising ``OverflowError``.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def format_instant(value: date | datetime) -> str:
    """ISO 8601, keeping a date a date so nothing is coerced to midnight."""
    return value.isoformat()


def parse_instant(text: str) -> date | datetime:
    """Read back what :func:`format_instant` wrote.

    A value with a time component is a datetime and must carry an offset; one
    without is a date and stays one.
    """
    if "T" in text:
        value = datetime.fromisoformat(text)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime has no offset")
        return value
    return date.fromisoformat(text)
