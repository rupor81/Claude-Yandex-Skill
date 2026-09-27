"""The ``mail_message_get`` tool contract.

This module imports no protocol library. It owns validation, quote stripping on
request, the character window, and the truncation marker; the client below it
finds, fetches and decodes the text part.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Annotated

from pydantic import Field

from yandex_core.errors import ProtocolError
from yandex_core.paging import decode_cursor, encode_cursor
from yandex_core.results import Chunk

from ..client.body import strip_quotes as remove_quotes
from ..client.imap_client import IMAPMailClient
from .messages import Address

__all__ = [
    "DEFAULT_MAX_CHARS",
    "MAX_MAX_CHARS",
    "TOOL_NAME",
    "MessageText",
    "build_mail_message_get",
]

TOOL_NAME = "mail_message_get"

#: Measured over 120 real messages: plain text p50 1.9k characters, p90 11k, max
#: 35k; rendered HTML p90 13k. This returns nine in ten whole.
DEFAULT_MAX_CHARS = 15_000
MIN_MAX_CHARS = 1_000
MAX_MAX_CHARS = 50_000

#: How far back from the limit a cut may move to land between words.
_WORD_SLACK = 200

ClientProvider = Callable[[], Awaitable[IMAPMailClient]]


class MessageText(Chunk):
    """One message's text, or a segment of it, with who and when for context."""

    uid: int = Field(description="IMAP UID of the message in `folder`.")
    folder: str = Field(description="The folder the message is in.")
    subject: str = Field(description="The subject, decoded.")
    sender: Address | None = Field(description="The From address, decoded.")
    date: str | None = Field(description="When the server received it, ISO 8601.")
    format: str = Field(
        description=(
            "`plain` -- the sender's plain text; `html` -- the message had only HTML, "
            "rendered here as text; `none` -- the message has no text at all."
        )
    )
    offset: int = Field(
        description="Index of this segment's first character in the text."
    )
    total_chars: int = Field(description="Length of the whole text, in characters.")
    notes: list[str] = Field(
        description=(
            "Anything the caller should know about how this text was produced: an "
            "HTML conversion, quoted history removed on request, a character set "
            "approximated. Empty when there is nothing to say."
        )
    )


def build_mail_message_get(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[MessageText]]:
    """Bind ``mail_message_get`` to a source of clients."""

    async def mail_message_get(
        uid: Annotated[
            int,
            Field(description="UID of the message, from `mail_messages_list`.", ge=1),
        ],
        folder: Annotated[
            str,
            Field(
                default="INBOX",
                description="The folder the UID came from, exactly as listed.",
            ),
        ] = "INBOX",
        max_chars: Annotated[
            int,
            Field(
                default=DEFAULT_MAX_CHARS,
                ge=MIN_MAX_CHARS,
                le=MAX_MAX_CHARS,
                description=(
                    f"Characters to return in one call (default {DEFAULT_MAX_CHARS})."
                ),
            ),
        ] = DEFAULT_MAX_CHARS,
        strip_quotes: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Remove quoted history and the signature. Off by default so "
                    "nothing is discarded unasked; when on, `notes` says how much "
                    "was removed."
                ),
            ),
        ] = False,
        cursor: Annotated[
            str | None,
            Field(default=None, description="Opaque cursor from the previous segment."),
        ] = None,
    ) -> MessageText:
        """Read one message's text. Long messages come in segments.

        When `complete` is false the text ends with a marker saying which
        characters were shown, and `next_cursor` continues from the next one.
        HTML-only mail is rendered to readable text, and `notes` says so.
        Reading never marks the message as read.
        """
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
            raise ProtocolError(
                "`uid` must be a positive integer from `mail_messages_list`."
            )
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not (MIN_MAX_CHARS <= max_chars <= MAX_MAX_CHARS)
        ):
            raise ProtocolError(
                f"`max_chars` must be between {MIN_MAX_CHARS} and {MAX_MAX_CHARS}."
            )
        folder_name = (folder or "").strip() or "INBOX"
        stamp = _stamp(uid, folder_name, bool(strip_quotes))
        resume = _decode(cursor, stamp) if cursor is not None else None

        client = await client_provider()
        message = await client.read_text(folder=folder_name, uid=uid)
        if resume is not None and (
            message.uidvalidity is None or resume["uv"] != message.uidvalidity
        ):
            raise ProtocolError(
                f"The folder {folder_name!r} was renumbered since this cursor was "
                "issued, so UID {uid} may name a different message now. Start "
                "again without a cursor."
            )

        text = message.text
        notes = list(message.notes)
        if strip_quotes:
            text, removed = remove_quotes(text)
            if removed:
                notes.append(
                    f"Quoted history and signature removed on request: {removed} "
                    "characters. Call again without `strip_quotes` to read them."
                )

        total = len(text)
        start = resume["offset"] if resume is not None else 0
        if start > total:
            raise ProtocolError("This cursor points past the end of the message.")
        end = start + max_chars
        complete = end >= total
        if complete:
            segment, end = text[start:], total
        else:
            space = text.rfind(" ", end - _WORD_SLACK, end)
            newline = text.rfind("\n", end - _WORD_SLACK, end)
            boundary = max(space, newline)
            if boundary > start:
                end = boundary + 1
            segment = text[start:end]
            segment += (
                f"\n\n[... truncated: characters {start + 1}-{end} of {total} shown. "
                "Pass next_cursor to read on.]"
            )

        header = message.header
        sender = header.sender
        return MessageText(
            text=segment,
            complete=complete,
            next_cursor=None
            if complete
            else encode_cursor(
                {"q": stamp, "offset": end, "uv": message.uidvalidity}, tool=TOOL_NAME
            ),
            uid=header.uid,
            folder=folder_name,
            subject=header.subject,
            sender=Address(name=sender[0], address=sender[1]) if sender else None,
            date=header.date.isoformat() if header.date else None,
            format=message.format,
            offset=start,
            total_chars=total,
            notes=notes,
        )

    mail_message_get.__name__ = TOOL_NAME
    return mail_message_get


def _stamp(uid: int, folder: str, stripped: bool) -> str:
    """Which text a cursor's offset is into: offsets into the stripped text mean
    nothing in the unstripped one, and nothing at all in another message."""
    text = f"{uid}\x1f{folder}\x1f{int(stripped)}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _decode(cursor: str, stamp: str) -> dict[str, int]:
    payload = decode_cursor(cursor, tool=TOOL_NAME)
    if payload.get("q") != stamp:
        raise ProtocolError(
            "This cursor belongs to a different message, folder, or `strip_quotes` "
            "setting. Pass them back exactly as they were, or start without a cursor."
        )
    offset, uidvalidity = payload.get("offset"), payload.get("uv")
    if not isinstance(offset, int) or offset < 0 or not isinstance(uidvalidity, int):
        raise ProtocolError("Cursor is not a cursor this server issued.")
    return {"offset": offset, "uv": uidvalidity}
