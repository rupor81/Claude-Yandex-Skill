"""The single error taxonomy shared by every Yandex connector.

Client layers translate protocol exceptions into these at their own boundary, so
no protocol exception type ever escapes ``client/``.  Nothing here ever carries a
secret: messages name *which* credential failed, never its value.
"""

from __future__ import annotations

__all__ = [
    "YandexError",
    "AuthError",
    "PolicyError",
    "NotFound",
    "Conflict",
    "RateLimited",
    "TransportError",
    "ProtocolError",
]


class YandexError(Exception):
    """Base class for every failure surfaced by a Yandex connector."""


class AuthError(YandexError):
    """The credential is missing, wrong, or revoked."""


class PolicyError(YandexError):
    """The credential is valid but organisation policy forbids the action."""


class NotFound(YandexError):
    """The addressed object does not exist."""


class Conflict(YandexError):
    """The object changed underneath us; nothing was modified."""


class RateLimited(YandexError):
    """The service asked us to slow down."""


class TransportError(YandexError):
    """The service could not be reached at all."""


class ProtocolError(YandexError):
    """The service answered, but not in a way we can honour."""
