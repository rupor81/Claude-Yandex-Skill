"""The one rule this suite cannot express as an assertion.

Yandex's rate limit is per account and does not reset between runs, so two full
runs in quick succession fail *somewhere* -- and never twice in the same place.
Every one of those tests passes alone in ten to fifteen seconds; there is nothing
in the code to fix.

That is a trap rather than an inconvenience. During epic 1 a flake here was
diagnosed twice: first as a test that cost too much (which produced a real
improvement and a wrong cause), and only then as the shared budget. A run that
begins minutes after the last one and fails in a new place invites exactly that
mistake again.

So the rule is stated where it will actually be read -- at the top of the run
that is about to break it. It warns rather than skips: a suite that silently
declines to run is a worse failure than one that runs and is honestly framed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from livescratch import stale_report, stale_scratch_calendars

#: How long the account's budget wants between full runs. Not measured to the
#: second -- four runs inside an hour was enough to move the failure around, and
#: several minutes between runs has been enough to avoid it.
QUIET_PERIOD_SECONDS = 300

#: Deliberately outside the repository: it is a fact about this machine's recent
#: network activity, not about the source.
_MARKER = Path(os.environ.get("TMPDIR", "/tmp")) / "yandex-mcp-live-last-run"


def _last_run_age() -> float | None:
    """Seconds since the last live run on this machine, or None if unknown."""
    try:
        return time.time() - _MARKER.stat().st_mtime
    except OSError:
        return None


@pytest.fixture(scope="session", autouse=True)
def _rate_limit_notice(request: pytest.FixtureRequest) -> Iterator[None]:
    """Say so, once, when this run is likely to be rate-limited."""
    if os.environ.get("YANDEX_MCP_LIVE_TESTS") != "1":
        return

    age = _last_run_age()
    if age is not None and age < QUIET_PERIOD_SECONDS:
        reporter = request.config.pluginmanager.getplugin("terminalreporter")
        message = (
            f"The last live run on this machine finished {int(age)}s ago, inside "
            f"the {QUIET_PERIOD_SECONDS}s quiet period. Yandex's rate limit is per "
            "account and does not reset between runs. A failure below may be the "
            "budget rather than a defect -- re-run the failing test alone before "
            "believing it."
        )
        if reporter is not None:
            reporter.write_line(f"\nWARNING: {message}\n", yellow=True, bold=True)
        else:  # pragma: no cover -- only under -p no:terminal
            print(f"WARNING: {message}")

    _report_leftovers(request)

    yield

    # Stamped after the run, not before: what matters is when the account last
    # stopped being hit, and a run that dies half way still hit it.
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.touch()
    except OSError:
        # Not being able to remember is not a reason to fail a passing suite.
        pass


def _report_leftovers(request: pytest.FixtureRequest) -> None:
    """Say so when an earlier run left something on the real account.

    This project has leaked a calendar twice: once because a cleanup used the
    wrong URL, and once because the network went away mid-run and no `finally`
    survives that. Both were found by a person reading the account afterwards,
    which is not a mechanism.

    It reports and does not remove. A name match is a heuristic, not proof of
    ownership, and a delete is the least recoverable thing this project does --
    quietly removing collections from somebody's real account on a name match
    would be the "harm with no sign of harm" the whole suite exists to prevent.
    What the leak actually cost was that nobody noticed; being told fixes that.

    Every failure here is swallowed. The check is a courtesy before the suite
    runs; the suite's own tests are what report a broken account.
    """
    try:
        from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient
        from yandex_core.config import load_profile
        from yandex_core.credentials import get_secret

        profile = load_profile()
        client = CalDAVCalendarClient(
            url=profile.caldav_url,
            username=profile.login,
            password=get_secret("calendar", profile.name),
        )
        names = [ref.name for ref in anyio.run(client.list_calendars)]
    except Exception:
        return

    report = stale_report(stale_scratch_calendars(names))
    if not report:
        return
    reporter = request.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\n{report}\n", yellow=True, bold=True)
    else:  # pragma: no cover -- only under -p no:terminal
        print(report)
