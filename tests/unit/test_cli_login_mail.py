"""`yandex-mcp login mail`: sign in in the browser and come back.

The browser here is played by a fake that does what Yandex documents for the
Authorization Code flow -- send the browser to the registered redirect with a
`code` and the `state` it was given -- and it does so against the *real*
loopback listener on a real socket. A fake that skipped the listener would only
prove the command agrees with the fake.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import threading
import urllib.parse
import urllib.request

import pytest

from yandex_core.config import Profile, load_profile, write_profile
from yandex_core.credentials import CredentialNotFound, get_secret
from yandex_mcp_cli import main as cli
from yandex_mcp_cli.main import main

CLIENT_ID = "0123456789abcdef0123456789abcdef"
LOGIN = "me@yandex.ru"
CODE = "AUTHCODE-7f3a9"  # distinctive: a substring search must mean something


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def port():
    return _free_port()


@pytest.fixture(autouse=True)
def _impatient(monkeypatch):
    """A broken flow fails in seconds, not in the five minutes an operator gets.

    A mutation that bound the listener to every interface was caught -- after
    one test sat out the full real timeout. That is a suite nobody runs twice.
    """
    monkeypatch.setattr(cli, "SIGN_IN_TIMEOUT_SECONDS", 10)


@pytest.fixture
def browser(monkeypatch):
    """Yandex's side of the redirect, as documented.

    `behaviour` decides what the browser comes back with: the code (the default),
    the operator declining, a forged state, or never coming back at all.
    """
    state = {"behaviour": "approve", "opened": []}

    def open_browser(url: str) -> bool:
        state["opened"].append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        redirect = query["redirect_uri"].replace("localhost", "127.0.0.1")
        behaviour = state["behaviour"]
        if behaviour == "never":
            return True
        if behaviour == "approve":
            back = {"code": CODE, "state": query["state"]}
        elif behaviour == "decline":
            back = {"error": "access_denied", "state": query["state"]}
        elif behaviour == "forged":
            back = {"code": "FORGED", "state": "not-the-one-we-sent"}
        else:  # pragma: no cover
            raise AssertionError(behaviour)

        def arrive():
            urllib.request.urlopen(
                f"{redirect}?{urllib.parse.urlencode(back)}", timeout=5
            ).read()

        threading.Thread(target=arrive, daemon=True).start()
        return True

    monkeypatch.setattr(cli, "open_browser", open_browser)
    return state


@pytest.fixture
def typed(monkeypatch):
    """Answer the visible ClientID prompt. Defaults to typing nothing."""
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


def _login(port, *extra):
    return main(["login", "mail", "--port", str(port), *extra])


# -- the flow the operator asked for --------------------------------------


def test_signing_in_in_the_browser_stores_the_refresh_token(
    port, browser, typed, token_endpoint, capsys
):
    _profile(oauth_client_id=CLIENT_ID)

    assert _login(port) == 0

    assert get_secret("mail", "default") == "REFRESH-xyz"
    output = capsys.readouterr().out
    assert "REFRESH-xyz" not in output, "the refresh token was printed"
    assert "ACCESS-abc" not in output, "the access token was printed"


def test_the_browser_is_opened_for_the_operator(port, browser, typed, token_endpoint):
    """Nothing is pasted by hand: the command opens the browser itself."""
    _profile(oauth_client_id=CLIENT_ID)

    _login(port)

    (url,) = browser["opened"]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith("https://oauth.yandex.ru/authorize")
    assert set(query["scope"].split()) == {"mail:imap_full", "mail:smtp"}
    assert query["redirect_uri"] == f"http://localhost:{port}/callback"


def test_the_address_is_printed_too_for_a_browser_that_does_not_open(
    port, browser, typed, token_endpoint, capsys
):
    _profile(oauth_client_id=CLIENT_ID)

    _login(port)

    (url,) = browser["opened"]
    assert url in capsys.readouterr().out


def test_the_exchange_names_the_verifier_and_the_redirect_the_url_used(
    port, browser, typed, token_endpoint
):
    """A challenge for one verifier and an exchange with another fails obscurely."""
    _profile(oauth_client_id=CLIENT_ID)
    _login(port)
    seen, _ = token_endpoint

    (url,) = browser["opened"]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    sent = seen[0]["data"]
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(sent["code_verifier"].encode()).digest()
        )
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == expected
    assert sent["redirect_uri"] == query["redirect_uri"]
    assert sent["code"] == CODE


# -- what comes back is checked before it is used ------------------------


def test_a_forged_callback_is_refused_and_nothing_is_exchanged(
    port, browser, typed, token_endpoint, capsys
):
    """With a real redirect, `state` is what stops somebody else's code landing here."""
    _profile(oauth_client_id=CLIENT_ID)
    browser["behaviour"] = "forged"

    assert _login(port) != 0

    seen, _ = token_endpoint
    assert seen == [], "a code from a forged callback was spent"
    with pytest.raises(CredentialNotFound):
        get_secret("mail", "default")
    assert "state" in capsys.readouterr().err.lower()


def test_declining_in_the_browser_stores_nothing_and_says_so(
    port, browser, typed, token_endpoint, capsys
):
    _profile(oauth_client_id=CLIENT_ID)
    browser["behaviour"] = "decline"

    assert _login(port) != 0

    seen, _ = token_endpoint
    assert seen == []
    with pytest.raises(CredentialNotFound):
        get_secret("mail", "default")
    assert "declined" in capsys.readouterr().err.lower()


def test_a_browser_that_never_comes_back_times_out_and_stores_nothing(
    port, browser, typed, token_endpoint, capsys, monkeypatch
):
    _profile(oauth_client_id=CLIENT_ID)
    browser["behaviour"] = "never"
    monkeypatch.setattr(cli, "SIGN_IN_TIMEOUT_SECONDS", 0.3)

    assert _login(port) != 0

    with pytest.raises(CredentialNotFound):
        get_secret("mail", "default")
    assert "nothing was stored" in capsys.readouterr().err.lower()


def test_a_busy_port_is_reported_before_the_browser_is_opened(
    browser, typed, token_endpoint, capsys
):
    """Sending the operator to sign in with nowhere to come back to wastes the sign-in."""
    _profile(oauth_client_id=CLIENT_ID)
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        busy = squatter.getsockname()[1]

        assert _login(busy) != 0

    assert browser["opened"] == [], "the browser was opened with nowhere to return to"
    assert str(busy) in capsys.readouterr().err


def test_a_refused_exchange_stores_nothing_and_says_what_to_do(
    port, browser, typed, token_endpoint, capsys
):
    _profile(oauth_client_id=CLIENT_ID)
    _, answer = token_endpoint
    answer["status"] = 400
    answer["payload"] = {"error": "invalid_grant", "error_description": "expired"}

    assert _login(port) != 0

    with pytest.raises(CredentialNotFound):
        get_secret("mail", "default")
    assert "again" in capsys.readouterr().err.lower()


# -- the one-time registration --------------------------------------------


def test_with_no_application_anywhere_it_refuses_before_opening_anything(
    port, browser, typed, token_endpoint, capsys
):
    _profile()

    assert _login(port) != 0

    seen, _ = token_endpoint
    assert seen == []
    assert browser["opened"] == []
    message = capsys.readouterr().err
    assert "oauth.yandex.ru" in message
    assert f"http://localhost:{port}/callback" in message
    assert "yandex-mcp login mail" in message
    assert "<" not in message, "the advice contains shell redirection characters"


def test_the_registration_steps_name_the_platform_and_the_exact_redirect(
    port, browser, typed, token_endpoint, capsys
):
    """The one registration mistake that silently brings back the paste flow is
    choosing the API-access kind, whose redirect Yandex fixes. So it is named."""
    _profile()
    typed["client_id"] = CLIENT_ID

    assert _login(port) == 0

    explained = capsys.readouterr().out
    assert "Web services" in explained
    assert f"http://localhost:{port}/callback" in explained
    assert "`mail:imap_full` and `mail:smtp`" in explained


def test_the_client_id_is_asked_for_once_and_remembered(
    port, browser, typed, token_endpoint
):
    _profile()
    typed["client_id"] = CLIENT_ID

    assert _login(port) == 0

    assert load_profile("default").oauth_client_id == CLIENT_ID
    seen, _ = token_endpoint
    assert seen[0]["data"]["client_id"] == CLIENT_ID


def test_a_profile_that_already_has_one_is_not_asked_again(
    port, browser, typed, token_endpoint
):
    _profile(oauth_client_id=CLIENT_ID)
    typed["client_id"] = "SHOULD-NOT-BE-READ"

    assert _login(port) == 0

    seen, _ = token_endpoint
    assert seen[0]["data"]["client_id"] == CLIENT_ID


def test_signing_in_again_replaces_the_stored_authorisation(
    port, browser, typed, token_endpoint
):
    _profile(oauth_client_id=CLIENT_ID)
    _login(port)
    _, answer = token_endpoint
    answer["payload"] = {"access_token": "ACCESS-2", "refresh_token": "REFRESH-2"}

    assert _login(port) == 0
    assert get_secret("mail", "default") == "REFRESH-2"


# -- nothing a shell or a history file should see ------------------------


def test_no_secret_is_ever_a_command_line_argument():
    """NFR7. A code on the command line is in the shell history forever."""
    text = cli.build_parser().format_help()
    for forbidden in ("--code", "--token", "--secret", "--client-secret"):
        assert forbidden not in text


def test_no_advice_this_command_gives_needs_a_placeholder_substituted():
    """Help text is copied straight into a shell. `<` and `>` break it there."""
    uri = "http://localhost:8765/callback"
    for source in (
        cli.build_parser().format_help(),
        cli.LOGIN_MAIL_EXPLANATION.format(redirect_uri=uri),
        cli.REGISTER_APPLICATION_EXPLANATION.format(redirect_uri=uri),
        cli.LOGIN_MAIL_STEPS,
    ):
        for line in source.splitlines():
            if "yandex-mcp" in line:
                assert "<" not in line and ">" not in line, line


def test_the_authorization_code_never_reaches_the_terminal(
    port, browser, typed, token_endpoint, capsys
):
    """The browser's request line carries the code in its query string.

    Python's HTTP server logs every request line to stderr by default. Left on,
    that puts a live credential in the operator's scrollback -- and in any log a
    wrapper script keeps of this command's output.
    """
    _profile(oauth_client_id=CLIENT_ID)

    assert _login(port) == 0

    captured = capsys.readouterr()
    assert CODE not in captured.out
    assert CODE not in captured.err, "the authorization code was logged"
