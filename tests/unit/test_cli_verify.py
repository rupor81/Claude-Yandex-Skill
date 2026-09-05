"""`yandex-mcp verify`, one test per row of the story's I/O matrix.

The calendar connector is replaced by a fake client class so that every row --
including the ones that only exist because a real Yandex account misbehaves --
is reachable without a network.
"""

from __future__ import annotations

import pytest
from yandex_core.config import Profile, config_path, write_profile
from yandex_core.credentials import REDACTED, store_secret
from yandex_core.errors import (
    AuthError,
    Conflict,
    NotFound,
    PolicyError,
    ProtocolError,
    RateLimited,
    TransportError,
)
from yandex_mcp_cli import verify as verify_module
from yandex_mcp_cli.main import main

SECRET = "hunter2-app-password"
LOGIN = "me@yandex.ru"

#: Deliberately not the default: a hardcoded endpoint in the production code
#: would then still satisfy the assertion that the profile's URL is what is used.
CALDAV_URL = "https://caldav.example.invalid/dav-for-this-test/"

#: Every connector package the CLI must not import at module level, including the
#: two this epic has not built yet -- adding one must not silently drop the guard.
CONNECTOR_PACKAGES = (
    "yandex_calendar_mcp",
    "yandex_mail_mcp",
    "yandex_disk_mcp",
)


class FakeCalendar:
    def __init__(self, name: str) -> None:
        self.name = name


def fake_client_class(*, calendars=None, raises=None, sleep=None):
    """A stand-in for ``CalDAVCalendarClient`` that answers or fails on demand.

    The signature is the real one exactly -- no ``**kwargs`` -- so a change to
    what verify passes fails here instead of being swallowed.
    """
    captured = {}

    class FakeClient:
        def __init__(self, *, url: str, username: str, password: str):
            captured["url"] = url
            captured["username"] = username
            captured["password"] = password

        async def list_calendars(self):
            if sleep is not None:
                import asyncio

                await asyncio.sleep(sleep)
            if raises is not None:
                raise raises
            return list(calendars or [])

    FakeClient.captured = captured
    return FakeClient


@pytest.fixture
def install_client(monkeypatch):
    """Install a fake calendar client class in place of the lazy import."""

    def install(**kwargs):
        client_class = fake_client_class(**kwargs)
        monkeypatch.setattr(
            verify_module, "_load_calendar_client_class", lambda: client_class
        )
        return client_class

    return install


@pytest.fixture
def configured():
    """A profile plus a stored app password -- the "set up already" state."""
    write_profile(Profile(name="default", login=LOGIN, caldav_url=CALDAV_URL))
    store_secret("calendar", "default", SECRET)


def run(capsys, argv=("verify",)):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- matrix rows -------------------------------------------------------------


def test_reachable_reports_the_calendar_count_and_exits_zero(
    configured, install_client, capsys
):
    install_client(calendars=[FakeCalendar("Home"), FakeCalendar("Work")])

    code, out, _ = run(capsys)

    assert code == 0
    assert "calendar" in out
    assert "reachable" in out
    assert "2 calendars" in out


def test_one_calendar_is_reported_in_the_singular(configured, install_client, capsys):
    install_client(calendars=[FakeCalendar("Home")])
    code, out, _ = run(capsys)
    assert code == 0
    assert "1 calendar." in out


def test_no_profile_at_all_is_unconfigured_not_a_failure(install_client, capsys):
    """No config file anywhere: everything is unconfigured, and that is fine."""
    install_client(calendars=[])

    code, out, _ = run(capsys)

    assert code == 0
    assert "unconfigured" in out
    assert "yandex-mcp setup calendar" in out
    assert "failed" not in out


def test_a_profile_with_no_credential_is_unconfigured(install_client, capsys):
    write_profile(Profile(name="default", login=LOGIN))
    install_client(calendars=[])

    code, out, _ = run(capsys)

    assert code == 0
    assert "unconfigured" in out
    assert "yandex-mcp setup calendar" in out


def test_a_rejected_password_names_the_credential(configured, install_client, capsys):
    install_client(raises=AuthError("Yandex rejected the calendar app password."))

    code, out, _ = run(capsys)

    assert code == 1
    assert "failed" in out
    assert "credential" in out


def test_an_organisation_policy_is_not_reported_as_a_bad_password(
    configured, install_client, capsys
):
    install_client(
        raises=PolicyError(
            "Yandex refused the connection with 403. On Yandex 360 this means "
            "organisation policy has disabled app passwords."
        )
    )

    code, out, _ = run(capsys)

    assert code == 1
    assert "organisation policy" in out
    assert "credential:" not in out


def test_an_undecidable_cause_says_so_rather_than_guessing(
    configured, install_client, capsys
):
    """The client's own "cannot tell 401 from 403" message reaches the operator."""
    install_client(
        raises=AuthError(
            "Yandex refused the connection and did not say whether the cause was "
            "a rejected calendar app password (401) or organisation policy (403)."
        )
    )

    code, out, _ = run(capsys)

    assert code == 1
    assert "did not say whether" in out


def test_an_unreachable_host_names_transport_and_still_reports_everything(
    configured, install_client, capsys
):
    install_client(raises=TransportError("Could not reach https://caldav.yandex.ru."))

    code, out, _ = run(capsys)

    assert code == 1
    assert "transport" in out
    assert "mail" in out and "disk" in out


def test_a_rate_limit_is_named_as_such(configured, install_client, capsys):
    install_client(raises=RateLimited("Yandex is rate limiting this account."))
    code, out, _ = run(capsys)
    assert code == 1
    assert "rate limit" in out


def test_a_protocol_failure_is_named_as_such(configured, install_client, capsys):
    install_client(raises=ProtocolError("Yandex answered in a way we cannot honour."))
    code, out, _ = run(capsys)
    assert code == 1
    assert "protocol" in out


def test_mail_and_disk_read_as_not_yet_built_and_do_not_fail_the_run(
    configured, install_client, capsys
):
    install_client(calendars=[])

    code, out, _ = run(capsys)

    assert code == 0
    lines = {line.split()[0]: line for line in out.splitlines() if line.strip()}
    assert "not yet built" in lines["mail"]
    assert "not yet built" in lines["disk"]
    assert "unconfigured" not in lines["mail"]


def test_a_missing_connector_is_reported_and_the_others_still_are(
    configured, monkeypatch, capsys
):
    def explode():
        raise ImportError("No module named 'yandex_calendar_mcp'")

    monkeypatch.setattr(verify_module, "_load_calendar_client_class", explode)

    code, out, _ = run(capsys)

    assert code == 1
    assert "connector unavailable" in out
    assert "mail" in out and "disk" in out


# -- properties that hold in every state -------------------------------------


def test_no_secret_appears_in_any_state(configured, install_client, capsys):
    """The password is stored and used, and must never be printed.

    The last case is the one that matters: a layer below us can put the
    credential into its own message -- a transport error quoting the URL it
    dialled with the password embedded in it -- so rendering an exception
    verbatim is not enough.
    """
    leaky = TransportError(
        f"Could not reach https://me%40yandex.ru:{SECRET}@caldav.yandex.ru/."
    )
    for kwargs in (
        {"calendars": [FakeCalendar("Home")]},
        {"raises": AuthError("rejected")},
        {"raises": TransportError("unreachable")},
        {"raises": RuntimeError("something odd")},
        {"raises": leaky},
    ):
        install_client(**kwargs)
        _, out, err = run(capsys)
        assert SECRET not in out + err

    # ...and the leaky line is still a usable report, not a blanked-out one.
    install_client(raises=leaky)
    _, out, _ = run(capsys)
    assert REDACTED in out
    assert "caldav.yandex.ru" in out


def test_the_stored_credential_and_endpoint_are_what_reach_the_client(
    configured, install_client, capsys
):
    client_class = install_client(calendars=[])
    run(capsys)
    assert client_class.captured["password"] == SECRET
    assert client_class.captured["username"] == LOGIN
    # The profile's endpoint, not a constant: verifying the wrong host would
    # report a healthy account that the server will never talk to.
    assert client_class.captured["url"] == CALDAV_URL


def test_an_empty_exception_message_still_explains_itself(
    configured, install_client, capsys
):
    install_client(raises=RuntimeError())
    code, out, _ = run(capsys)
    assert code == 1
    assert "RuntimeError" in out


def test_a_multi_line_detail_stays_on_one_line(configured, install_client, capsys):
    install_client(raises=ProtocolError("Yandex answered\n  with\n\nnonsense."))
    code, out, _ = run(capsys)
    assert code == 1
    assert "Yandex answered with nonsense." in out
    assert len([line for line in out.splitlines() if line.strip()]) == 3


def test_an_unexpected_exception_is_still_a_reported_line(
    configured, install_client, capsys
):
    """Anything outside the taxonomy is reported, not raised out of the command."""
    install_client(raises=RuntimeError("something odd"))

    code, out, _ = run(capsys)

    assert code == 1
    assert "unexpected" in out


def test_a_check_that_raises_cannot_silence_the_services_after_it(monkeypatch):
    def broken(profile_name=None):
        raise RuntimeError("the check itself is buggy")

    monkeypatch.setattr(verify_module, "CHECKS", (broken, verify_module.check_mail))

    results = verify_module.run_checks(None)

    assert len(results) == 2
    assert results[0].state is verify_module.State.FAILED
    assert results[1].state is verify_module.State.NOT_YET_BUILT


def test_a_named_profile_is_the_one_verified(install_client, capsys):
    write_profile(Profile(name="work", login="me@company.ru"))
    store_secret("calendar", "work", SECRET)
    install_client(calendars=[])

    code, out, _ = run(capsys, argv=("verify", "--profile", "work"))

    assert code == 0
    assert "'work'" in out


def _snapshot(directory):
    """Every file under a directory: name, bytes, and modification time."""
    return {
        str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_verify_writes_nothing(configured, install_client, capsys, isolated_config):
    """Read-only, on the path that actually builds a client and calls out.

    The unconfigured path returns before anything could be written, so proving
    read-onlyness there proves nothing: this runs against a real profile, once
    reachable and once failing, and compares the whole config tree either side.
    """
    install_client(calendars=[FakeCalendar("Home")])
    before = _snapshot(isolated_config)
    assert before, "nothing was configured; this test would pass on an empty tree"

    assert run(capsys)[0] == 0
    assert _snapshot(isolated_config) == before

    install_client(raises=TransportError("Could not reach the host."))
    assert run(capsys)[0] == 1
    assert _snapshot(isolated_config) == before


def test_verify_never_prompts(configured, install_client, monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise AssertionError("verify must not prompt")

    monkeypatch.setattr("builtins.input", refuse)
    import getpass

    monkeypatch.setattr(getpass, "getpass", refuse)
    install_client(calendars=[])

    assert run(capsys)[0] == 0


# -- the lazy import is a structural guarantee, so it is asserted -------------


def test_the_cli_does_not_import_the_connector_at_module_level():
    """A top-level import would make a broken connector break the whole command."""
    import ast
    from pathlib import Path

    import yandex_mcp_cli

    package_root = Path(yandex_mcp_cli.__file__).parent
    modules = sorted(package_root.rglob("*.py"))
    assert modules, "no CLI modules found; this test would pass on nothing"

    def is_type_checking(node):
        """`if TYPE_CHECKING:` never runs, so an import inside it is free."""
        if not isinstance(node, ast.If):
            return False
        test = node.test
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"

    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Only statements directly in the module body run at import time; the
        # same import inside a function body is exactly what this story wants.
        for node in ast.walk(ast.Module(body=tree.body, type_ignores=[])):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.body = []
            elif is_type_checking(node):
                node.body = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            offenders = [
                name for name in names if name.split(".")[0] in CONNECTOR_PACKAGES
            ]
            assert not offenders, (
                f"{path.name} imports {offenders} at module level"
            )


def test_verify_still_reports_when_the_connector_is_genuinely_absent(tmp_path):
    """The real lazy import, in a process where the calendar package is missing."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import yandex_core
    import yandex_mcp_cli

    program = """
import sys

class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "yandex_calendar_mcp" or name.startswith("yandex_calendar_mcp."):
            raise ImportError("the calendar connector is deliberately unavailable")
        return None

sys.meta_path.insert(0, Blocker())
from yandex_mcp_cli.main import main
raise SystemExit(main(["verify"]))
"""
    # The suite's isolation is process-local: inheriting the developer's
    # environment wholesale would let a real exported password, or a real
    # profile, take part in this run.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("YANDEX_MCP_")
    }
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(Path(yandex_core.__file__).parent.parent),
            str(Path(yandex_mcp_cli.__file__).parent.parent),
        ]
    )
    environment["YANDEX_MCP_CONFIG_DIR"] = str(tmp_path / "config")
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert result.returncode == 1, result.stderr
    assert "connector unavailable" in result.stdout
    assert "mail" in result.stdout and "disk" in result.stdout


# -- the real lazy import, exercised in its success state --------------------


def test_the_lazy_import_actually_resolves_the_real_client():
    """Only the failure of this import was ever covered.

    A mistyped module path inside ``_load_calendar_client_class`` would satisfy
    every other test in this file -- they all replace it -- while breaking every
    real run of the command.
    """
    from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient

    assert verify_module._load_calendar_client_class() is CalDAVCalendarClient


def test_the_real_client_accepts_what_verify_passes_it():
    """The fake cannot vouch for the real constructor, so the real one is asked.

    Verify builds the client by keyword; a rename or a new required argument in
    ``CalDAVCalendarClient`` would otherwise only show up in production.
    """
    import inspect

    from yandex_calendar_mcp.client.caldav_client import CalDAVCalendarClient

    signature = inspect.signature(CalDAVCalendarClient)
    signature.bind(url="https://example.invalid", username="u", password="p")

    assert inspect.iscoroutinefunction(CalDAVCalendarClient.list_calendars)


# -- absent is not the same thing as broken ----------------------------------


def _write_config(text: str):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_corrupt_config_is_a_failure_not_an_unconfigured_machine(
    install_client, capsys
):
    """A file that exists and cannot be parsed is broken, and setup would eat it."""
    install_client(calendars=[])
    _write_config("this is not = = toml\n")

    code, out, _ = run(capsys)

    assert code == 1
    calendar_line = _line_for("calendar", out)
    assert "failed" in calendar_line
    assert "unconfigured" not in calendar_line
    assert "yandex-mcp setup" not in calendar_line


def test_a_profile_without_a_login_is_a_failure_not_an_unconfigured_machine(
    install_client, capsys
):
    install_client(calendars=[])
    _write_config(
        'default_profile = "default"\n\n[profiles.default]\n'
        'caldav_url = "https://example.invalid"\n'
    )

    code, out, _ = run(capsys)

    assert code == 1
    calendar_line = _line_for("calendar", out)
    assert "failed" in calendar_line
    assert "login" in calendar_line
    assert "yandex-mcp setup" not in calendar_line


def test_a_named_profile_that_does_not_exist_is_a_failure(install_client, capsys):
    """Naming a profile that is not there is an operator error, not an empty box."""
    write_profile(Profile(name="personal", login=LOGIN))
    install_client(calendars=[])

    code, out, _ = run(capsys, argv=("verify", "--profile", "nope"))

    assert code == 1
    calendar_line = _line_for("calendar", out)
    assert "failed" in calendar_line
    assert "unconfigured" not in calendar_line
    assert "personal" in calendar_line
    assert "yandex-mcp setup" not in calendar_line


# -- the hint names what was checked -----------------------------------------


def test_the_setup_hint_names_the_service_and_a_non_default_profile():
    assert verify_module.setup_hint("calendar", "default") == (
        "Run `yandex-mcp setup calendar` to configure it."
    )
    assert verify_module.setup_hint("mail", "work") == (
        "Run `yandex-mcp setup mail --profile work` to configure it."
    )
    assert verify_module.setup_hint("disk", None) == (
        "Run `yandex-mcp setup disk` to configure it."
    )


def test_an_unconfigured_named_profile_is_told_to_set_up_that_profile(
    install_client, capsys
):
    """Without ``--profile`` the hint would tell the operator to write another one."""
    write_profile(Profile(name="work", login="me@company.ru"))
    install_client(calendars=[])

    code, out, _ = run(capsys, argv=("verify", "--profile", "work"))

    assert code == 0
    assert "unconfigured" in out
    assert "yandex-mcp setup calendar --profile work" in _line_for("calendar", out)


def test_a_world_readable_credential_file_is_a_failure_without_a_setup_hint(
    configured, install_client, capsys
):
    """The credential is fine; its permissions are not, and setup would replace it."""
    from yandex_core.config import config_dir

    install_client(calendars=[])
    (config_dir() / "credentials" / "calendar.default").chmod(0o644)

    code, out, _ = run(capsys)

    calendar_line = _line_for("calendar", out)
    assert code == 1
    assert "failed" in calendar_line
    assert "chmod 600" in calendar_line
    assert "yandex-mcp setup" not in calendar_line


# -- a call that never answers -----------------------------------------------


def test_a_host_that_never_answers_times_out_rather_than_hanging(
    configured, install_client, monkeypatch, capsys
):
    monkeypatch.setattr(verify_module, "CHECK_TIMEOUT_SECONDS", 0.05)
    install_client(sleep=30, calendars=[])

    code, out, _ = run(capsys)

    calendar_line = _line_for("calendar", out)
    assert code == 1
    assert "timed out" in calendar_line
    assert "0.05 seconds" in calendar_line
    # The deadline is one service's, not the command's: the rest still report.
    assert "mail" in out and "disk" in out


# -- the remaining taxonomy rows ---------------------------------------------


def test_a_not_found_is_named_as_such(configured, install_client, capsys):
    install_client(raises=NotFound("The calendar home set is not there."))
    code, out, _ = run(capsys)
    assert code == 1
    assert "not found:" in out


def test_a_conflict_is_named_as_such(configured, install_client, capsys):
    install_client(raises=Conflict("The collection changed underneath the request."))
    code, out, _ = run(capsys)
    assert code == 1
    assert "conflict:" in out


def test_one_failure_beside_an_unconfigured_service_still_exits_one(
    install_client, monkeypatch, capsys
):
    """The mixed state the exit rule exists for: failed + unconfigured + two stubs."""

    def check_notes(profile_name=None):
        return verify_module.ServiceResult(
            "notes",
            verify_module.State.FAILED,
            "Notes refused the connection.",
            cause="transport",
            profile=profile_name or "default",
        )

    install_client(calendars=[])
    monkeypatch.setattr(
        verify_module,
        "CHECKS",
        (check_notes, verify_module.check_calendar, verify_module.check_mail,
         verify_module.check_disk),
    )

    code, out, _ = run(capsys)

    assert code == 1
    assert "failed" in _line_for("notes", out)
    assert "unconfigured" in _line_for("calendar", out)
    assert "not yet built" in _line_for("mail", out)
    assert "not yet built" in _line_for("disk", out)


# -- every line says which profile it is about -------------------------------


def test_every_line_names_the_profile(install_client, capsys):
    write_profile(Profile(name="work", login="me@company.ru"))
    install_client(calendars=[])

    _, out, _ = run(capsys, argv=("verify", "--profile", "work"))

    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 3
    assert all("'work'" in line for line in lines)


def test_a_closed_pipe_is_not_reported_as_a_service_failure(
    configured, install_client, monkeypatch
):
    """`yandex-mcp verify | head -1` must not look like a broken calendar."""
    import sys

    class ClosedPipe:
        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    install_client(calendars=[])
    monkeypatch.setattr(sys, "stdout", ClosedPipe())

    assert main(["verify"]) == 0


def _line_for(service: str, out: str) -> str:
    for line in out.splitlines():
        if line.split(maxsplit=1)[:1] == [service]:
            return line
    raise AssertionError(f"no line for {service!r} in:\n{out}")
