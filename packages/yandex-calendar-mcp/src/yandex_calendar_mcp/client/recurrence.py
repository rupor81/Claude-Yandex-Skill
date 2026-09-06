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

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

import icalendar
import recurring_ical_events
from yandex_core.errors import ProtocolError

__all__ = [
    "CalendarSource",
    "Occurrence",
    "TRANSPARENCY_OPAQUE",
    "TRANSPARENCY_TRANSPARENT",
    "Expansion",
    "DEFAULT_CEILING",
    "expand",
    "occurrence_sort_key",
    "position_sort_key",
    "format_instant",
    "parse_instant",
    "EventNotInDocument",
    "InstanceNotInSeries",
    "Participant",
    "EventRecord",
    "SCOPE_SINGLE",
    "SCOPE_SERIES",
    "SCOPE_OCCURRENCE",
    "read_event",
]


#: What ``TRANSP`` says when an event blocks time.  iCalendar's own default.
TRANSPARENCY_OPAQUE = "OPAQUE"

#: What ``TRANSP`` says when an event is on the calendar but consumes no time.
TRANSPARENCY_TRANSPARENT = "TRANSPARENT"


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

    transparency: str = TRANSPARENCY_OPAQUE
    """Whether this occurrence consumes time: ``OPAQUE`` or ``TRANSPARENT``.

    iCalendar's default is ``OPAQUE``, and that default is applied here rather
    than left to each caller: an event with no ``TRANSP`` line does block time,
    and a caller reading ``None`` as "unknown" would have to guess which way.
    """

    participation_status: str | None = None
    """The *operator's own* reply to this event, as iCalendar spells it.

    ``ACCEPTED``, ``DECLINED``, ``TENTATIVE``, ``NEEDS-ACTION`` -- or ``None``
    when the operator is not on the attendee list at all, which is the normal
    shape of an event they created themselves.  Nobody else's reply is ever
    read into this field: another attendee's ``PARTSTAT`` is a fact about their
    calendar, not this one's.
    """


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
    operator: str | None = None,
    operator_domains: Sequence[str] = (),
    overlap: bool = False,
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
        operator: the configured account's address, used to pick *its own*
            ``ATTENDEE`` line out of an invitation.  ``None`` leaves every
            occurrence's ``participation_status`` unset rather than guessing
            whose reply to read.
        operator_domains: the mail domains the account actually owns, for a
            login written without one.  Only these turn a bare login into a
            full address: an attendee on any other domain is somebody else,
            however alike the two local parts look.
        overlap: keep occurrences that merely *overlap* the window instead of
            only those starting inside it.  Off by default, because the listing
            contract needs an occurrence to belong to exactly one of two
            adjacent ranges.  A question about busy *time* needs the other rule:
            a meeting that began yesterday and runs until noon takes up this
            morning whether or not it started in it.

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
    addresses = _operator_addresses(operator, operator_domains)

    for group in _group_by_calendar_and_uid(parsed):
        if group.uid is None:
            # An event with no UID cannot be identified, addressed, or
            # deduplicated against its own overrides. Reported, not dropped --
            # unless it provably could not have appeared in this window anyway.
            if _may_intersect(
                group.components, start=start, end=end, overlap=overlap
            ):
                unreadable += len(group.components)
            continue
        try:
            occurrences = list(
                _expand_one_series(
                    group,
                    uid=group.uid,
                    start=start,
                    end=end,
                    addresses=addresses,
                    overlap=overlap,
                )
            )
        except Exception:  # noqa: BLE001 - one bad series, one reported failure
            if _may_intersect(
                group.components, start=start, end=end, overlap=overlap
            ):
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
    group: _Group,
    *,
    uid: str,
    start: datetime,
    end: datetime,
    addresses: tuple[str, ...] = (),
    overlap: bool = False,
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
        occurrence_end = _end_instant(expanded, occurrence_start)

        if overlap:
            if not _overlaps_window(
                occurrence_start, occurrence_end, start=start, end=end
            ):
                continue
        elif not _in_window(occurrence_start, start=start, end=end):
            # The library answers with anything overlapping the window; the
            # listing contract is about where the occurrence *starts*.
            continue

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
            transparency=_transparency(expanded),
            participation_status=_participation_status(expanded, addresses),
        )


def _in_window(value: date | datetime, *, start: datetime, end: datetime) -> bool:
    """Start-inclusive, end-exclusive, judged on the occurrence's own start."""
    instant = _as_instant(value)
    return _as_instant(start) <= instant < _as_instant(end)


def _overlaps_window(
    occurrence_start: date | datetime,
    occurrence_end: date | datetime,
    *,
    start: datetime,
    end: datetime,
) -> bool:
    """Whether an occupied span touches the window at all.

    Half-open at both ends, so an occurrence that ends exactly when the window
    opens does not overlap it and one that starts exactly when it closes belongs
    to the next window.  An occurrence of zero length occupies no span to
    intersect, so it is judged on its start alone rather than being dropped
    silently for having no width.
    """
    begins = _as_instant(occurrence_start)
    finishes = _as_instant(occurrence_end)
    window_start = _as_instant(start)
    window_end = _as_instant(end)
    if finishes <= begins:
        return window_start <= begins < window_end
    return begins < window_end and finishes > window_start


def _transparency(component: icalendar.Event) -> str:
    """Whether this component consumes time, defaulting the way iCalendar does.

    Absent ``TRANSP`` means ``OPAQUE`` by the specification, so the default is
    applied here rather than reported as unknown: an event with no such line
    genuinely does block time, and passing the ambiguity upwards would only move
    the same guess somewhere with less information.
    """
    value = component.get("TRANSP")
    if value is None:
        return TRANSPARENCY_OPAQUE
    text = str(value).strip().upper()
    return TRANSPARENCY_TRANSPARENT if text == TRANSPARENCY_TRANSPARENT else (
        TRANSPARENCY_OPAQUE
    )


def _operator_addresses(
    operator: str | None, domains: Sequence[str] = ()
) -> tuple[str, ...]:
    """Every spelling of the configured account's own address, case-folded.

    A profile login is written either way -- ``me`` or ``me@example.com`` -- and
    an invitation always carries the full address, so a bare login has to be
    completed before it can match anything.  The domains used are the ones the
    account actually owns: the login's own, plus whatever the caller derived
    from the account it is connected to.  Nothing is assumed beyond that, in
    either direction.  Guessing a domain would read every invitation on a
    custom-domain account as unanswered, and matching a local part on *any*
    domain would let ``me@somewhere-else.example`` decline this account's
    meetings for it.
    """
    if not operator:
        return ()
    value = operator.strip().casefold()
    if not value:
        return ()
    local, _, own_domain = value.partition("@")
    owned = [own_domain] if own_domain else []
    for domain in domains:
        cleaned = (domain or "").strip().casefold().lstrip("@")
        if cleaned and cleaned not in owned:
            owned.append(cleaned)
    if not local:
        return (value,)
    spellings = [value, *(f"{local}@{domain}" for domain in owned)]
    return tuple(dict.fromkeys(spellings))


def _participation_status(
    component: icalendar.Event, addresses: tuple[str, ...]
) -> str | None:
    """The operator's own ``PARTSTAT`` on this component, or ``None``.

    Only a line addressed to the configured account is read, and "addressed to"
    means the whole address: another attendee's reply describes their
    availability, not this account's, and a shared local part on a domain this
    account does not own is a stranger with the same name.
    """
    if not addresses:
        return None
    for person in _participants(component, "ATTENDEE"):
        email = (person.email or "").strip().casefold()
        if not email:
            continue
        if email in addresses:
            status = (person.response_status or "").strip().upper()
            return status or None
    return None


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
    components: Sequence[icalendar.Event],
    *,
    start: datetime,
    end: datetime,
    overlap: bool = False,
) -> bool:
    """Whether a series that failed to expand could have landed in the window.

    A broken event is only a loss if it might have been part of the answer.
    Without this, one malformed invite from years ago would make every future
    query report ``unreadable >= 1`` for ever, with no range narrow enough to
    escape it.  The check is deliberately generous: anything that cannot be
    ruled out counts.

    ``overlap`` must be the same rule the expansion is running under.  Under the
    overlap rule a meeting that began before the window is part of the answer,
    so ruling one out by its start would drop it with nothing counted -- an
    answer that reads as free time.
    """
    return any(
        _component_may_intersect(component, start=start, end=end, overlap=overlap)
        for component in components
    )


def _component_may_intersect(
    component: icalendar.Event, *, start: datetime, end: datetime, overlap: bool = False
) -> bool:
    begins = _raw_instant(component, "DTSTART")
    if begins is None:
        return True  # Unknown: cannot be ruled out.
    if begins >= _as_instant(end):
        # Nothing recurs before its own DTSTART.
        return False

    recurs = any(component.get(name) is not None for name in ("RRULE", "RDATE"))
    if not recurs:
        if begins >= _as_instant(start):
            return True
        if not overlap:
            return False
        # It began before the window; under the overlap rule it is still part of
        # the answer unless it provably finished first. A component this broken
        # may have no readable end at all, and unknown means counted.
        finishes = _raw_instant(component, "DTEND")
        return finishes is None or finishes > _as_instant(start)

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


def _duration_property(component: icalendar.Event) -> timedelta | None:
    """The ``DURATION`` of one component, when it carries a readable one."""
    field = component.get("DURATION")
    if field is None:
        return None
    value = getattr(field, "dt", field)
    return value if isinstance(value, timedelta) else None


def _end_instant(
    component: icalendar.Event, start: date | datetime
) -> date | datetime:
    """When this component ends: ``DTEND``, else ``DTSTART`` plus ``DURATION``.

    ``DURATION`` is the other legal spelling of how long a meeting lasts, and it
    is the one Yandex writes for some events.  Reading only ``DTEND`` reports a
    45-minute meeting as instantaneous -- and worse, the listing tool resolves
    ``DURATION`` through the expansion library, so the two tools would report
    different ends for the same meeting.  With neither property, zero length is
    the only honest reading and is never guessed wider.
    """
    end = _instant(component, "DTEND")
    if end is not None:
        return end
    duration = _duration_property(component)
    if duration is not None:
        return start + duration
    return start


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


# -- one event, in full ---------------------------------------------------
#
# Listing answers "what is on my calendar"; this half answers "what is this
# meeting". It shares the expansion above rather than reimplementing it, because
# selecting one instance and listing them all must agree about what an override
# means, when an instance is cancelled, and where a series' boundaries are.

#: A one-off event: it has no instances, so `recurrence_id` never applies.
SCOPE_SINGLE = "single"

#: The series itself, not one of its instances.
SCOPE_SERIES = "series"

#: One instance of a series, selected by its recurrence id.
SCOPE_OCCURRENCE = "occurrence"


class EventNotInDocument(Exception):
    """The fetched document does not hold the requested ``UID``.

    Addressing an object by URL can land on a document that exists but is not
    the event asked for.  That is a miss for this UID, not a failure, so the
    caller may keep looking in the next calendar.
    """


class InstanceNotInSeries(Exception):
    """The ``UID`` was found, but the series has no such instance.

    Deliberately distinct from :class:`EventNotInDocument`: "the meeting is not
    on this account" and "that day is not part of this series" need different
    corrections from the caller, and one dressed as the other sends them looking
    in the wrong place.
    """


@dataclass(frozen=True, slots=True)
class Participant:
    """One ``ORGANIZER`` or ``ATTENDEE`` line, read rather than interpreted.

    ``name`` is ``None`` when the line carried no ``CN``: an address with no
    name is routine, and inventing one from the local part would put a name in
    front of a human that nobody chose.
    """

    email: str | None
    name: str | None
    response_status: str | None
    role: str | None


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One event -- a series, or one instance of one -- with its full detail."""

    uid: str
    recurrence_id: str | None
    is_series: bool
    scope: str
    cancelled: bool
    status: str | None
    summary: str | None
    description: str | None
    location: str | None
    organizer: Participant | None
    organizers: tuple[Participant, ...]
    attendees: tuple[Participant, ...]
    join_url: str | None
    recurrence_summary: str | None
    start: date | datetime
    end: date | datetime
    all_day: bool
    calendar_url: str
    calendar_name: str


def read_event(
    sources: CalendarSource | Sequence[CalendarSource],
    *,
    uid: str,
    recurrence_id: date | datetime | None = None,
) -> EventRecord:
    """Read one event out of the fetched documents that hold it, in full.

    Several documents are accepted, and their components are gathered by ``UID``
    across all of them before anything is read -- exactly as the listing path
    groups them.  A ``RECURRENCE-ID`` override is frequently a CalDAV object of
    its own: reading one document at a time returns a moved instance at the
    series' unmodified time, and turns a document that holds only an override
    into a not-found for an instance that plainly exists.

    Args:
        sources: the documents the server returned for this UID, in the order
            they were fetched.  The first one that holds the ``UID`` names the
            calendar the answer is attributed to.
        uid: the event asked for.  A document holding some *other* event is a
            miss, not a match: addressing by URL can land anywhere.
        recurrence_id: which instance of a series, or ``None`` for the series
            itself.

    Raises:
        EventNotInDocument: no document holds a component with this ``UID``.
        InstanceNotInSeries: the ``UID`` is here but has no such instance --
            including the case where the event is not a series at all.
        ProtocolError: a document could not be parsed, the event carries a
            timestamp with no offset, only overrides were found, or the instance
            is superseded by a ``RANGE=THISANDFUTURE`` override this server does
            not resolve.  None of these is a missing event, and reporting any of
            them as one would be a claim about the account rather than the data.
    """
    if isinstance(sources, CalendarSource):
        sources = [sources]

    parsed: list[tuple[icalendar.Calendar, CalendarSource]] = []
    unparseable = 0
    for source in sources:
        try:
            parsed.append((icalendar.Calendar.from_ical(source.ics), source))
        except Exception:  # noqa: BLE001 - counted below, never silently dropped
            unparseable += 1

    holders = [
        (document, source, found)
        for document, source in parsed
        if (found := _components_with_uid(document, uid))
    ]
    if not holders:
        if unparseable:
            # The event may well be in the document that would not parse, so
            # this is a fault in the data, not an absence on the account.
            raise ProtocolError(
                f"The calendar object for event {uid!r} could not be read: the "
                "server returned something this parser does not recognise as "
                "iCalendar. The event may well exist."
            )
        raise EventNotInDocument(uid)

    source = holders[0][1]
    documents = tuple(document for document, _, _ in holders)
    components = [component for _, _, found in holders for component in found]

    master = _master_of(components)
    overrides = [c for c in components if c.get("RECURRENCE-ID") is not None]
    is_series = _is_series(components)

    try:
        if recurrence_id is None:
            if master is None:
                # Every component here overrides one instance of a series whose
                # own definition was not among the objects read. One instance's
                # start, end, summary and location are not the series'.
                raise ProtocolError(
                    f"Event {uid!r} was found only as RECURRENCE-ID overrides: "
                    "the component that defines the series itself is not in the "
                    "objects read, so there is nothing to report as the series. "
                    "One instance's times are not the series' times. Ask for a "
                    "particular instance with `recurrence_id` instead."
                )
            return _record(
                master,
                uid=uid,
                source=source,
                recurrence_id=None,
                is_series=is_series,
                scope=SCOPE_SERIES if is_series else SCOPE_SINGLE,
                start=_required_start(master, uid),
                recurrence_summary=(
                    _recurrence_summary(master) if is_series else None
                ),
            )

        if not is_series:
            # Answering with the event itself would hide the caller's mistake
            # behind a plausible result.
            raise InstanceNotInSeries(uid)

        return _select_instance(
            uid=uid,
            source=source,
            documents=documents,
            master=master,
            overrides=overrides,
            components=components,
            recurrence_id=recurrence_id,
        )
    except _NaiveTimestamp as exc:
        raise ProtocolError(
            f"Event {uid!r} carries a timestamp with no timezone, so it cannot "
            "be reported with an explicit offset. Guessing one would move the "
            "event for every reader at a different offset."
        ) from exc


def _components_with_uid(
    document: icalendar.Calendar, uid: str
) -> list[icalendar.Event]:
    return [
        component
        for component in document.walk("VEVENT")
        if str(component.get("UID") or "").strip() == uid
    ]


def _select_instance(
    *,
    uid: str,
    source: CalendarSource,
    documents: Sequence[icalendar.Calendar],
    master: "icalendar.Event | None",
    overrides: Sequence[icalendar.Event],
    components: Sequence[icalendar.Event],
    recurrence_id: date | datetime,
) -> EventRecord:
    """One instance of a series: an override, a cancellation, or an expansion."""
    target = _as_instant(recurrence_id)
    summary = _recurrence_summary(master) if master is not None else None

    # An override wins outright: it *is* that instance, in its modified form.
    for component in overrides:
        moment = _instant(component, "RECURRENCE-ID")
        if moment is not None and _as_instant(moment) == target:
            return _record(
                component,
                uid=uid,
                source=source,
                recurrence_id=format_instant(moment),
                is_series=True,
                scope=SCOPE_OCCURRENCE,
                start=_required_start(component, uid),
                cancelled=_is_cancelled(component),
                recurrence_summary=summary,
            )

    superseding = _superseded_from(overrides, target)
    if superseding is not None:
        # RANGE=THISANDFUTURE rewrites this instance and every later one. This
        # server does not resolve it, and the unmodified time it would otherwise
        # return is known to be wrong -- so the instance is reported as
        # unresolved rather than answered with a value that sends people late.
        raise ProtocolError(
            f"Event {uid!r} has a RECURRENCE-ID;RANGE=THISANDFUTURE override at "
            f"{superseding}, which supersedes this instance and every later one. "
            "This server does not resolve THISANDFUTURE overrides, so the "
            "instance could not be resolved; the unmodified time is known to be "
            "wrong and is not returned. Use `calendar_events_list` over the day "
            "in question, which expands the series through the same library the "
            "server itself uses."
        )

    if master is None:
        raise InstanceNotInSeries(uid)

    # An EXDATE instance is not in the expansion at all -- the library removes
    # it. Reported as cancelled rather than as missing, because "this meeting is
    # off" and "there was never a meeting then" are different answers.
    for excluded in _exdates(master):
        if _as_instant(excluded) == target:
            duration = _duration_of(master)
            return _record(
                master,
                uid=uid,
                source=source,
                recurrence_id=format_instant(excluded),
                is_series=True,
                scope=SCOPE_OCCURRENCE,
                start=excluded,
                end=excluded + duration if duration is not None else excluded,
                cancelled=True,
                status="CANCELLED",
                recurrence_summary=summary,
            )

    # The whole documents are carried across, not just the components: a `TZID`
    # the events refer to resolves through the `VTIMEZONE` beside them, and
    # dropping it would make the expansion fail on a series it can read.
    group = _Group(
        calendar_url=source.calendar_url,
        uid=uid,
        components=tuple(components),
        documents=tuple(documents),
        source=source,
    )
    # A day either side: enough for the instance itself and for an all-day value
    # that sorts at midnight UTC, and narrow enough that a long series is not
    # expanded wholesale to answer a question about one day.
    window_start = target - timedelta(days=1)
    window_end = target + timedelta(days=2)
    for expanded in recurring_ical_events.of(
        _calendar_with(group), components=["VEVENT"]
    ).between(window_start, window_end):
        moment = _instant(expanded, "RECURRENCE-ID") or _instant(expanded, "DTSTART")
        if moment is None or _as_instant(moment) != target:
            continue
        return _record(
            expanded,
            uid=uid,
            source=source,
            recurrence_id=format_instant(moment),
            is_series=True,
            scope=SCOPE_OCCURRENCE,
            start=_required_start(expanded, uid),
            cancelled=_is_cancelled(expanded),
            recurrence_summary=summary,
        )

    raise InstanceNotInSeries(uid)


def _superseded_from(
    overrides: Sequence[icalendar.Event], target: datetime
) -> str | None:
    """The ``RANGE=THISANDFUTURE`` override, if any, that rewrites ``target``.

    Such an override replaces its own instance *and every later one*.  Ignoring
    the parameter returns later instances at times the organiser has already
    changed, which is the one kind of wrong answer that reads as right.
    """
    for component in overrides:
        field = component.get("RECURRENCE-ID")
        if field is None:
            continue
        if str(_parameter(field, "RANGE") or "").upper() != "THISANDFUTURE":
            continue
        try:
            moment = _instant(component, "RECURRENCE-ID")
        except _NaiveTimestamp:
            # An unreadable boundary cannot rule the supersession out.
            return "an unreadable time"
        if moment is not None and _as_instant(moment) <= target:
            return format_instant(moment)
    return None


def _master_of(components: Sequence[icalendar.Event]) -> "icalendar.Event | None":
    """The component that defines the series, as opposed to overriding one instance."""
    for component in components:
        if component.get("RECURRENCE-ID") is None:
            return component
    return None


def _required_start(component: icalendar.Event, uid: str) -> date | datetime:
    start = _instant(component, "DTSTART")
    if start is None:
        raise ProtocolError(
            f"Event {uid!r} has no start time, so there is nothing to report as "
            "one. The object exists but is not a usable event."
        )
    return start


def _duration_of(component: icalendar.Event) -> timedelta | None:
    """How long one instance of this series lasts, when that can be read."""
    try:
        start = _instant(component, "DTSTART")
        if start is None:
            return None
        end = _end_instant(component, start)
    except _NaiveTimestamp:
        return None
    if isinstance(start, datetime) != isinstance(end, datetime):
        return None
    return end - start


def _exdates(component: icalendar.Event) -> list[date | datetime]:
    """Every excluded instance of a series, however the property was spelled.

    ``EXDATE`` may appear once with several values or several times with one
    each, and ``icalendar`` represents the two differently.

    The values go through the same rule as every other instant here: a floating
    one is refused rather than decorated.  Read raw, a floating ``EXDATE`` came
    back as a naive ``start`` and a naive ``recurrence_id`` -- a value this
    tool's own validator rejects if the caller passes it back.
    """
    field = component.get("EXDATE")
    if field is None:
        return []
    fields = field if isinstance(field, list) else [field]
    values: list[date | datetime] = []
    for entry in fields:
        dates = getattr(entry, "dts", None)
        items = [entry] if dates is None else list(dates)
        for item in items:
            value = getattr(item, "dt", None if dates is None else item)
            if isinstance(value, datetime):
                if value.tzinfo is None or value.utcoffset() is None:
                    raise _NaiveTimestamp(
                        "EXDATE has no timezone; a floating exclusion cannot be "
                        "reported with an explicit offset."
                    )
                values.append(value)
            elif isinstance(value, date):
                values.append(value)
    return values


#: The first absolute link in a text field, when a property did not carry one.
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")


def _join_url(component: icalendar.Event) -> str | None:
    """Where to join this meeting, when the invitation says so.

    Read from the properties that exist to carry it -- ``CONFERENCE`` and the
    ``X-`` spellings clients write -- and only then from ``LOCATION`` and
    ``DESCRIPTION``, which is where Yandex and most clients actually put a
    Telemost or meeting link.  ``None`` when nothing on the event carried one:
    a link is never assembled from anything but text the invitation contained.
    """
    for name in ("CONFERENCE", "X-GOOGLE-CONFERENCE", "X-TELEMOST-URL"):
        field = component.get(name)
        if field is None:
            continue
        entry = field[0] if isinstance(field, list) and field else field
        text = str(entry).strip()
        if text.lower().startswith(("http://", "https://")):
            return text
    for name in ("LOCATION", "DESCRIPTION"):
        text = _text(component, name)
        if not text:
            continue
        found = _URL_IN_TEXT.search(text)
        if found:
            return found.group(0).rstrip(".,;)")
    return None


#: How each ``FREQ`` reads in a sentence, singular and plural.
_FREQUENCIES = {
    "SECONDLY": ("every second", "seconds"),
    "MINUTELY": ("every minute", "minutes"),
    "HOURLY": ("hourly", "hours"),
    "DAILY": ("daily", "days"),
    "WEEKLY": ("weekly", "weeks"),
    "MONTHLY": ("monthly", "months"),
    "YEARLY": ("yearly", "years"),
}


def _recurrence_summary(component: icalendar.Event) -> str | None:
    """How this series recurs, in a sentence, or ``None`` when it does not.

    A caller reading one event wants to know how it repeats; ``is_series: true``
    alone tells them it does without telling them anything they can plan around.
    The rule itself is deliberately not returned -- above this module nothing
    knows what an ``RRULE`` is.
    """
    rule = component.get("RRULE")
    if rule is None:
        if component.get("RDATE") is not None:
            return "Repeats on individual dates listed on the event."
        return None
    if not hasattr(rule, "get"):
        return None

    frequency = str(_first_value(rule.get("FREQ")) or "").upper()
    words = _FREQUENCIES.get(frequency)
    if words is None:
        return None
    every, plural = words

    try:
        interval = int(_first_value(rule.get("INTERVAL")) or 1)
    except (TypeError, ValueError):
        interval = 1
    parts = [f"Repeats every {interval} {plural}" if interval > 1 else f"Repeats {every}"]

    byday = rule.get("BYDAY")
    if byday:
        days = byday if isinstance(byday, (list, tuple)) else [byday]
        parts.append("on " + ", ".join(str(day) for day in days))

    count = _first_value(rule.get("COUNT"))
    until = _first_value(rule.get("UNTIL"))
    if count:
        parts.append(f"{int(count)} times in all")
    elif until is not None:
        moment = getattr(until, "dt", until)
        if isinstance(moment, (date, datetime)):
            parts.append(f"until {format_instant(moment)}")
    return ", ".join(parts) + "."


def _first_value(value: object) -> object:
    """One value from a property that ``icalendar`` may hand over as a list."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _participants(component: icalendar.Event, name: str) -> list[Participant]:
    """``ATTENDEE`` or ``ORGANIZER`` lines, read as addresses and parameters."""
    field = component.get(name)
    if field is None:
        return []
    fields = field if isinstance(field, list) else [field]
    people: list[Participant] = []
    for entry in fields:
        people.append(
            Participant(
                email=_address_of(entry),
                name=_parameter(entry, "CN"),
                response_status=_parameter(entry, "PARTSTAT"),
                role=_parameter(entry, "ROLE"),
            )
        )
    return people


def _address_of(entry: object) -> str | None:
    """The address behind a ``mailto:``, or whatever else the line carried."""
    text = str(entry).strip()
    if not text:
        return None
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:") :].strip()
    return text or None


def _parameter(entry: object, name: str) -> str | None:
    """One parameter of a property line, or ``None`` when it was not given."""
    params = getattr(entry, "params", None)
    if params is None:
        return None
    try:
        value = params.get(name)
    except Exception:  # noqa: BLE001 - an unreadable parameter is an absent one
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text(component: icalendar.Event, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _record(
    component: icalendar.Event,
    *,
    uid: str,
    source: CalendarSource,
    recurrence_id: str | None,
    is_series: bool,
    scope: str,
    start: date | datetime,
    end: date | datetime | None = None,
    cancelled: bool | None = None,
    status: str | None = None,
    recurrence_summary: str | None = None,
) -> EventRecord:
    """Build the record for one component, given the instant it stands for."""
    if end is None:
        end = _end_instant(component, start)
    organizers = _participants(component, "ORGANIZER")
    read_status = status if status is not None else _text(component, "STATUS")
    return EventRecord(
        uid=uid,
        recurrence_id=recurrence_id,
        is_series=is_series,
        scope=scope,
        cancelled=_is_cancelled(component) if cancelled is None else cancelled,
        status=read_status.upper() if read_status else None,
        summary=_text(component, "SUMMARY"),
        description=_text(component, "DESCRIPTION"),
        location=_text(component, "LOCATION"),
        organizer=organizers[0] if organizers else None,
        organizers=tuple(organizers),
        attendees=tuple(_participants(component, "ATTENDEE")),
        join_url=_join_url(component),
        recurrence_summary=recurrence_summary,
        start=start,
        end=end,
        all_day=_is_all_day(start),
        calendar_url=source.calendar_url,
        calendar_name=source.calendar_name,
    )
