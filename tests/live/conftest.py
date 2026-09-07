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

import pytest

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

    yield

    # Stamped after the run, not before: what matters is when the account last
    # stopped being hit, and a run that dies half way still hit it.
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.touch()
    except OSError:
        # Not being able to remember is not a reason to fail a passing suite.
        pass
