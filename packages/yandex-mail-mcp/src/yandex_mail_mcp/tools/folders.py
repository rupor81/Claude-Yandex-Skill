"""The ``mail_folders_list`` tool contract.

This module imports no protocol library. It owns validation, ordering, paging,
and the completeness flag; the client below it owns IMAP.

Ordering is this server's own rather than the mailbox's, because the measured
capability line carries no ``SORT`` -- there is no server order to resume
against, so a cursor that named a position in one would name nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated

from pydantic import BaseModel, Field

from yandex_core.errors import ProtocolError
from yandex_core.paging import (
    checked_limit,
    decode_position_cursor,
    encode_position_cursor,
)
from yandex_core.results import Page

from ..client.imap_client import FolderRef, IMAPMailClient

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "NO_COUNTS_NOT_SELECTABLE",
    "TOOL_NAME",
    "FolderSummary",
    "build_mail_folders_list",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MIN_LIMIT = 1

TOOL_NAME = "mail_folders_list"

#: Said in words rather than left as a bare absence. A caller that sees no count
#: and no reason cannot tell "this folder holds nothing" from "nobody asked".
NO_COUNTS_NOT_SELECTABLE = (
    "This is a container in the folder hierarchy, not a mailbox, so it holds no "
    "messages of its own and has no counts. Its children may."
)

ClientProvider = Callable[[], Awaitable[IMAPMailClient]]

_CURSOR_FIELDS = ("name",)


class FolderSummary(BaseModel):
    """One folder in the mailbox."""

    name: str = Field(
        description=(
            "Full folder name as the server spells it, including any parent "
            "path. Pass it verbatim to tools that take a folder."
        )
    )
    delimiter: str = Field(
        description=(
            "Character this server separates hierarchy levels with -- Yandex "
            "uses `|`, not `/`. Build a child's name with it rather than "
            "guessing."
        )
    )
    selectable: bool = Field(
        description=(
            "False for a container that holds only other folders. Such a "
            "folder has no messages and cannot be opened."
        )
    )
    messages: int | None = Field(
        default=None,
        description=(
            "Messages in the folder, or null when this could not be "
            "established. Null is never a count -- see `counts_note`."
        ),
    )
    unseen: int | None = Field(
        default=None,
        description="Unread messages, or null when this could not be established.",
    )
    counts_note: str | None = Field(
        default=None,
        description=(
            "Why the counts are absent, when they are. Null means they are "
            "present and true."
        ),
    )
    flags: list[str] = Field(
        default_factory=list,
        description="IMAP folder attributes as the server reported them.",
    )


def build_mail_folders_list(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[Page]]:
    """Bind ``mail_folders_list`` to a source of clients."""

    async def mail_folders_list(
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_LIMIT,
                ge=MIN_LIMIT,
                le=MAX_LIMIT,
                description=(
                    f"Maximum folders to return (default {DEFAULT_LIMIT}, "
                    f"maximum {MAX_LIMIT})."
                ),
            ),
        ] = DEFAULT_LIMIT,
        cursor: Annotated[
            str | None,
            Field(
                default=None,
                description="Opaque cursor from a previous truncated call.",
            ),
        ] = None,
    ) -> Page[FolderSummary]:
        """List the folders in the configured Yandex mailbox, with message counts.

        Returns at most `limit` folders, ordered by name. If more exist,
        `complete` is false and `next_cursor` carries the remainder. Counts are
        fetched only for the folders actually returned, because each one costs a
        request; a folder whose counts could not be had says so in `counts_note`
        rather than reporting zero.
        """
        limit_used = checked_limit(limit, minimum=MIN_LIMIT, maximum=MAX_LIMIT)
        after = _position_from(cursor)
        client = await client_provider()

        # The window is decided here and handed down, so the connection below
        # spends one STATUS per folder actually returned and not one more.
        state: dict[str, list[FolderRef]] = {}

        def count_for(folders: Sequence[FolderRef]) -> list[str]:
            ordered = sorted(folders, key=_sort_key)
            if after is not None:
                ordered = [ref for ref in ordered if _sort_key(ref) > after]
            window = ordered[:limit_used]
            state["ordered"] = list(ordered)
            state["window"] = window
            return [ref.name for ref in window if ref.selectable]

        listed = await client.list_folders(count_for=count_for)

        # `count_for` runs inside the call above. A client that did not call it
        # has not been told what the window is, so it fetched counts for
        # everything or for nothing -- and quietly re-deriving the window here
        # would hide that while returning a page that looks right. It is a
        # broken client, and it says so.
        if "window" not in state:
            raise ProtocolError(
                "The mail client did not consult the folder selector, so this "
                "page's bounds were never applied. Nothing is returned rather "
                "than a page whose limit and request cost are both unknown."
            )
        by_name = {ref.name: ref for ref in listed}
        window = [by_name.get(ref.name, ref) for ref in state["window"]]
        complete = len(window) == len(state["ordered"])

        return Page[FolderSummary](
            items=[_summary(ref) for ref in window],
            complete=complete,
            next_cursor=(
                None
                if complete
                else encode_position_cursor({"name": window[-1].name}, tool=TOOL_NAME)
            ),
        )

    mail_folders_list.__name__ = TOOL_NAME
    return mail_folders_list


def _summary(ref: FolderRef) -> FolderSummary:
    note = ref.counts_note
    if note is None and not ref.selectable:
        note = NO_COUNTS_NOT_SELECTABLE
    return FolderSummary(
        name=ref.name,
        delimiter=ref.delimiter,
        selectable=ref.selectable,
        messages=ref.messages,
        unseen=ref.unseen,
        counts_note=note,
        flags=list(ref.flags),
    )


def _sort_key(ref: FolderRef) -> str:
    """Total order over folders. A folder name is unique within a mailbox, so
    the name alone is a key and no tiebreak is needed."""
    return ref.name


def _position_from(cursor: str | None) -> str | None:
    """The folder a previous page stopped at, or None to start at the top.

    Naming the folder rather than counting how many were skipped is what keeps
    a page correct when the mailbox gains or loses one in between.
    """
    if cursor is None:
        return None
    return decode_position_cursor(cursor, tool=TOOL_NAME, fields=_CURSOR_FIELDS)["name"]
