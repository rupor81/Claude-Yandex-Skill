"""Timezone-aware instants, as every tool that takes a moment must accept them (AD-7).

Moved here from the calendar's `tools/timerange.py` when Mail became the second
tool to need it -- an early piece of story 2.5, which exists to move into the core
exactly what a second protocol proves to be common. Servers may not import one
another (AD-2), so the alternative was a copy, and two copies of a validation
rule drift.
"""

from __future__ import annotations

from datetime import datetime

from .errors import ProtocolError

__all__ = ["checked_instant"]


def checked_instant(value: object, name: str) -> datetime:
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
