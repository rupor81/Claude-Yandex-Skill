"""Composing the one iCalendar document this server ever writes.

Reading a calendar and writing to one are different jobs, and this module owns
the second: given the few values a caller may set, it produces the exact bytes
that are PUT.  Like the rest of ``client/`` it imports no ``mcp`` and knows
nothing about tool contracts.

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
from datetime import date, datetime, timezone

import icalendar
from yandex_core.errors import ProtocolError

__all__ = [
    "EventDraft",
    "PRODID",
    "new_uid",
    "build_event_document",
    "written_boundary",
]

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
    if not draft.summary or not draft.summary.strip():
        raise ProtocolError(
            "An event needs a summary: an untitled event cannot be found again."
        )
    start_is_day = not isinstance(draft.start, datetime)
    end_is_day = not isinstance(draft.end, datetime)
    if start_is_day != end_is_day:
        raise ProtocolError(
            "`start` and `end` must both be dates for an all-day event, or both "
            "be timestamps. One of each has no reading that is not a guess."
        )
    for name, value in (("start", draft.start), ("end", draft.end)):
        if isinstance(value, datetime) and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ProtocolError(
                f"`{name}` has no UTC offset, so it names a different moment to "
                "every reader. Give an explicit one, for example "
                "2026-06-08T09:00:00+03:00."
            )
    if _as_instant(draft.end) <= _as_instant(draft.start):
        raise ProtocolError(
            "`end` must be after `start`; an event that ends when it begins "
            "occupies no time. For an all-day event `end` is exclusive, so a "
            "single day ends on the following date."
        )


def _as_instant(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
