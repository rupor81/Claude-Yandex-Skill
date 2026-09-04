"""Opaque cursors.

Callers must never parse a cursor, so it is base64 of a JSON payload the core
owns.  Decoding validates aggressively: a cursor we did not mint is a
``ProtocolError``, never a silently ignored argument.

A cursor also carries the name of the tool that issued it.  Without that, one
listing's cursor decodes cleanly in another and is honoured as an offset into a
different collection, which is exactly the kind of quiet wrong answer this
project refuses to produce.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from .errors import ProtocolError

__all__ = ["encode_cursor", "decode_cursor"]

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
