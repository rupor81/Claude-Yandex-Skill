"""What a range query has to be, and how it admits to being incomplete.

Two tools now take a date range, and they must agree about it down to the
wording: two adjacent tools disagreeing about what an over-wide range means is
worse for a caller than either rule on its own.  The same goes for the rest of
what a range query shares -- the fingerprint that binds a cursor to the question
it answered, and the vocabulary for saying an answer fell short.  All of it
lives here once, rather than being copied into each tool and drifting.  Neither
tool reaches sideways into the other for it.

Every check happens *before* a request is made.  A naive timestamp means a
different moment to everyone who reads it, and an over-wide range is refused by
name rather than narrowed -- a quietly shortened window answers a question
nobody asked while looking complete.

Nothing here imports a protocol or iCalendar library, like everything else under
``tools/``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Literal

from yandex_core.errors import ProtocolError
from yandex_core.paging import decode_position_cursor

__all__ = [
    "MAX_RANGE_DAYS",
    "MIN_LIMIT",
    "MORE_PAGES",
    "RANGE_TRUNCATED",
    "UNREADABLE_DATA",
    "IncompleteReason",
    "checked_instant",
    "checked_calendar_url",
    "check_range",
    "query_stamp",
    "decoded_position",
    "incomplete_reasons",
]

#: The smallest limit any tool accepts.  Zero would ask for an answer that
#: cannot say whether there was one.
MIN_LIMIT = 1

#: The three ways an answer can fall short of the question, as a caller reads
#: them.  Spelled as literals as well as constants so the permitted values reach
#: the JSON schema an MCP client actually sees.
MORE_PAGES = "more_pages"
RANGE_TRUNCATED = "range_truncated"
UNREADABLE_DATA = "unreadable_data"

IncompleteReason = Literal["more_pages", "range_truncated", "unreadable_data"]

#: The widest span one call may ask for.  A range beyond this is refused by
#: name rather than narrowed: a quietly shortened window returns an answer that
#: looks complete for a question nobody asked.
MAX_RANGE_DAYS = 366


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


def checked_calendar_url(calendar_url: object) -> str | None:
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


def check_range(start: datetime, end: datetime) -> None:
    """Ordered, and no wider than the documented maximum.

    Raises:
        ProtocolError: if ``end`` is not after ``start``, or the span exceeds
            :data:`MAX_RANGE_DAYS`. Neither is repaired: an inverted range is a
            mistake with no obvious correct reading, and a narrowed one answers
            a different question.
    """
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


def query_stamp(
    *,
    start: datetime,
    end: datetime,
    calendar_url: str | None,
    extra: Sequence[str | None] = (),
) -> str:
    """A short fingerprint of the question a cursor was issued for.

    Without it, page one's cursor replayed with a different range decodes
    cleanly and resumes a different question -- the same quiet wrong answer the
    tool-name stamp already refuses for a different tool's cursor.  ``extra``
    carries whatever else narrowed the question, so a tool that adds a filter
    cannot forget to bind it.
    """
    parts = (
        start.astimezone(timezone.utc).isoformat(),
        end.astimezone(timezone.utc).isoformat(),
        calendar_url or "",
        *(value or "" for value in extra),
    )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def decoded_position(
    cursor: str,
    *,
    tool: str,
    fields: Sequence[str],
    query: str,
    restate: str,
) -> dict:
    """One decoded cursor, proven to belong to this tool and this question.

    Raises:
        ProtocolError: if the cursor was issued by another tool, is malformed,
            or names a different question. The last is refused rather than
            honoured: continuing a different range from it would look like
            paging and answer something nobody asked.
    """
    position = decode_position_cursor(cursor, tool=tool, fields=fields)
    if position["query"] != query:
        raise ProtocolError(
            "This cursor was issued for a different question. A cursor resumes "
            f"one range against one calendar; pass {restate} back exactly as "
            "they were, or start again without a cursor."
        )
    return position


def incomplete_reasons(
    *, more_remain: bool, truncated: bool, lost: bool
) -> tuple[IncompleteReason, ...]:
    """Every way this answer fell short, the one with an action attached first.

    More than one can be true at once, and naming only the first hides the
    others: a caller told "more pages" and nothing else, that stops after one
    page, never learns the expansion hit its ceiling and that the later part of
    the range is simply missing.
    """
    reasons: list[IncompleteReason] = []
    if more_remain:
        reasons.append(MORE_PAGES)
    if truncated:
        reasons.append(RANGE_TRUNCATED)
    if lost:
        reasons.append(UNREADABLE_DATA)
    return tuple(reasons)
