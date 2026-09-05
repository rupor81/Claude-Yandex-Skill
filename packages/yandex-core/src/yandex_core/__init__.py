"""Shared contracts for the Yandex MCP connectors.

Deliberately narrow: this package knows about results, errors, cursors, risk,
profiles, and credentials.  It knows nothing about any particular server, and it
is not generalised for protocols that do not exist yet.
"""

from .errors import (
    AuthError,
    Conflict,
    NotFound,
    PolicyError,
    ProtocolError,
    RateLimited,
    TransportError,
    YandexError,
)
from .paging import checked_limit, decode_cursor, encode_cursor
from .results import Page

__all__ = [
    "AuthError",
    "Conflict",
    "NotFound",
    "PolicyError",
    "ProtocolError",
    "RateLimited",
    "TransportError",
    "YandexError",
    "Page",
    "encode_cursor",
    "decode_cursor",
    "checked_limit",
]
