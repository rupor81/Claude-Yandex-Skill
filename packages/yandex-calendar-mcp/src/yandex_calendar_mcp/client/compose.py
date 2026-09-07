"""Composing and editing the iCalendar documents this server writes.

Reading a calendar and writing to one are different jobs, and this module owns
the second: given the few values a caller may set, it produces the exact bytes
that are PUT.  Like the rest of ``client/`` it imports no ``mcp`` and knows
nothing about tool contracts.

It does two things, and the difference between them is the point.  A *creation*
composes a whole new document from nothing.  A *change* reads the document the
server already holds, edits the one component that was asked about, and writes
the whole thing back.  Composing a replacement instead would be shorter and
would silently destroy data: measured on this account, a series and the
``RECURRENCE-ID`` overrides of its instances live in a single object, so a
replacement takes every moved instance with it while answering as a success.

Three choices are worth reading twice:

* **A timed event is written in UTC, as ``...Z``.**  iCalendar has no way to
  spell an offset: a local time needs a ``TZID`` naming a ``VTIMEZONE`` that
  would have to be shipped with the event, and a floating local time means a
  different moment to every reader.  Converting to UTC keeps the instant exactly
  and needs nothing else to interpret it.  The readback reports what the server
  holds, so a caller sees the instant, not this spelling.
* **An all-day event stays dates.**  ``VALUE=DATE`` on both bounds, ``DTEND``
  exclusive as iCalendar requires.  Coercing a date to midnight would move the
  day for everybody not on UTC.
* **The invariants are checked here too**, not only in ``tools/``.  This layer is
  usable from a plain script, and a naive or inverted event that reached the
  server would be a permanent fact on somebody's calendar.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import icalendar
from yandex_core.errors import ProtocolError

__all__ = [
    "EventDraft",
    "EventEdit",
    "EditedDocument",
    "CancelledInstance",
    "PRODID",
    "SCOPE_OCCURRENCE",
    "SCOPE_SERIES",
    "UNCHANGED",
    "apply_event_edit",
    "apply_instance_cancellation",
    "check_event_edit",
    "exdates",
    "FloatingExclusion",
    "EDITABLE_FIELDS",
    "new_uid",
    "build_event_document",
    "written_boundary",
]

#: The two things "change this event" can mean.  Spelled here as well as in
#: ``recurrence.py`` so this module needs nothing from the reader to be usable.
SCOPE_SERIES = "series"
SCOPE_OCCURRENCE = "occurrence"

#: What this server stamps on the documents it writes, so an event it created is
#: identifiable later without guessing from its shape.
PRODID = "-//yandex-mcp//calendar//EN"


def new_uid() -> str:
    """A UID for an event this server is about to create.

    Random rather than derived from the event's own values: two meetings with
    the same title at the same time are a normal thing to want, and a derived
    UID would make the second one collide with -- and, without the write's
    guard, replace -- the first.
    """
    return f"{uuid.uuid4()}"


@dataclass(frozen=True, slots=True)
class EventDraft:
    """One event as a caller asked for it, before anything has been written."""

    uid: str
    summary: str
    start: date | datetime
    end: date | datetime
    description: str | None = None
    location: str | None = None


def build_event_document(draft: EventDraft, *, now: datetime | None = None) -> str:
    """The iCalendar text for one new, non-recurring event.

    Raises:
        ProtocolError: if the draft is untitled, carries a naive timestamp,
            mixes a date with a timestamp, or does not end after it starts.
            Each is refused rather than repaired: there is no correction that
            is not a guess about what the caller meant.
    """
    _check(draft)

    event = icalendar.Event()
    event.add("UID", draft.uid)
    event.add("DTSTAMP", (now or datetime.now(timezone.utc)).replace(microsecond=0))
    event.add("SUMMARY", draft.summary)
    event.add("DTSTART", written_boundary(draft.start))
    event.add("DTEND", written_boundary(draft.end))
    if draft.description is not None:
        event.add("DESCRIPTION", draft.description)
    if draft.location is not None:
        event.add("LOCATION", draft.location)
    # Stated rather than left to the default, so the event this server writes
    # answers the busy-time question the same way whoever reads it.
    event.add("TRANSP", "OPAQUE")
    event.add("SEQUENCE", 0)

    document = icalendar.Calendar()
    document.add("PRODID", PRODID)
    document.add("VERSION", "2.0")
    document.add("CALSCALE", "GREGORIAN")
    document.add_component(event)
    return document.to_ical().decode("utf-8")


def written_boundary(value: date | datetime) -> date | datetime:
    """The value as it is written: a date stays a date, a moment becomes UTC.

    Public because what was *sent* is the only honest thing to compare the
    server's stored values against.  Comparing against the caller's own value
    would report the microseconds this function drops as an edit the server
    made.
    """
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0)
    return value


def _check(draft: EventDraft) -> None:
    _check_summary(draft.summary)
    _check_boundaries(draft.start, draft.end)


def _check_summary(summary: str | None) -> None:
    if not summary or not summary.strip():
        raise ProtocolError(
            "An event needs a summary: an untitled event cannot be found again."
        )


def _check_boundaries(start: date | datetime, end: date | datetime) -> None:
    """Both ends of the same kind, both unambiguous, and in order.

    Shared by a creation and a change: a document that reached the server with
    a naive or inverted boundary is a permanent fact on somebody's calendar
    either way, and this layer is usable from a plain script.
    """
    for name, value in (("start", start), ("end", end)):
        if not isinstance(value, date):
            raise ProtocolError(
                f"`{name}` is {type(value).__name__}, not a date or a timestamp. "
                "A boundary is given as a `datetime.date` for an all-day event "
                "or a `datetime.datetime` with an explicit offset for a timed "
                "one; a string is parsed by the caller, not here. Nothing was "
                "written."
            )
    start_is_day = not isinstance(start, datetime)
    end_is_day = not isinstance(end, datetime)
    if start_is_day != end_is_day:
        raise ProtocolError(
            "`start` and `end` must both be dates for an all-day event, or both "
            "be timestamps. One of each has no reading that is not a guess."
        )
    for name, value in (("start", start), ("end", end)):
        if isinstance(value, datetime) and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ProtocolError(
                f"`{name}` has no UTC offset, so it names a different moment to "
                "every reader. Give an explicit one, for example "
                "2026-06-08T09:00:00+03:00."
            )
    if _as_instant(end) <= _as_instant(start):
        raise ProtocolError(
            "`end` must be after `start`; an event that ends when it begins "
            "occupies no time. For an all-day event `end` is exclusive, so a "
            "single day ends on the following date."
        )


def _as_instant(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


# -- editing a document the server already holds --------------------------


class _Unchanged:
    """The absence of an instruction, told apart from a value of ``None``.

    ``None`` is a real answer to "what is the location" -- it has none -- so it
    cannot also mean "leave the location alone".  A field this sentinel is left
    at is never touched, which is what makes "nothing outside what was asked to
    change is altered" enforceable rather than aspirational.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "UNCHANGED"

    def __bool__(self) -> bool:
        return False


UNCHANGED: Any = _Unchanged()

#: The fields a change may name, in the order an answer lists them.
EDITABLE_FIELDS = ("summary", "start", "end", "description", "location")


@dataclass(frozen=True, slots=True)
class EventEdit:
    """The values a caller asked to change, and nothing else.

    Every field defaults to :data:`UNCHANGED`.  An edit that names nothing is
    empty, and is refused rather than turned into a write that would bump the
    sequence number of an event nobody asked to alter.

    A text field named as ``None`` means "it has none", and the property is
    *removed* -- not written as the literal string ``None``, which is what a
    plain assignment would put in the location of somebody's meeting.  A
    boundary must be a ``date`` or an offset-carrying ``datetime``; anything
    else, a string included, is refused as a :class:`ProtocolError` rather than
    left to fail somewhere further down.
    """

    summary: Any = UNCHANGED
    start: Any = UNCHANGED
    end: Any = UNCHANGED
    description: Any = UNCHANGED
    location: Any = UNCHANGED

    @property
    def named(self) -> dict[str, Any]:
        """Only the fields the caller actually named, in a stable order."""
        return {
            field: getattr(self, field)
            for field in EDITABLE_FIELDS
            if getattr(self, field) is not UNCHANGED
        }

    @property
    def is_empty(self) -> bool:
        return not self.named


@dataclass(frozen=True, slots=True)
class EditedDocument:
    """The result of editing one stored document.

    ``changed`` is false when every value the caller named is already what the
    document holds.  That is not an error and not a failure -- it is a no-op,
    and the write is simply not sent: a PUT that stores the same values still
    bumps the object's version and would refuse the next caller's precondition
    for no reason at all.

    ``sent`` carries the values as they were *written*, for the same reason
    :func:`written_boundary` is public: comparing the server's stored values
    against the caller's originals would report a microsecond this module
    dropped as an edit the server made.
    """

    document: str
    changed: bool
    sent: dict[str, Any]


def apply_event_edit(
    ics: str,
    *,
    uid: str,
    scope: str,
    recurrence_id: date | datetime | None,
    edit: EventEdit,
    now: datetime | None = None,
) -> EditedDocument:
    """Edit one stored document and hand back the whole of it, changed.

    The document is parsed, the one component the scope names is modified, and
    everything else -- other components, other properties, the ``VTIMEZONE``
    the event's own times refer to -- is carried through untouched.

    With ``scope`` of ``series`` the component edited is the one that defines
    the event: the master, or a one-off event, which is the same component. The
    ``RECURRENCE-ID`` overrides beside it are left exactly as they are, so an
    instance somebody already moved keeps its own values.

    With ``scope`` of ``occurrence`` the component edited is the override for
    ``recurrence_id``.  When there is not one yet, it is derived from the
    master -- the instance's own start and end, the series' other values, and
    none of the series' recurrence properties -- and added beside it, which is
    how one instance of a series is changed without touching the rest.

    Raises:
        ProtocolError: the document cannot be parsed, does not hold the UID,
            holds no component to edit, holds two components defining the same
            event, or the edit itself is not one that can be written.  Nothing
            is returned half-applied.
    """
    check_event_edit(edit)

    try:
        document = icalendar.Calendar.from_ical(ics)
    except Exception as exc:  # noqa: BLE001 - never a missing event
        raise ProtocolError(
            f"The stored calendar object for event {uid!r} could not be read, so "
            "it cannot be changed: the server returned something this parser "
            "does not recognise as iCalendar. Nothing was written."
        ) from exc

    holder, components = _holder_of(document, uid)
    master = _master_component(components, uid=uid)
    overrides = [c for c in components if c.get("RECURRENCE-ID") is not None]

    if scope == SCOPE_SERIES:
        if master is None:
            raise ProtocolError(
                f"Event {uid!r} is present only as RECURRENCE-ID overrides in the "
                "stored object: the component that defines the series is not "
                "there, so there is nothing to change with `scope: series`. "
                "Change a particular instance with `scope: occurrence` instead."
            )
        target, added = master, None
    else:
        target, added = _override_for(
            master, overrides, uid=uid, recurrence_id=recurrence_id
        )

    changed, sent = _apply(target, edit)
    if not changed:
        # Nothing to write.  For a derived override that also means it is never
        # added: an override saying only what the series already says is a
        # component that would quietly hide every later change to the series
        # from this one instance.
        return EditedDocument(document=ics, changed=False, sent=sent)

    if added is not None:
        holder.add_component(added)
    _stamp(target, now=now)
    return EditedDocument(
        document=document.to_ical().decode("utf-8"), changed=True, sent=sent
    )


def check_event_edit(edit: EventEdit) -> None:
    """The invariants of a change, checked before a request is even made.

    Public because the client layer calls it before it opens a connection: a
    change that cannot be written should be refused without anything being
    asked of the server, and the rule must not live only in ``tools/``.
    """
    if edit.is_empty:
        raise ProtocolError(
            "Nothing was named to change. Give at least one of "
            + ", ".join(f"`{field}`" for field in EDITABLE_FIELDS)
            + "; an update that names nothing would rewrite the event to say "
            "what it already says."
        )
    if edit.summary is not UNCHANGED:
        _check_summary(edit.summary)
    moving_start = edit.start is not UNCHANGED
    moving_end = edit.end is not UNCHANGED
    if moving_start != moving_end:
        raise ProtocolError(
            "`start` and `end` must be given together. Moving one end alone "
            "would either stretch the event or invert it, and which of those "
            "was meant is a guess this server will not make: pass both, even "
            "when one of them is unchanged."
        )
    if moving_start:
        _check_boundaries(edit.start, edit.end)


@dataclass(frozen=True, slots=True)
class CancelledInstance:
    """The result of cancelling one instance of a stored series.

    ``changed`` is false when the instance is already excluded.  That is not an
    error: the meeting is off, which is what the caller wanted, and a write that
    stored the same exclusion again would bump the object's version and refuse
    the next caller's precondition for nothing.

    ``override_removed`` says whether the instance also had a
    ``RECURRENCE-ID`` override that went with it.  An exclusion and an override
    for the same moment are a contradiction -- this instance does not happen,
    and here is what happens at it -- and readers disagree about which wins, so
    the two are never left side by side.

    ``exclusion_added`` is false when the exclusion was already there and this
    write existed only to remove the override contradicting it.  The two
    together are what tells a caller which of the acts happened: a cancellation,
    or the repair of a document that said both things at once.
    """

    document: str
    changed: bool
    override_removed: bool
    exclusion_added: bool = True


def apply_instance_cancellation(
    ics: str,
    *,
    uid: str,
    recurrence_id: date | datetime,
    now: datetime | None = None,
) -> CancelledInstance:
    """Exclude one instance from a stored series, and hand back the whole object.

    Cancelling one instance is an *edit*: an ``EXDATE`` is added to the
    component that defines the series, and everything else in the object --
    other components, the overrides of other instances, the ``VTIMEZONE`` the
    event's own times refer to -- is carried through untouched.  Nothing is
    removed from the calendar; the object is written back by a conditional
    write, exactly as a change is.

    The one thing that *is* removed is an override belonging to the instance
    being cancelled, in the same write.  That also repairs a document that
    already held both: an exclusion beside an override for the same moment is
    the contradiction this function exists to avoid leaving behind, and finding
    one already there is a reason to write, not a reason to call the instance
    already gone and leave it showing in half the readers.

    ``changed`` is false only when there is genuinely nothing to do: the
    instance is excluded and no override contradicts the exclusion.

    Raises:
        ProtocolError: the document cannot be parsed, does not hold the UID, or
            holds no component defining the series.  Nothing is returned
            half-applied.
    """
    try:
        document = icalendar.Calendar.from_ical(ics)
    except Exception as exc:  # noqa: BLE001 - never a missing event
        raise ProtocolError(
            f"The stored calendar object for event {uid!r} could not be read, so "
            "the instance cannot be cancelled: the server returned something "
            "this parser does not recognise as iCalendar. Nothing was written."
        ) from exc

    holder, components = _holder_of(document, uid)
    master = _master_component(components, uid=uid)
    if master is None:
        raise ProtocolError(
            f"Event {uid!r} is present only as RECURRENCE-ID overrides in the "
            "stored object: the component that defines the series is not there, "
            "so an exclusion has nothing to attach to and nothing was written. "
            "Read the event with `calendar_event_get` and cancel it in a client "
            "that can address each component."
        )

    target = _as_instant(recurrence_id)
    already_excluded = any(
        _as_instant(excluded) == target for excluded in exdates(master)
    )

    override_removed = False
    # Identity, not equality: an ``icalendar`` component is a dict, so two
    # components that merely look alike compare equal, and removing "the first
    # one that matches" could take a component belonging to another event that
    # happens to hold the same properties.
    doomed = [
        index
        for index, component in enumerate(holder.subcomponents)
        if any(component is held for held in components)
        and (moment := _component_value(component, "RECURRENCE-ID")) is not None
        and _as_instant(moment) == target
    ]
    for index in reversed(doomed):
        del holder.subcomponents[index]
        override_removed = True

    if already_excluded and not override_removed:
        # Already off, and nothing beside the exclusion disagrees. Said so
        # rather than written again.
        return CancelledInstance(
            document=ics,
            changed=False,
            override_removed=False,
            exclusion_added=False,
        )

    if not already_excluded:
        # Added rather than replaced: a series may exclude many instances, and
        # replacing the property would put every other cancelled meeting back.
        master.add("EXDATE", written_boundary(recurrence_id))
    _stamp(master, now=now)
    return CancelledInstance(
        document=document.to_ical().decode("utf-8"),
        changed=True,
        override_removed=override_removed,
        exclusion_added=not already_excluded,
    )


class FloatingExclusion(ProtocolError):
    """An ``EXDATE`` with no timezone, which names no particular instant.

    Its own class because the reader and the writer must do different things
    with it and both must do *something*: the reader turns it into the message
    that says the event cannot be reported with an explicit offset, and the
    writer refuses to write beside it.  Skipping it -- which the writer used to
    do -- means concluding "not excluded" about an instance the reader will not
    report at all, and writing a second exclusion for a meeting already off.
    """


def exdates(component: icalendar.Event) -> list[date | datetime]:
    """Every instance a series excludes, however ``EXDATE`` was spelled.

    ``EXDATE`` may appear once carrying several values or several times
    carrying one each, and ``icalendar`` represents those two differently.

    The one implementation shared by the writer here and the reader in
    ``recurrence.py``.  They had one each, and they disagreed: on a floating
    value the reader raised while the writer skipped it, so the writer could
    conclude "not excluded" about an instance the reader refuses to report and
    add a duplicate exclusion beside it.  Which of the two is right is not a
    question a caller can be expected to arbitrate, so there is now one answer.

    Raises:
        FloatingExclusion: a value carries no offset. Nothing is guessed: a
            floating exclusion names a different instant to every reader.
    """
    field = component.get("EXDATE")
    if field is None:
        return []
    values: list[date | datetime] = []
    for entry in field if isinstance(field, list) else [field]:
        dates = getattr(entry, "dts", None)
        items = [entry] if dates is None else list(dates)
        for item in items:
            value = getattr(item, "dt", None if dates is None else item)
            if isinstance(value, datetime):
                if value.tzinfo is None or value.utcoffset() is None:
                    raise FloatingExclusion(
                        "EXDATE has no timezone; a floating exclusion names a "
                        "different instant to every reader, so this server "
                        "will not decide whether it excludes the instance in "
                        "hand. Nothing was written."
                    )
                values.append(value)
            elif isinstance(value, date):
                values.append(value)
    return values


def _holder_of(
    document: icalendar.Calendar, uid: str
) -> tuple[icalendar.Calendar, list[icalendar.Event]]:
    """The component that holds this UID's events, and those events.

    The holder is returned as well as the events because an occurrence-scoped
    change may have to *add* a component beside them, and it must be added to
    the same container the others live in.
    """
    components = [
        component
        for component in document.walk("VEVENT")
        if str(component.get("UID") or "") == uid
    ]
    if not components:
        raise ProtocolError(
            f"The stored object does not hold event {uid!r}, so there is nothing "
            "to change in it. Nothing was written."
        )
    return document, components


def _master_component(
    components: list[icalendar.Event], *, uid: str
) -> "icalendar.Event | None":
    """The one component that defines the event, or nothing when there is none.

    Two components sharing the UID with no ``RECURRENCE-ID`` between them are
    two definitions of one event.  Editing the first and answering that the
    event was changed would be a claim about a document half of which was left
    as it was, and the caller has no way to see that from the answer.
    """
    masters = [
        component
        for component in components
        if component.get("RECURRENCE-ID") is None
    ]
    if len(masters) > 1:
        raise ProtocolError(
            f"The stored object holds {len(masters)} components for event "
            f"{uid!r} with no RECURRENCE-ID between them, so which of them "
            "defines the event cannot be told. Changing one and reporting the "
            "event as changed would be untrue of the other, so nothing was "
            "written. Read the event with `calendar_event_get` and repair it in "
            "a client that can address each component."
        )
    return masters[0] if masters else None


def _override_for(
    master: "icalendar.Event | None",
    overrides: list[icalendar.Event],
    *,
    uid: str,
    recurrence_id: date | datetime | None,
) -> tuple[icalendar.Event, "icalendar.Event | None"]:
    """The component for one instance, found or derived.

    Returns the component to edit and, when it had to be derived, that same
    component again -- so the caller knows it still has to be added to the
    document, and knows not to add it if the edit turns out to change nothing.
    """
    if recurrence_id is None:
        raise ProtocolError(
            f"Changing one occurrence of {uid!r} needs the `recurrence_id` of the "
            "instance to change."
        )
    target = _as_instant(recurrence_id)
    for component in overrides:
        moment = _component_value(component, "RECURRENCE-ID")
        if moment is not None and _as_instant(moment) == target:
            return component, None

    if master is None:
        raise ProtocolError(
            f"Event {uid!r} has no series definition in the stored object, so "
            "the instance to change cannot be derived from it. Nothing was "
            "written."
        )
    derived = _derived_override(master, recurrence_id=recurrence_id)
    return derived, derived


def _stamp(component: icalendar.Event, *, now: datetime | None) -> None:
    """Say that this component was revised, and when.

    ``SEQUENCE`` is what tells another client that this version supersedes the
    one it holds; without the bump a change is written and every other calendar
    keeps showing the old time.
    """
    moment = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    _replace(component, "DTSTAMP", moment)
    _replace(component, "LAST-MODIFIED", moment)
    try:
        sequence = int(component.get("SEQUENCE", 0) or 0)
    except (TypeError, ValueError):
        sequence = 0
    _replace(component, "SEQUENCE", sequence + 1)


def _apply(
    component: icalendar.Event, edit: EventEdit
) -> tuple[bool, dict[str, Any]]:
    """Write the named values onto one component, and say whether any differed.

    A value already equal to what is stored is not written.  The distinction
    matters: a PUT that stores the same bytes still bumps the object's version
    on the server, and would refuse the next caller's precondition for a change
    that never happened.
    """
    changed = False
    sent: dict[str, Any] = {}

    for field, value in edit.named.items():
        if field in ("start", "end"):
            continue
        written = value
        sent[field] = written
        name = _PROPERTY_OF[field]
        current = _text_property(component, name)
        if current == written:
            continue
        if written is None:
            # `None` is documented as "it has none", so the property goes.
            # Writing it through `add` would store the literal string "None" as
            # the location of somebody's meeting.
            component.pop(name, None)
        else:
            _replace(component, name, written)
        changed = True

    if edit.start is not UNCHANGED:
        start = written_boundary(edit.start)
        end = written_boundary(edit.end)
        sent["start"], sent["end"] = start, end
        if not _same_boundary(_component_value(component, "DTSTART"), start) or not (
            _same_boundary(_component_value(component, "DTEND"), end)
        ):
            _replace(component, "DTSTART", start)
            _replace(component, "DTEND", end)
            # A component that stated its length as a DURATION now states it as
            # an end. Leaving both would let two readers disagree about when
            # the meeting finishes.
            component.pop("DURATION", None)
            changed = True

    return changed, sent


#: The iCalendar property behind each plain-text field a caller may change.
_PROPERTY_OF = {
    "summary": "SUMMARY",
    "description": "DESCRIPTION",
    "location": "LOCATION",
}

#: Properties that define the *series* and must never be copied onto one of its
#: instances: an override carrying an RRULE is a second series.
_SERIES_ONLY = frozenset({"RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"})


def _derived_override(
    master: icalendar.Event, *, recurrence_id: date | datetime
) -> icalendar.Event:
    """A new override for one instance, carrying the series' own values.

    Copied property by property rather than composed afresh, so an instance
    keeps the description, location, attendees and everything else the series
    gave it.  The recurrence properties are the exception: they belong to the
    series, and one instance holding them would be a second series.
    """
    override = icalendar.Event()
    for name, value in master.items():
        if str(name).upper() in _SERIES_ONLY:
            continue
        # Assigned rather than added: the value is already encoded, and with it
        # any TZID parameter that makes it readable.
        override[name] = value
    for subcomponent in master.subcomponents:
        # A VALARM is a subcomponent, not a property: an override copied only
        # property by property has no reminder at all, and moving one meeting
        # would silently remove the notification for it.
        override.add_component(subcomponent)

    duration = _instance_duration(master)
    _replace(override, "RECURRENCE-ID", written_boundary(recurrence_id))
    _replace(override, "DTSTART", written_boundary(recurrence_id))
    if duration is not None:
        override.pop("DURATION", None)
        _replace(override, "DTEND", written_boundary(recurrence_id + duration))
    return override


def _instance_duration(master: icalendar.Event) -> timedelta | None:
    """How long one instance of this series lasts, when that can be read."""
    duration = master.get("DURATION")
    if duration is not None and getattr(duration, "dt", None) is not None:
        return duration.dt
    start = _component_value(master, "DTSTART")
    end = _component_value(master, "DTEND")
    if start is None or end is None:
        return None
    if isinstance(start, datetime) != isinstance(end, datetime):
        return None
    return _as_instant(end) - _as_instant(start)


def _component_value(component: icalendar.Event, name: str) -> date | datetime | None:
    """One date-or-time property as a Python value, or ``None`` when absent."""
    field = component.get(name)
    if field is None:
        return None
    value = getattr(field, "dt", None)
    if value is None:
        return None
    if isinstance(value, datetime) and (
        value.tzinfo is None or value.utcoffset() is None
    ):
        raise ProtocolError(
            f"The stored event's {name} carries no timezone, so this server "
            "cannot tell which instant it names and will not guess one while "
            "changing it. Nothing was written."
        )
    return value


def _text_property(component: icalendar.Event, name: str) -> str | None:
    value = component.get(name)
    return None if value is None else str(value)


def _same_boundary(
    stored: date | datetime | None, written: date | datetime
) -> bool:
    """Whether a boundary already names the same point, spelling aside.

    A date is never the same boundary as a timestamp: an all-day event turned
    into a timed one is a change, not a re-spelling.
    """
    if stored is None:
        return False
    if isinstance(stored, datetime) != isinstance(written, datetime):
        return False
    return _as_instant(stored) == _as_instant(written)


def _replace(component: icalendar.Event, name: str, value: Any) -> None:
    """Set one property to exactly one value, whatever was there before.

    ``add`` appends, so a property that was already present would end up
    twice -- and a component with two ``DTSTART`` lines is one every reader is
    free to disagree about.
    """
    component.pop(name, None)
    component.add(name, value)
