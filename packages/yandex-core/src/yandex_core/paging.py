"""Opaque cursors.

Callers must never parse a cursor, so it is base64 of a JSON payload the core
owns.  Decoding validates aggressively: a cursor we did not mint is a
``ProtocolError``, never a silently ignored argument.

A cursor also carries the name of the tool that issued it.  Without that, one
listing's cursor decodes cleanly in another and is honoured as an offset into a
different collection, which is exactly the kind of quiet wrong answer this
project refuses to produce.

Two shapes of cursor live here.  An *index* cursor says how far into a list the
caller got, and is only safe where the list is stable -- a fixed set of
calendars.  A *position* cursor names the last item returned instead, so a
collection that gains or loses members between pages cannot silently skip or
repeat one; derived collections such as expanded event occurrences need that.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import ProtocolError

__all__ = [
    "checked_limit",
    "encode_cursor",
    "decode_cursor",
    "encode_position_cursor",
    "decode_position_cursor",
]

_VERSION = 1

_FOREIGN = "Cursor is not a cursor this server issued."


def encode_cursor(payload: dict[str, Any], *, tool: str) -> str:
    """Encode cursor state into an opaque, URL-safe string.

    Args:
        payload: the state the issuing tool needs back.
        tool: the name of the tool issuing it; only that tool may decode it.
    """
    raw = json.dumps(
        {"v": _VERSION, "t": tool, "p": payload}, sort_keys=True, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, tool: str) -> dict[str, Any]:
    """Decode a cursor minted by :func:`encode_cursor` for the same tool.

    Raises:
        ProtocolError: if the cursor is malformed, of an unknown version, or was
            issued by a different tool.
    """
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        envelope = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(_FOREIGN) from exc

    if not isinstance(envelope, dict) or envelope.get("v") != _VERSION:
        raise ProtocolError(_FOREIGN)

    issuer = envelope.get("t")
    if not isinstance(issuer, str) or issuer != tool:
        raise ProtocolError(
            f"Cursor was not issued by {tool!r}; pass back only the cursor that "
            "the same tool returned."
        )

    payload = envelope.get("p")
    if not isinstance(payload, dict):
        raise ProtocolError(_FOREIGN)
    return payload


def encode_position_cursor(position: Mapping[str, str | None], *, tool: str) -> str:
    """Encode a cursor naming the last item returned, rather than its index.

    Args:
        position: the fields that identify that item, all strings or ``None``.
        tool: the name of the tool issuing it; only that tool may decode it.
    """
    payload = {key: position[key] for key in sorted(position)}
    for key, value in payload.items():
        if value is not None and not isinstance(value, str):
            raise ProtocolError(
                f"Cursor position field {key!r} must be a string or null."
            )
    return encode_cursor({"after": payload}, tool=tool)


def decode_position_cursor(
    cursor: str, *, tool: str, fields: Sequence[str]
) -> dict[str, str | None]:
    """Decode a position cursor, insisting it names exactly the expected fields.

    A cursor carrying a different shape came from a different version of this
    tool; honouring it would resume from a position nobody can vouch for.

    Raises:
        ProtocolError: if the cursor is foreign, malformed, or the wrong shape.
    """
    payload = decode_cursor(cursor, tool=tool)
    after = payload.get("after")
    if not isinstance(after, dict) or set(after) != set(fields):
        raise ProtocolError(_FOREIGN)
    for value in after.values():
        if value is not None and not isinstance(value, str):
            raise ProtocolError(_FOREIGN)
    return dict(after)


def checked_limit(limit: object, *, minimum: int, maximum: int) -> int:
    """Enforce a listing's limit range here, not only in the JSON schema.

    The schema binds a model calling over the protocol; it binds nothing when
    the tool function is called directly, and the bound has to hold either way.
    Every paginated tool needs exactly this, so it lives beside the cursors
    rather than being copied into each one.

    Raises:
        ProtocolError: if ``limit`` is not an integer, or is out of range.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ProtocolError(f"`limit` must be an integer, not {type(limit).__name__}.")
    if not minimum <= limit <= maximum:
        raise ProtocolError(
            f"`limit` must be between {minimum} and {maximum}; got {limit}."
        )
    return limit
