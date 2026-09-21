"""`yandex-mcp login mail`: the one place an operator authorises the mailbox.

What it prints, what it refuses, and what it stores are all asserted, because a
login that half-succeeds leaves a connector that looks configured and is not.
"""

from __future__ import annotations

import getpass

import pytest

from yandex_core.config import Profile, load_profile, write_profile
from yandex_core.credentials import CredentialNotFound, get_secret
from yandex_mcp_cli import main as cli
from yandex_mcp_cli.main import main

CLIENT_ID = "0123456789abcdef0123456789abcdef"
LOGIN = "me@yandex.ru"
CODE = "1234567"


@pytest.fixture
def paste(monkeypatch):
    """Answer the hidden code prompt without a terminal."""
    given = {"code": CODE}

    def enter(prompt=""):
        return given["code"]

    monkeypatch.setattr(getpass, "getpass", enter)
    return given


@pytest.fixture
def typed(monkeypatch):
    """Answer the visible ClientID prompt without a terminal.

    Defaults to typing nothing, which is the "I have not registered one" case.
    """
    given = {"client_id": ""}
    monkeypatch.setattr("builtins.input", lambda prompt="": given["client_id"])
    return given


@pytest.fixture
def token_endpoint(monkeypatch):
    """Stand in for Yandex's token endpoint, recording what it was sent."""
    seen: list = []
    answer = {
        "status": 200,
        "payload": {
            "access_token": "ACCESS-abc",
            "refresh_token": "REFRESH-xyz",
            "expires_in": 3600,
        },
    }

    def post(url, data):
        seen.append({"url": url, "data": dict(data)})
        return answer["status"], answer["payload"]

    monkeypatch.setattr(cli, "oauth_post", post)
    return seen, answer


def _profile(**kwargs):
    write_profile(Profile(name="default", login=LOGIN, **kwargs))


def test_a_login_stores_the_refresh_token_and_nothing_else(
    paste, token_endpoint, capsys
):
    _profile(oauth_client_id=CLIENT_ID)

    assert main(["login", "mail"]) == 0

    assert get_secret("mail", "default") == "REFRESH-xyz"
    output = capsys.readouterr().out
    assert "REFRESH-xyz" not in output, "the refresh token was printed"
    assert "ACCESS-abc" not in output, "the access token was printed"


def test_the_operator_is_shown_the_url_and_both_scopes_before_approving(
    paste, token_endpoint, capsys
):
    """They are granting these rights. An unreadable ask is not consent."""
    _profile(oauth_client_id=CLIENT_ID)

    main(["login", "mail"])

    output = capsys.readouterr().out
    assert "https://oauth.yandex.ru/authorize" in output
    assert "mail%3Aimap_full" in output or "mail:imap_full" in output
    assert "mail%3Asmtp" in output or "mail:smtp" in output


def test_the_exchange_sends_the_verifier_that_matches_the_url_it_printed(
    paste, token_endpoint, capsys
):
    """A challenge for one verifier and an exchange with another fails obscurely."""
    import urllib.parse

    _profile(oauth_client_id=CLIENT_ID)
    main(["login", "mail"])
    seen, _ = token_endpoint

    printed = [
        word
        for word in capsys.readouterr().out.split()
        if word.startswith("https://oauth.yandex.ru/authorize")
    ]
    assert printed, "no authorization URL was printed"
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(printed[0]).query))

    import base64
    import hashlib

    sent = seen[0]["data"]["code_verifier"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(sent.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert query["code_challenge"] == expected


def test_no_advice_this_command_gives_needs_a_placeholder_substituted():
    """Help text is copied straight into a shell. `<` and `>` break it there."""
    from yandex_mcp_cli.main import build_parser

    parser = build_parser()
    for source in (
        parser.format_help(),
        cli.LOGIN_MAIL_EXPLANATION,
        cli.REGISTER_APPLICATION_EXPLANATION,
    ):
        for line in source.splitlines():
            if "yandex-mcp" in line:
                assert "<" not in line and ">" not in line, line


def test_no_secret_is_ever_a_command_line_argument():
    """NFR7. A code on the command line is in the shell history forever."""
    from yandex_mcp_cli.main import build_parser

    parser = build_parser()
    text = parser.format_help()
    for forbidden in ("--code", "--token", "--secret", "--client-secret"):
        assert forbidden not in text


def test_a_login_with_no_application_anywhere_refuses_without_touching_the_network(
    paste, typed, token_endpoint, capsys
):
    """Nothing given on the command line, nothing in the profile, nothing typed."""
    _profile()

    assert main(["login", "mail"]) != 0

    seen, _ = token_endpoint
    assert seen == [], "the token endpoint was called with no client_id"
    message = capsys.readouterr().err
    assert "oauth.yandex.ru" in message, "the operator is not told where to register"
    assert "mail:imap_full" in message
    # The command to run next, with nothing in it for a shell to interpret.
    # `start_login` refuses an empty client_id too and names the same page, so
    # without this the command's own guard is indistinguishable from that one.
    assert "yandex-mcp login mail" in message
    assert "<" not in message, "the advice contains shell redirection characters"


def test_the_client_id_is_asked_for_rather_than_demanded_on_the_command_line(
    paste, typed, token_endpoint, capsys
):
    """A ClientID pasted after a flag invites a `<ClientID>` placeholder going in
    verbatim -- where zsh reads `<` as a redirection and the command never runs."""
    _profile()
    typed["client_id"] = CLIENT_ID

    assert main(["login", "mail"]) == 0

    seen, _ = token_endpoint
    assert seen[0]["data"]["client_id"] == CLIENT_ID
    assert load_profile("default").oauth_client_id == CLIENT_ID
    explained = capsys.readouterr().out
    assert "oauth.yandex.ru" in explained
    # Asserted against the explanation itself, not the captured output: the
    # authorization URL carries `redirect_uri=...verification_code` in its query
    # string, so an output-wide search passes even when the instructions say
    # nothing about which kind of application to register.
    assert "verification_code" in cli.REGISTER_APPLICATION_EXPLANATION, (
        "the operator is not told which application kind has the right redirect"
    )
    assert cli.REGISTER_APPLICATION_EXPLANATION in explained


def test_a_profile_that_already_has_one_is_not_asked_again(
    paste, typed, token_endpoint, capsys
):
    _profile(oauth_client_id=CLIENT_ID)
    typed["client_id"] = "SHOULD-NOT-BE-READ"

    assert main(["login", "mail"]) == 0

    seen, _ = token_endpoint
    assert seen[0]["data"]["client_id"] == CLIENT_ID


def test_a_client_id_given_on_the_command_line_is_remembered(
    paste, token_endpoint, capsys
):
    """It is public by design, so it is an argument -- and it should stick."""
    _profile()

    assert main(["login", "mail", "--client-id", CLIENT_ID]) == 0

    assert load_profile("default").oauth_client_id == CLIENT_ID


def test_an_empty_paste_stores_nothing(paste, token_endpoint, capsys):
    _profile(oauth_client_id=CLIENT_ID)
    paste["code"] = "   "

    assert main(["login", "mail"]) != 0

    seen, _ = token_endpoint
    assert seen == []
    with pytest.raises(CredentialNotFound):
        get_secret("mail", "default")


def test_a_refused_exchange_stores_nothing_and_says_what_to_do(
    paste, token_endpoint, capsys
):
    _profile(oauth_client_id=CLIENT_ID)
    _, answer = token_endpoint
    answer["status"] = 400
    answer["payload"] = {"error": "invalid_grant", "error_description": "expired"}

    assert main(["login", "mail"]) != 0

    with pytest.raises(CredentialNotFound):
        get_secret("mail", "default")
    assert "again" in capsys.readouterr().err.lower()


def test_a_whole_pasted_url_is_accepted(paste, token_endpoint):
    """The browser puts the address on the clipboard as readily as the code."""
    _profile(oauth_client_id=CLIENT_ID)
    paste["code"] = f"https://oauth.yandex.ru/verification_code?code={CODE}"

    assert main(["login", "mail"]) == 0

    seen, _ = token_endpoint
    assert seen[0]["data"]["code"] == CODE


def test_logging_in_again_replaces_the_stored_authorisation(paste, token_endpoint):
    _profile(oauth_client_id=CLIENT_ID)
    main(["login", "mail"])
    _, answer = token_endpoint
    answer["payload"] = {"access_token": "ACCESS-2", "refresh_token": "REFRESH-2"}

    assert main(["login", "mail"]) == 0
    assert get_secret("mail", "default") == "REFRESH-2"


def test_the_registration_steps_are_not_explained_twice_in_one_run(
    paste, typed, token_endpoint, capsys
):
    """Two overlapping essays in one run is worse than one.

    The long version is the `--help` epilog; what runs at the prompt is what the
    operator needs while standing at it.
    """
    _profile()
    typed["client_id"] = CLIENT_ID

    main(["login", "mail"])

    output = capsys.readouterr().out
    assert output.count("Open https://oauth.yandex.ru and create an application") == 1
    assert output.count("`mail:imap_full` and `mail:smtp`") == 1
    assert cli.LOGIN_MAIL_EXPLANATION not in output, "the help epilog was printed too"


def test_the_address_is_shown_before_the_code_is_asked_for(
    paste, typed, token_endpoint, capsys, monkeypatch
):
    """Buffered stdout arriving after the prompt asks for a code from nowhere."""
    order: list[str] = []
    monkeypatch.setattr(
        getpass, "getpass", lambda prompt="": (order.append("prompt"), CODE)[1]
    )
    real_print = print

    def watched(*args, **kwargs):
        if args and "oauth.yandex.ru/authorize" in str(args[0]):
            order.append("url")
        real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", watched)
    _profile(oauth_client_id=CLIENT_ID)

    main(["login", "mail"])

    assert order[: order.index("prompt")].count("url") == 1, order
