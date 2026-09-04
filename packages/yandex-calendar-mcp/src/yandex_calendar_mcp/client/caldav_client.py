"""CalDAV access, and the only place CalDAV exceptions exist.

This module imports no ``mcp`` and is usable from a plain script.  The ``caldav``
library is blocking, so the single thread hop lives here -- ``tools/`` sees only
awaitables.

Two translation hazards are handled deliberately:

* ``caldav`` raises ``AuthorizationError`` for 401 *and* 403.  Yandex 360 answers
  403 when an administrator has disabled app passwords, which is an organisation
  policy problem and not a wrong password.  The status code on the attached
  response separates them when there is one; the reason phrase is the fallback,
  and when neither can settle it the caller is told so rather than guessed at.
* ``caldav`` does **not** wrap transport failures.  Connection resets, DNS
  failures and timeouts arrive as ``niquests`` exceptions and escape this module
  unless caught explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

import anyio.to_thread
import caldav
from caldav.lib import error as caldav_error
from yandex_core.errors import (
    AuthError,
    NotFound,
    PolicyError,
    ProtocolError,
    RateLimited,
    TransportError,
)

try:  # pragma: no cover - import shape depends on the installed caldav
    from niquests import exceptions as http_error
except ImportError:  # pragma: no cover
    from requests import exceptions as http_error  # type: ignore[no-redef]

__all__ = ["CalendarRef", "CalDAVCalendarClient"]

_APP_PASSWORD_HINT = (
    "Yandex CalDAV rejects OAuth tokens; the credential must be an app password "
    "created in Yandex ID."
)


@dataclass(frozen=True, slots=True)
class CalendarRef:
    """One calendar as the wire describes it: a display name and its URL."""

    name: str
    url: str


class CalDAVCalendarClient:
    """A blocking CalDAV connection, exposed as async methods.

    ``DAVClient`` is constructed directly rather than via ``get_davclient()``:
    the factory consults ``CALDAV_*`` environment variables and a config file of
    its own, can return ``None``, and would read credentials outside
    ``yandex_core.credentials``.
    """

    def __init__(
        self,
        *,
        url: str,
        username: str,
        password: str,
        credential_name: str = "calendar app password",
        timeout: int = 30,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._credential_name = credential_name
        self._timeout = timeout

    async def list_calendars(self) -> list[CalendarRef]:
        """Every calendar on the principal, in the order the server returns them."""
        return await anyio.to_thread.run_sync(self._list_calendars_blocking)

    # -- blocking half -----------------------------------------------------

    def _list_calendars_blocking(self) -> list[CalendarRef]:
        with self._translated():
            # The client owns a TLS connection pool, so it is closed here rather
            # than left to the garbage collector once per call.
            with caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
                timeout=self._timeout,
            ) as client:
                principal = client.principal()
                return [
                    CalendarRef(name=_display_name(calendar), url=str(calendar.url))
                    for calendar in principal.calendars()
                ]

    def _translated(self) -> "_Translator":
        return _Translator(self._credential_name, self._url)


def _display_name(calendar: object) -> str:
    """A calendar's human name, falling back to the last URL segment."""
    try:
        name = calendar.name  # type: ignore[attr-defined]
    except AttributeError:
        # Only a genuinely absent attribute is a fallback. A DAV or HTTP failure
        # while fetching the name must reach the translation boundary, not be
        # dressed up as a calendar called after its own URL.
        name = None
    if name:
        return str(name)
    return str(getattr(calendar, "url", "")).rstrip("/").rsplit("/", 1)[-1] or "(unnamed)"


class _Translator:
    """Context manager turning protocol exceptions into the core taxonomy."""

    def __init__(self, credential_name: str, url: str) -> None:
        self._credential_name = credential_name
        self._url = url

    def __enter__(self) -> "_Translator":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        if exc is None:
            return False
        raise self._translate(exc) from exc

    def _translate(self, exc: BaseException) -> Exception:
        # Transport first: caldav does not wrap these, so they would otherwise
        # escape client/ as a raw HTTP-library exception.
        if isinstance(exc, http_error.RequestException):
            return TransportError(
                f"Could not reach {self._url}: the network or the host is unavailable "
                f"({type(exc).__name__})."
            )

        if isinstance(exc, caldav_error.AuthorizationError):
            try:
                forbidden = _is_forbidden(exc)
            except _Undecidable:
                # Guessing here would either accuse a correct password or send an
                # operator to an administrator who can do nothing. Say both.
                return AuthError(
                    f"Yandex refused the connection and did not say whether the "
                    f"cause was a rejected {self._credential_name} (401) or "
                    "organisation policy disabling app passwords (403): it "
                    "returned neither a status code nor a usable reason phrase. "
                    f"Check the {self._credential_name} first, then whether an "
                    f"administrator has disabled app passwords. {_APP_PASSWORD_HINT}"
                )
            if forbidden:
                return PolicyError(
                    "Yandex refused the connection with 403. On Yandex 360 this means "
                    "organisation policy has disabled app passwords; an administrator "
                    "must re-enable them."
                )
            return AuthError(
                f"Yandex rejected the {self._credential_name}: it is wrong or has been "
                f"revoked. {_APP_PASSWORD_HINT}"
            )

        if isinstance(exc, caldav_error.NotFoundError):
            return NotFound(f"Nothing found at {self._url}.")

        if isinstance(exc, caldav_error.RateLimitError):
            return RateLimited("Yandex is rate limiting this account; retry later.")

        if isinstance(exc, caldav_error.DAVError):
            return ProtocolError(
                f"Yandex answered {self._url} in a way this server cannot honour "
                f"({type(exc).__name__})."
            )

        if isinstance(exc, Exception):
            return ProtocolError(
                f"Unexpected failure talking to {self._url} ({type(exc).__name__})."
            )
        return exc  # BaseException (KeyboardInterrupt, Cancelled) passes through.


#: The exact reason phrase for 403; anything else is not treated as one.
_FORBIDDEN_PHRASE = "forbidden"

#: Reason phrases caldav attaches when the server gave it nothing usable.
_EMPTY_REASONS = frozenset({"", "none", "none given", "no reason"})


class _Undecidable(Exception):
    """Neither a status code nor a usable reason phrase was available."""


def _is_forbidden(exc: BaseException) -> bool:
    """Distinguish 403 (policy) from 401 (bad password) on a caldav auth error.

    ``AuthorizationError`` covers both, and carries no status of its own -- the
    number, when there is one, is on the response attached to it. The reason
    phrase is the fallback, matched exactly rather than by substring so that a
    phrase merely containing the word cannot decide the question.

    Raises:
        _Undecidable: when neither source can settle it.
    """
    status = _status_from(exc)
    if status is not None:
        return status == 403

    reason = str(getattr(exc, "reason", "") or "").strip().lower()
    if reason == _FORBIDDEN_PHRASE:
        return True
    if reason and reason not in _EMPTY_REASONS:
        return False
    raise _Undecidable


def _status_from(exc: BaseException) -> int | None:
    """The numeric HTTP status from the response caldav attached, if any."""
    for attribute in ("response", "resp", "reason_code", "status_code", "status"):
        candidate = getattr(exc, attribute, None)
        if isinstance(candidate, bool) or candidate is None:
            continue
        if isinstance(candidate, int):
            return candidate
        for field in ("status_code", "status"):
            nested = getattr(candidate, field, None)
            if isinstance(nested, int) and not isinstance(nested, bool):
                return nested
    return None
