"""Naming and ageing for the throwaway objects live tests create.

Shared by the live suite, which makes the names, and by the unit suite, which
tests the rules without a network.

Two decisions are recorded here rather than in a commit message.

**The name carries its own timestamp.** The deferred-work entry that asked for
this proposed removing anything named ``yandex-mcp-live-*`` older than an hour --
but the names it was written about were ``yandex-mcp-live-f57b5924``, which carry
no time at all. Age was not derivable from them, and CalDAV does not promise a
creation date for a collection, so the rule as written could not be implemented.
Putting the stamp in the name makes it decidable with no extra request.

**Stale objects are reported, never removed automatically.** A name match is a
heuristic, not proof of ownership, and this project's own position is that a
delete is the least recoverable thing it does. A sweep that quietly removed
collections from the operator's real account on a name match would be exactly
the "harm with no sign of harm" the whole suite exists to prevent. What the leak
actually cost was that nobody noticed it; being told is the fix for that.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = [
    "SCRATCH_PREFIX",
    "STALE_AFTER",
    "ScratchCalendar",
    "new_scratch_name",
    "stale_report",
    "stale_scratch_calendars",
]

SCRATCH_PREFIX = "yandex-mcp-live-"

#: How long after its run a throwaway object is a leak rather than a live test's
#: working space. Generous on purpose: the full suite takes about three minutes,
#: and reporting a calendar another process is still using would train the
#: operator to ignore the report.
STALE_AFTER = timedelta(hours=1)

_STAMPED = re.compile(
    rf"^{re.escape(SCRATCH_PREFIX)}(?P<stamp>\d{{8}}T\d{{6}}Z)-[0-9a-f]+$"
)


def new_scratch_name(now: datetime | None = None) -> str:
    """A throwaway name that says when it was made."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"{SCRATCH_PREFIX}{moment.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class ScratchCalendar:
    """One throwaway object that outlived the run that made it."""

    name: str
    #: When it was made, or ``None`` for a name from before stamps existed.
    #: Unknown is reported as unknown; it is never assumed to be recent, which
    #: would hide exactly the leaks this was written for.
    made: datetime | None

    @property
    def age_note(self) -> str:
        if self.made is None:
            return "age unknown -- the name carries no timestamp, so it predates this check"
        return f"made {self.made.isoformat()}"


def stale_scratch_calendars(
    names: list[str], *, now: datetime | None = None
) -> list[ScratchCalendar]:
    """Throwaway objects old enough to be leaks, oldest first.

    A name that is not ours is never returned: the operator's own calendars are
    none of this check's business, whatever they are called.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    found: list[ScratchCalendar] = []
    for name in names:
        if not name.startswith(SCRATCH_PREFIX):
            continue
        match = _STAMPED.match(name)
        if match is None:
            found.append(ScratchCalendar(name=name, made=None))
            continue
        try:
            made = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            found.append(ScratchCalendar(name=name, made=None))
            continue
        if moment - made >= STALE_AFTER:
            found.append(ScratchCalendar(name=name, made=made))
    return sorted(found, key=lambda item: (item.made is not None, item.made or moment))


def stale_report(stale: list[ScratchCalendar]) -> str:
    """What to print when something was left behind. Empty when nothing was."""
    if not stale:
        return ""
    lines = [
        f"{len(stale)} throwaway object(s) from an earlier live run are still on "
        "this account. A run that lost its network cannot clean up after itself, "
        "so they are reported rather than removed -- a name match is not proof of "
        "ownership, and a delete is the least recoverable thing this project does.",
        "",
    ]
    lines.extend(f"  {item.name}  ({item.age_note})" for item in stale)
    lines += [
        "",
        "Look at what they hold before removing them, then remove them by hand.",
    ]
    return "\n".join(lines)
