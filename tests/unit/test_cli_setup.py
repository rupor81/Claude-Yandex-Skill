"""`yandex-mcp setup calendar` end to end, with the prompts stubbed out.

The command is the only place an operator hands over an app password, so what it
stores, what it refuses, and what it prints are all asserted here.
"""

from __future__ import annotations

import getpass

import pytest
from yandex_core.config import DEFAULT_CALDAV_URL, load_profile
from yandex_core.credentials import get_secret
from yandex_mcp_cli.main import main

SECRET = "hunter2-app-password"
LOGIN = "me@yandex.ru"


@pytest.fixture
def answers(monkeypatch):
    """Answer the hidden password prompt without a terminal."""

    def enter(prompt=""):
        return SECRET

    monkeypatch.setattr(getpass, "getpass", enter)


def test_setup_stores_what_it_was_given(answers, capsys):
    code = main(["setup", "calendar", "--login", LOGIN])
    assert code == 0

    profile = load_profile("default")
    assert profile.login == LOGIN
    assert profile.caldav_url == DEFAULT_CALDAV_URL
    assert get_secret("calendar", "default") == SECRET

    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_a_named_profile_and_url_round_trip(answers, monkeypatch):
    code = main(
        [
            "setup",
            "calendar",
            "--profile",
            "work",
            "--login",
            "me@company.ru",
            "--caldav-url",
            "https://caldav.example.test",
        ]
    )
    assert code == 0

    profile = load_profile("work")
    assert profile.name == "work"
    assert profile.login == "me@company.ru"
    assert profile.caldav_url == "https://caldav.example.test"
    assert get_secret("calendar", "work") == SECRET


def test_an_empty_login_is_refused_with_exit_code_two(answers, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "   ")
    assert main(["setup", "calendar"]) == 2
    assert "login is required" in capsys.readouterr().err


def test_an_empty_password_is_refused_with_exit_code_two(monkeypatch, capsys):
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "  ")
    assert main(["setup", "calendar", "--login", LOGIN]) == 2

    captured = capsys.readouterr()
    assert "Nothing was stored" in captured.err
    from yandex_core.errors import AuthError

    with pytest.raises(AuthError):
        get_secret("calendar", "default")


def test_the_password_never_reaches_either_stream(answers, capsys):
    main(["setup", "calendar", "--login", LOGIN])
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "stored in" in captured.out


def test_a_failing_profile_write_leaves_no_orphaned_credential(answers, monkeypatch):
    """A secret nobody can name is a secret nobody will ever clean up."""
    from yandex_core.errors import AuthError, ProtocolError
    from yandex_mcp_cli import main as cli

    def refuse(profile, **kwargs):
        raise ProtocolError("config directory is read-only")

    monkeypatch.setattr(cli, "write_profile", refuse)

    assert main(["setup", "calendar", "--login", LOGIN]) == 1
    with pytest.raises(AuthError):
        get_secret("calendar", "default")


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_an_interrupted_prompt_exits_cleanly(monkeypatch, capsys, interruption):
    def interrupt(prompt=""):
        raise interruption()

    monkeypatch.setattr(getpass, "getpass", interrupt)
    assert main(["setup", "calendar", "--login", LOGIN]) == 130
    assert "Cancelled" in capsys.readouterr().err
