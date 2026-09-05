"""``yandex-mcp verify`` -- does the stored configuration actually work?

Setup stores a credential without ever using it. A wrong app password, an
organisation that forbids app passwords, or an unreachable host therefore stays
invisible until a tool call fails inside an MCP client, where the operator can
do nothing with the answer. This module makes one real, minimal call per
configured service and reports a line each.

Four properties are deliberate:

* **A real call.** A configuration-only check would report success for precisely
  the case being diagnosed -- a stored password Yandex rejects.
* **Every service is reported.** A check returns a state; it never raises, so one
  broken service cannot hide the rest.
* **The connector is imported inside the check.** A missing or broken calendar
  package costs one reported line rather than the whole command.
* **Every call has a deadline.** A black-holed host would otherwise stall the
  command indefinitely, which is the one failure an operator cannot even read.

Nothing here reads or prints a secret: credentials go straight from
``yandex_core.credentials`` to the wire, every message names a cause rather
than a value, and the one secret this command does hold is scrubbed out of any
message a lower layer hands back.

"Absent" and "broken" are kept apart throughout. Nothing set up yet is not a
failure and exits 0; configuration that exists and is wrong is a failure and
exits non-zero, because the two need opposite things from the operator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from yandex_core.config import (
    DEFAULT_PROFILE_NAME,
    load_profile,
    selected_profile_name,
)
from yandex_core.credentials import get_secret, redact_secret
from yandex_core.errors import (
    AuthError,
    Conflict,
    CredentialNotFound,
    NotConfigured,
    NotFound,
    PolicyError,
    ProtocolError,
    RateLimited,
    TransportError,
    YandexError,
)

__all__ = [
    "CHECK_TIMEOUT_SECONDS",
    "CHECKS",
    "ServiceResult",
    "State",
    "check_calendar",
    "check_disk",
    "check_mail",
    "render_results",
    "run_checks",
    "setup_hint",
]

#: How long one service's real call may take before the check gives up. Verify
#: promises a stable exit code to a script; a host that accepts a connection and
#: then answers nothing would otherwise hold the command open for minutes.
CHECK_TIMEOUT_SECONDS = 30.0

#: A later epic replaces each placeholder with a real check; the phrasing has to
#: stay distinct from "unconfigured", which means "you can fix this".
NOT_YET_BUILT_DETAIL = (
    "This connector is not part of this release; a later epic adds it. "
    "Nothing to configure yet."
)


class State(Enum):
    """What verification found. ``FAILED`` and ``TIMED_OUT`` fail the command."""

    REACHABLE = "reachable"
    UNCONFIGURED = "unconfigured"
    NOT_YET_BUILT = "not yet built"
    TIMED_OUT = "timed out"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """One service's verdict. Checks return these instead of raising."""

    service: str
    state: State
    detail: str
    cause: str | None = None
    profile: str | None = None

    @property
    def is_failure(self) -> bool:
        return self.state in (State.FAILED, State.TIMED_OUT)


#: The cause label printed for each error in the core taxonomy. The label is the
#: operator's index into "who can fix this"; the exception's own message says
#: what happened. Order matters: the walk is an ordered ``isinstance`` test, so
#: every subclass has to come before the class it derives from.
_CAUSE_LABELS: tuple[tuple[type[BaseException], str], ...] = (
    (PolicyError, "organisation policy"),
    (AuthError, "credential"),
    (TransportError, "transport"),
    (RateLimited, "rate limit"),
    (NotFound, "not found"),
    (Conflict, "conflict"),
    (ProtocolError, "protocol"),
    (YandexError, "service"),
)


def _cause_for(exc: BaseException) -> str:
    for error_type, label in _CAUSE_LABELS:
        if isinstance(exc, error_type):
            return label
    return "unexpected"


def setup_hint(service: str, profile: str | None = None) -> str:
    """The command that would configure exactly what was just checked.

    A hint naming a different service, or writing the default profile when a
    named one was verified, tells the operator to fix something other than the
    thing that is broken.
    """
    command = f"yandex-mcp setup {service}"
    if profile and profile != DEFAULT_PROFILE_NAME:
        command += f" --profile {profile}"
    return f"Run `{command}` to configure it."


def _collapse(text: str) -> str:
    """One line's worth of whitespace, so a message cannot break the columns."""
    return " ".join(text.split())


def _sentence(text: str) -> str:
    """Terminate a message so the hint that follows reads as its own sentence."""
    text = _collapse(text)
    if not text or text.endswith((".", "!", "?", ":", ";")):
        return text
    return f"{text}."


def _describe(exc: BaseException, secret: str | None = None) -> str:
    """The operator-facing text for an exception, scrubbed and never empty."""
    return _collapse(redact_secret(str(exc), secret)) or type(exc).__name__


def _reason_of(exc: BaseException) -> str:
    """An unconfigured exception's explanation, without its generic hint."""
    return _collapse(str(getattr(exc, "reason", None) or exc)) or type(exc).__name__


def _unconfigured(service: str, profile: str, exc: BaseException) -> ServiceResult:
    detail = f"{_sentence(_reason_of(exc))} {setup_hint(service, profile)}".strip()
    return ServiceResult(service, State.UNCONFIGURED, detail, profile=profile)


def _profile_label(profile_name: str | None) -> str:
    """The profile a line is about, known before any call is made.

    A failing line with no profile on it cannot be acted on when more than one
    profile exists, so the label is resolved even when the configuration that
    would name it is unreadable.
    """
    if profile_name:
        return profile_name
    try:
        return selected_profile_name()
    except Exception:  # noqa: BLE001 - an unreadable file still gets a label
        return DEFAULT_PROFILE_NAME


def _load_calendar_client_class() -> type:
    """Import the calendar client, here and not at module import time.

    The CLI does not depend on the calendar package. Importing it lazily is what
    keeps a missing or broken connector to one reported line.
    """
    from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient

    return CalDAVCalendarClient


def check_calendar(profile_name: str | None = None) -> ServiceResult:
    """Attempt one real ``list_calendars`` call and report what happened."""
    service = "calendar"
    profile = _profile_label(profile_name)

    try:
        client_class = _load_calendar_client_class()
    except Exception as exc:  # noqa: BLE001 - a broken connector is a reported line
        return ServiceResult(
            service,
            State.FAILED,
            f"The calendar connector could not be loaded ({type(exc).__name__}: "
            f"{_describe(exc)}). Reinstall the yandex-calendar-mcp package.",
            cause="connector unavailable",
            profile=profile,
        )

    try:
        loaded = load_profile(profile_name)
    except NotConfigured as exc:
        # Nothing is set up yet: there is nothing broken, only nothing to check.
        return _unconfigured(service, profile, exc)
    except ProtocolError as exc:
        # Configuration exists and cannot be used -- unreadable, not valid TOML,
        # or naming a profile that is not there. Telling the operator to run
        # setup here would overwrite one problem with another.
        return ServiceResult(
            service,
            State.FAILED,
            _describe(exc),
            cause="configuration",
            profile=profile,
        )

    profile = loaded.name

    try:
        password = get_secret(service, profile)
    except CredentialNotFound as exc:
        # The same "not set up yet" state, reached one step later.
        return _unconfigured(service, profile, exc)
    except AuthError as exc:
        # A credential that exists but cannot be used -- a world-readable
        # fallback file. Setup would replace a password that is perfectly good.
        return ServiceResult(
            service, State.FAILED, _describe(exc), cause="credential", profile=profile
        )

    client = client_class(
        url=loaded.caldav_url,
        username=loaded.login,
        password=password,
    )

    try:
        calendars = asyncio.run(
            asyncio.wait_for(client.list_calendars(), CHECK_TIMEOUT_SECONDS)
        )
    except TimeoutError:
        return ServiceResult(
            service,
            State.TIMED_OUT,
            f"{loaded.caldav_url} did not answer within "
            f"{CHECK_TIMEOUT_SECONDS:g} seconds.",
            cause="deadline",
            profile=profile,
        )
    except Exception as exc:  # noqa: BLE001 - every cause becomes a reported line
        return ServiceResult(
            service,
            State.FAILED,
            _describe(exc, password),
            cause=_cause_for(exc),
            profile=profile,
        )

    count = len(calendars)
    return ServiceResult(
        service,
        State.REACHABLE,
        f"Answered with {count} {'calendar' if count == 1 else 'calendars'}.",
        profile=profile,
    )


def check_mail(profile_name: str | None = None) -> ServiceResult:
    return ServiceResult(
        "mail",
        State.NOT_YET_BUILT,
        NOT_YET_BUILT_DETAIL,
        profile=_profile_label(profile_name),
    )


def check_disk(profile_name: str | None = None) -> ServiceResult:
    return ServiceResult(
        "disk",
        State.NOT_YET_BUILT,
        NOT_YET_BUILT_DETAIL,
        profile=_profile_label(profile_name),
    )


#: Adding a service is adding a function here. Order is report order.
CHECKS: tuple[Callable[[str | None], ServiceResult], ...] = (
    check_calendar,
    check_mail,
    check_disk,
)


def run_checks(profile_name: str | None = None) -> list[ServiceResult]:
    """Run every check. One check's failure never stops another from reporting."""
    results: list[ServiceResult] = []
    for check in CHECKS:
        try:
            results.append(check(profile_name))
        except Exception as exc:  # noqa: BLE001 - a check that raises is itself a bug
            results.append(
                ServiceResult(
                    getattr(check, "__name__", "unknown").removeprefix("check_"),
                    State.FAILED,
                    f"The check itself failed ({type(exc).__name__}: {_describe(exc)}).",
                    cause="internal",
                    profile=_profile_label(profile_name),
                )
            )
    return results


def render_results(results: Sequence[ServiceResult]) -> list[str]:
    """One human-readable line per service, aligned into columns."""
    profiles = [f"{r.profile!r}" if r.profile else "-" for r in results]
    service_width = max((len(r.service) for r in results), default=0)
    profile_width = max((len(p) for p in profiles), default=0)
    state_width = max((len(r.state.value) for r in results), default=0)
    lines = []
    for result, profile in zip(results, profiles, strict=True):
        detail = _collapse(result.detail)
        text = f"{result.cause}: {detail}" if result.cause else detail
        lines.append(
            f"{result.service:<{service_width}}  "
            f"{profile:<{profile_width}}  "
            f"{result.state.value:<{state_width}}  {text}"
        )
    return lines
