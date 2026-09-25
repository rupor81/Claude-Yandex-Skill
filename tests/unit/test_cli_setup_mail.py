"""`yandex-mcp setup mail`: the mailbox is connected with an app password.

The same shape as `setup calendar`, on purpose. Mail clients connect to Yandex
this way as a matter of course, and it needs no registered application -- which
the OAuth flow it replaced did. OAuth stays in the core for Disk, which has no
password route at all.
"""

from __future__ import annotations

import getpass

import pytest

from yandex_core.config import Profile, load_profile, write_profile
from yandex_core.credentials import CredentialNotFound, get_secret
from yandex_mcp_cli import main as cli
from yandex_mcp_cli.main import main

SECRET = "mail-app-password-abcd"
LOGIN = "me@yandex.ru"


@pytest.fixture
def answers(monkeypatch):
    given = {"password": SECRET, "login": LOGIN}
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": given["password"])
    monkeypatch.setattr("builtins.input", lambda prompt="": given["login"])
    return given


def test_setup_stores_the_mail_password_under_its_own_name(answers, capsys):
    """Its own slot, not the calendar's: Yandex scopes app passwords by type,
    and one made for the calendar is refused by IMAP -- measured."""
    code = main(["setup", "mail", "--login", LOGIN])

    assert code == 0
    assert get_secret("mail", "default") == SECRET
    with pytest.raises(CredentialNotFound):
        get_secret("calendar", "default")
    assert SECRET not in capsys.readouterr().out, "the password was printed"


def test_setting_up_mail_leaves_a_working_calendar_alone(answers):
    write_profile(Profile(name="default", login=LOGIN))
    from yandex_core.credentials import store_secret

    store_secret("calendar", "default", "calendar-password")

    assert main(["setup", "mail"]) == 0

    assert get_secret("calendar", "default") == "calendar-password"
    assert get_secret("mail", "default") == SECRET


def test_an_existing_profile_s_login_is_used_without_asking(answers, monkeypatch):
    write_profile(Profile(name="default", login=LOGIN))
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("asked for a login it has")
    )

    assert main(["setup", "mail"]) == 0


def test_the_explanation_names_the_two_things_to_do_in_yandex(answers, capsys):
    """Both halves of the one error Yandex gives: the password's type, and the
    IMAP switch. Naming one sends the operator to fix the other."""
    main(["setup", "mail", "--login", LOGIN])

    explained = capsys.readouterr().out
    assert "id.yandex.ru" in explained
    assert "Почта" in explained or "Mail" in explained
    # Where the switch is, not merely the word IMAP: the text mentions IMAP in
    # other sentences too, and a check on the word alone passed with the step
    # itself deleted.
    assert "Mail programs" in explained, "the IMAP switch's location is not named"


def test_an_empty_password_stores_nothing(answers, capsys):
    answers["password"] = "   "

    assert main(["setup", "mail", "--login", LOGIN]) != 0

    with pytest.raises(CredentialNotFound):
        get_secret("mail", "default")


def test_no_password_is_ever_a_command_line_argument():
    text = cli.build_parser().format_help()
    for forbidden in ("--password", "--secret", "--token"):
        assert forbidden not in text


def test_there_is_one_way_to_connect_mail_not_two():
    """The OAuth `login mail` it replaced is gone, not kept alongside."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["login", "mail"])


def test_the_profile_keeps_its_login(answers):
    main(["setup", "mail", "--login", LOGIN])

    assert load_profile("default").login == LOGIN


def test_setting_up_mail_keeps_everything_else_the_profile_says(answers):
    """A profile can carry a non-default CalDAV address, or hosts fronted by a
    Yandex 360 domain. Rebuilding it from the login alone would reset them to
    defaults, and the calendar would break with nothing saying why."""
    write_profile(
        Profile(
            name="default",
            login=LOGIN,
            caldav_url="https://caldav.example.ru",
            imap_host="imap.example.ru",
        )
    )

    assert main(["setup", "mail"]) == 0

    kept = load_profile("default")
    assert kept.caldav_url == "https://caldav.example.ru"
    assert kept.imap_host == "imap.example.ru"
