"""The single error taxonomy shared by every Yandex connector.

Client layers translate protocol exceptions into these at their own boundary, so
no protocol exception type ever escapes ``client/``.  Nothing here ever carries a
secret: messages name *which* credential failed, never its value.
"""

from __future__ import annotations

__all__ = [
    "YandexError",
    "AuthError",
    "CredentialNotFound",
    "PolicyError",
    "NotFound",
    "Conflict",
    "RateLimited",
    "TransportError",
    "ProtocolError",
    "NotConfigured",
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


class _Reasoned(YandexError):
    """A failure that also carries its explanation without any setup hint.

    A caller that renders a hint of its own -- one naming the service and the
    profile actually being worked on -- reads ``reason`` instead of the message,
    so the operator is never shown two hints, and no caller has to recognise a
    hint by matching prose.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason if reason is not None else message


class NotConfigured(_Reasoned, ProtocolError):
    """Nothing is set up yet.

    This is the *absence* of configuration -- no config file, no profiles -- and
    is deliberately narrower than ``ProtocolError``: a config file that exists
    but is corrupt, or names a profile that is not there, is broken rather than
    absent and stays a plain ``ProtocolError``.
    """


class CredentialNotFound(_Reasoned, AuthError):
    """No credential is stored anywhere for this service and profile.

    Narrower than ``AuthError`` for the same reason: a credential that exists but
    cannot be used -- a world-readable fallback file, a password Yandex rejects
    -- is a problem to fix, not a setup step that has not happened yet.
    """
