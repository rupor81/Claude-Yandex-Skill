"""The ``mail_messages_list`` tool contract.

This module imports no protocol library. It owns validation, filtering, the
window, and the completeness flag; the client below it owns IMAP.

Reading a message's headers costs ~40 ms cold on this server, measured, so a
filtered question over a busy range cannot be answered in one call. Each call
reads at most :data:`SCAN_BUDGET` messages, newest first, and says how many are
left. ``complete`` is true only when nothing in the range is left unread and
nothing read was held back by ``limit``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, Field

from yandex_core.errors import ProtocolError
from yandex_core.instants import checked_instant
from yandex_core.paging import checked_limit, decode_cursor, encode_cursor
from yandex_core.results import Page

from ..client.imap_client import HeaderRecord, IMAPMailClient

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SCAN_BUDGET",
    "TOOL_NAME",
    "Address",
    "MessagePage",
    "MessageSummary",
    "build_mail_messages_list",
]

TOOL_NAME = "mail_messages_list"

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MIN_LIMIT = 1

#: Messages whose headers one call reads. ~40 ms each cold, measured, so about
#: six seconds at worst -- inside an MCP client's patience, with the cursor making
#: the rest of the range reachable.
SCAN_BUDGET = 150

UNKNOWN_ATTACHMENTS = (
    "The server's description of this message's structure could not be read, "
    "so whether it has attachments is not known. It is not reported as none."
)

ClientProvider = Callable[[], Awaitable[IMAPMailClient]]


class Address(BaseModel):
    name: str = Field(description="Display name, decoded; empty when none was given.")
    address: str = Field(description="Email address.")


class MessageSummary(BaseModel):
    """One message's headers. Nothing from its body."""

    uid: int = Field(
        description=(
            "IMAP UID of the message in `folder`. Stable while the folder is; pass "
            "it with the folder to tools that read one message."
        )
    )
    folder: str = Field(description="The folder the message is in, verbatim.")
    date: str | None = Field(
        description=(
            "When the server received the message, ISO 8601 with its offset. This "
            "is the date the range was searched by, so it always falls inside it."
        )
    )
    sender: Address | None = Field(description="The From address, decoded.")
    to: list[Address] = Field(description="The To addresses, decoded.")
    subject: str = Field(description="The subject, decoded from MIME encoded-words.")
    unread: bool = Field(description="True when the message has not been read.")
    flags: list[str] = Field(
        description=(
            "IMAP flags as the server reports them. Besides the standard ones "
            "(\\\\Seen, \\\\Answered, \\\\Flagged, $Forwarded), Yandex adds keywords of "
            "its own: `encrypted` appears on every message and describes Yandex's "
            "storage, not the mail; `system_*` are Yandex's internal markers."
        )
    )
    size: int | None = Field(description="Size of the whole message in bytes.")
    has_attachments: bool | None = Field(
        description=(
            "True when the message carries an attachment as mail clients count "
            "one -- inline images such as signature logos are not. Null when it "
            "could not be determined; see `attachments_note`."
        )
    )
    attachments_note: str | None = Field(
        default=None, description="Why `has_attachments` is null, when it is."
    )


class MessagePage(Page[MessageSummary]):
    """A page of messages, and how much of the range is still unread."""

    remaining: int = Field(
        description=(
            "Messages in the range not yet read. Nonzero means `complete` is "
            "false: pass `next_cursor` back to continue. The server searches by "
            "whole days, so a few of these may fall just outside the exact range "
            "and will be skipped when read."
        )
    )


def build_mail_messages_list(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[MessagePage]]:
    """Bind ``mail_messages_list`` to a source of clients."""

    async def mail_messages_list(
        start: Annotated[
            str | None,
            Field(
                description=(
                    "Start of the range, inclusive: ISO 8601 with an explicit UTC "
                    "offset, e.g. 2026-09-01T00:00:00+03:00. Required -- there is "
                    "no all-history default."
                )
            ),
        ],
        end: Annotated[
            str | None,
            Field(description="End of the range, exclusive, same format. Required."),
        ],
        folder: Annotated[
            str,
            Field(
                default="INBOX",
                description=(
                    "Folder name exactly as `mail_folders_list` reports it, "
                    "hierarchy included (`Archive|2024`)."
                ),
            ),
        ] = "INBOX",
        from_contains: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Keep messages whose sender name or address contains this "
                    "text, case-insensitively."
                ),
            ),
        ] = None,
        subject_contains: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Keep messages whose decoded subject contains this text, "
                    "case-insensitively. Matched exactly as written, word forms "
                    "included -- `встреча` does not match `встречи`."
                ),
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_LIMIT,
                ge=MIN_LIMIT,
                le=MAX_LIMIT,
                description=f"Maximum messages to return (default {DEFAULT_LIMIT}).",
            ),
        ] = DEFAULT_LIMIT,
        cursor: Annotated[
            str | None,
            Field(default=None, description="Opaque cursor from the previous page."),
        ] = None,
    ) -> MessagePage:
        """List message headers in a date range, newest first.

        Each call reads at most a bounded number of messages, because reading
        headers is slow on this server. When `complete` is false -- which can
        happen even when no message matched yet -- pass `next_cursor` back to
        read further. Filters match the decoded sender and subject exactly as
        written, case-insensitively.
        """
        if start is None or end is None:
            raise ProtocolError(
                "`start` and `end` are both required. There is no all-history "
                "default: give the period to look in, with an explicit offset."
            )
        begins = checked_instant(start, "start")
        ends = checked_instant(end, "end")
        if ends <= begins:
            raise ProtocolError(
                f"`end` must be after `start`; got start={begins.isoformat()} and "
                f"end={ends.isoformat()}."
            )
        limit_used = checked_limit(limit, minimum=MIN_LIMIT, maximum=MAX_LIMIT)
        folder_name = (folder or "").strip() or "INBOX"
        sender_filter = (from_contains or "").strip().casefold() or None
        subject_filter = (subject_contains or "").strip().casefold() or None
        stamp = _query_stamp(begins, ends, folder_name, sender_filter, subject_filter)
        resume = _decode(cursor, stamp) if cursor is not None else None

        state: dict[str, object] = {}

        def choose(uids: list[int], uidvalidity: int | None) -> list[int]:
            if resume is not None:
                if uidvalidity is None or resume["uv"] != uidvalidity:
                    raise ProtocolError(
                        f"The folder {folder_name!r} was renumbered since this "
                        "cursor was issued, so its position no longer names a "
                        "message. Start again without a cursor."
                    )
                below = resume["below"]
                pending = [u for u in reversed(uids) if u < below]
            else:
                pending = list(reversed(uids))
            chosen = pending[:SCAN_BUDGET]
            state.update(pending=pending, chosen=chosen, uidvalidity=uidvalidity)
            return chosen

        def keep(records: list[HeaderRecord]) -> list[int]:
            by_uid = {r.uid: r for r in records}
            matches: list[HeaderRecord] = []
            stopped_at: int | None = None
            for uid in state["chosen"]:  # newest first
                record = by_uid.get(uid)
                if record is None or not _matches(
                    record, begins, ends, sender_filter, subject_filter
                ):
                    continue
                if len(matches) == limit_used:
                    stopped_at = matches[-1].uid
                    break
                matches.append(record)
            state.update(matches=matches, stopped_at=stopped_at)
            return [m.uid for m in matches]

        client = await client_provider()
        scan = await client.list_messages(
            folder=folder_name,
            since=_day_before(begins),
            before=_day_after(ends),
            choose=choose,
            keep=keep,
        )
        if "chosen" not in state or "matches" not in state:
            raise ProtocolError(
                "The mail client did not consult the window, so this page's "
                "bounds were never applied. Nothing is returned rather than a "
                "page whose limit and request cost are both unknown."
            )

        pending: list[int] = state["pending"]  # type: ignore[assignment]
        chosen: list[int] = state["chosen"]  # type: ignore[assignment]
        stopped_at = state["stopped_at"]
        if stopped_at is not None:
            below = stopped_at
        elif chosen:
            below = chosen[-1]
        else:
            below = None
        remaining = [u for u in pending if below is not None and u < below]
        complete = not remaining

        items = [
            _summary(record, folder_name, scan.attachments.get(record.uid))
            for record in state["matches"]  # type: ignore[union-attr]
        ]
        return MessagePage(
            items=items,
            complete=complete,
            next_cursor=None
            if complete
            else encode_cursor(
                {"q": stamp, "below": below, "uv": scan.uidvalidity}, tool=TOOL_NAME
            ),
            remaining=len(remaining),
        )

    mail_messages_list.__name__ = TOOL_NAME
    return mail_messages_list


def _day_before(moment: datetime) -> date:
    """The SEARCH day window is widened a day each side.

    IMAP compares dates "disregarding time and timezone" in whatever zone the
    server stored them, so a message just after midnight UTC may carry the
    previous day there. Widening costs a few extra headers; not widening loses
    messages the exact range includes, with nothing to show for it.
    """
    return (moment.astimezone(UTC) - timedelta(days=1)).date()


def _day_after(moment: datetime) -> date:
    return (moment.astimezone(UTC) + timedelta(days=2)).date()


def _matches(
    record: HeaderRecord,
    begins: datetime,
    ends: datetime,
    sender_filter: str | None,
    subject_filter: str | None,
) -> bool:
    if record.date is None or not begins <= record.date < ends:
        return False
    if subject_filter and subject_filter not in record.subject.casefold():
        return False
    if sender_filter:
        name, address = record.sender or ("", "")
        if (
            sender_filter not in name.casefold()
            and sender_filter not in address.casefold()
        ):
            return False
    return True


def _summary(
    record: HeaderRecord, folder: str, presence: bool | None
) -> MessageSummary:
    sender = record.sender
    return MessageSummary(
        uid=record.uid,
        folder=folder,
        date=record.date.isoformat() if record.date else None,
        sender=Address(name=sender[0], address=sender[1]) if sender else None,
        to=[Address(name=n, address=a) for n, a in record.to],
        subject=record.subject,
        unread="\\Seen" not in record.flags,
        flags=list(record.flags),
        size=record.size,
        has_attachments=presence,
        attachments_note=UNKNOWN_ATTACHMENTS if presence is None else None,
    )


def _query_stamp(*parts: object) -> str:
    """The question a cursor belongs to, so it cannot resume a different one."""
    text = "\x1f".join(
        p.astimezone(UTC).isoformat() if isinstance(p, datetime) else str(p or "")
        for p in parts
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _decode(cursor: str, stamp: str) -> dict[str, int]:
    payload = decode_cursor(cursor, tool=TOOL_NAME)
    below, uidvalidity = payload.get("below"), payload.get("uv")
    if payload.get("q") != stamp:
        raise ProtocolError(
            "This cursor was issued for a different question. Pass `start`, "
            "`end`, `folder` and the filters back exactly as they were, or start "
            "again without a cursor."
        )
    if not isinstance(below, int) or not isinstance(uidvalidity, int):
        raise ProtocolError("Cursor is not a cursor this server issued.")
    return {"below": below, "uv": uidvalidity}
