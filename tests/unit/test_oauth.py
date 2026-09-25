"""The Authorization Code flow with PKCE, and what must never leak out of it.

Every fake here answers the way Yandex was *seen* to answer, or the way its
documentation says it answers -- never the way this module would find
convenient. A fake built from the code under test proves that the code agrees
with itself, which epic 1 learned twice.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import urllib.parse

import pytest

from yandex_core.errors import AuthError, PolicyError, ProtocolError, TransportError
from yandex_core.oauth import (
    AUTHORIZE_URL,
    LOOPBACK_PORT,
    TOKEN_URL,
    LoginRequest,
    checked_state,
    code_from_callback,
    exchange_code,
    loopback_redirect_uri,
    refresh_access_token,
    start_login,
)

CLIENT_ID = "0123456789abcdef0123456789abcdef"
SCOPES = ("mail:imap_full", "mail:smtp")


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _answers(payload, status: int = 200):
    """A transport that answers one canned response and records what it was sent."""
    seen: list[dict] = []

    def post(url: str, data: dict[str, str]) -> tuple[int, dict]:
        seen.append({"url": url, "data": dict(data)})
        return status, payload

    return post, seen


# -- the authorization URL the operator is shown --------------------------


def test_the_authorization_url_names_both_scopes_so_they_can_be_read_first():
    """The operator is granting these. Hiding them would make the ask untrustworthy."""
    request = start_login(client_id=CLIENT_ID, scopes=SCOPES)

    assert request.url.startswith(AUTHORIZE_URL)
    query = _query(request.url)
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert set(query["scope"].split()) == set(SCOPES)


def test_the_url_carries_a_pkce_challenge_and_never_a_secret():
    """This connector is a public client: there is no application secret to send."""
    request = start_login(client_id=CLIENT_ID, scopes=SCOPES)

    query = _query(request.url)
    assert query["code_challenge_method"] == "S256"
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(request.code_verifier.encode("ascii")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    assert query["code_challenge"] == expected, (
        "the challenge is not S256 of the verifier"
    )
    assert "client_secret" not in query
    assert request.code_verifier not in request.url, "the verifier travelled in the URL"


def test_every_login_gets_its_own_verifier_and_state():
    """A reused verifier is a replayable login; a reused state proves nothing."""
    first = start_login(client_id=CLIENT_ID, scopes=SCOPES)
    second = start_login(client_id=CLIENT_ID, scopes=SCOPES)

    assert first.code_verifier != second.code_verifier
    assert first.state != second.state
    # RFC 7636 puts the floor at 43 characters; below it the challenge is guessable.
    assert len(first.code_verifier) >= 43


def test_the_url_sends_the_browser_back_to_this_machine():
    """The standard desktop flow: sign in in the browser, come back to the command.

    An earlier version sent the operator to a page that displayed a code to paste
    by hand, on the strength of a "measurement" that was never taken. See spec
    2.1's change log.
    """
    request = start_login(client_id=CLIENT_ID, scopes=SCOPES)

    redirect = _query(request.url)["redirect_uri"]
    assert redirect == loopback_redirect_uri()
    assert redirect == f"http://localhost:{LOOPBACK_PORT}/callback"
    assert request.redirect_uri == redirect, (
        "the exchange would name a different redirect"
    )


def test_a_redirect_can_be_chosen_for_a_different_registered_port():
    request = start_login(
        client_id=CLIENT_ID, scopes=SCOPES, redirect_uri=loopback_redirect_uri(9001)
    )

    assert _query(request.url)["redirect_uri"] == "http://localhost:9001/callback"


# -- what comes back from the operator ------------------------------------


def test_a_mismatched_state_is_refused_before_anything_is_exchanged():
    with pytest.raises(ProtocolError) as caught:
        checked_state(issued="the-one-we-sent", returned="something-else")

    assert "state" in str(caught.value).lower()


def test_a_matching_state_passes_quietly():
    checked_state(issued="same", returned="same")


def test_the_code_is_taken_from_a_callback_that_proves_it_is_ours():
    assert code_from_callback(
        {"code": "1234567", "state": "ours"}, issued_state="ours"
    ) == ("1234567")


def test_a_callback_with_a_foreign_state_is_refused_and_nothing_is_exchanged():
    """With a real redirect, `state` is what stops a forged callback.

    In the paste flow nothing ever came back carrying one, so the check was
    vacuous there. It is not vacuous now.
    """
    with pytest.raises(ProtocolError) as caught:
        code_from_callback({"code": "1234567", "state": "theirs"}, issued_state="ours")

    assert "state" in str(caught.value).lower()


def test_a_callback_with_no_state_at_all_is_refused():
    with pytest.raises(ProtocolError):
        code_from_callback({"code": "1234567"}, issued_state="ours")


def test_the_operator_pressing_cancel_is_reported_as_their_decision():
    """`access_denied` is not a failure of anything. It is an answer."""
    with pytest.raises(AuthError) as caught:
        code_from_callback(
            {"error": "access_denied", "state": "ours"}, issued_state="ours"
        )

    message = str(caught.value).lower()
    assert "declined" in message or "cancel" in message
    assert "nothing was stored" in message


def test_an_error_yandex_sent_back_is_reported_with_its_own_words():
    with pytest.raises(ProtocolError) as caught:
        code_from_callback(
            {
                "error": "invalid_request",
                "error_description": "redirect_uri mismatch",
                "state": "ours",
            },
            issued_state="ours",
        )

    assert "redirect_uri mismatch" in str(caught.value)


def test_a_redirect_mismatch_says_which_address_to_register():
    """The one registration mistake worth naming precisely: it is a typo away."""
    with pytest.raises(ProtocolError) as caught:
        code_from_callback(
            {
                "error": "invalid_request",
                "error_description": "redirect_uri does not match",
                "state": "ours",
            },
            issued_state="ours",
        )

    assert loopback_redirect_uri() in str(caught.value)


def test_a_callback_with_neither_code_nor_error_is_refused():
    with pytest.raises(ProtocolError):
        code_from_callback({"state": "ours"}, issued_state="ours")


# -- exchanging the code --------------------------------------------------


def test_the_exchange_sends_the_verifier_and_no_secret():
    post, seen = _answers(
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    )

    tokens = exchange_code(
        client_id=CLIENT_ID,
        code="1234567",
        code_verifier="v" * 43,
        redirect_uri=loopback_redirect_uri(),
        post=post,
    )

    assert tokens.access_token == "at"
    assert tokens.refresh_token == "rt"
    (call,) = seen
    assert call["url"] == TOKEN_URL
    assert call["data"]["grant_type"] == "authorization_code"
    assert call["data"]["code"] == "1234567"
    assert call["data"]["code_verifier"] == "v" * 43
    # RFC 6749 4.1.3: named at the exchange when it was named at authorisation.
    assert call["data"]["redirect_uri"] == loopback_redirect_uri()
    assert "client_secret" not in call["data"]


def test_a_used_or_wrong_code_says_so_and_yields_nothing_to_store():
    """Yandex's documented answer for a code that is wrong, expired or spent."""
    post, _ = _answers(
        {"error": "invalid_grant", "error_description": "Code has expired"}, 400
    )

    with pytest.raises(AuthError) as caught:
        exchange_code(
            client_id=CLIENT_ID, code="1234567", code_verifier="v" * 43, post=post
        )

    message = str(caught.value)
    assert "code" in message.lower()
    assert "again" in message.lower(), "the operator is not told what to do next"


def test_an_answer_with_no_refresh_token_is_refused_rather_than_half_stored():
    """A login that stores nothing usable is worse than one that failed loudly."""
    post, _ = _answers({"access_token": "at", "expires_in": 3600})

    with pytest.raises(ProtocolError) as caught:
        exchange_code(
            client_id=CLIENT_ID, code="1234567", code_verifier="v" * 43, post=post
        )

    assert "refresh" in str(caught.value).lower()


# -- renewing, which happens without anybody being asked ------------------


def test_a_refresh_sends_the_refresh_grant_and_no_secret():
    post, seen = _answers({"access_token": "fresh", "expires_in": 3600})

    tokens = refresh_access_token(client_id=CLIENT_ID, refresh_token="rt", post=post)

    assert tokens.access_token == "fresh"
    (call,) = seen
    assert call["data"]["grant_type"] == "refresh_token"
    assert call["data"]["refresh_token"] == "rt"
    assert "client_secret" not in call["data"]


def test_a_revoked_refresh_token_names_the_command_that_repairs_it():
    post, _ = _answers(
        {"error": "invalid_grant", "error_description": "expired token"}, 400
    )

    with pytest.raises(AuthError) as caught:
        refresh_access_token(
            client_id=CLIENT_ID, refresh_token="rt", profile="work", post=post
        )

    message = str(caught.value)
    assert "yandex-mcp login mail" in message
    assert "work" in message, "the profile that needs repairing is not named"


def test_a_refresh_that_keeps_the_old_refresh_token_is_honoured():
    """Yandex may answer a refresh without issuing a new refresh token."""
    post, _ = _answers({"access_token": "fresh", "expires_in": 3600})

    tokens = refresh_access_token(client_id=CLIENT_ID, refresh_token="rt", post=post)

    assert tokens.refresh_token is None, "a token nobody issued was invented"


# -- organisation policy, which is not a bad credential -------------------


def test_an_organisation_that_forbids_external_clients_is_reported_as_policy():
    """FR4.5: telling an operator their token is wrong sends them to fix the wrong thing."""
    post, _ = _answers(
        {
            "error": "access_denied",
            "error_description": "Application is not allowed by organization policy",
        },
        403,
    )

    with pytest.raises(PolicyError) as caught:
        exchange_code(
            client_id=CLIENT_ID, code="1234567", code_verifier="v" * 43, post=post
        )

    assert (
        "organisation" in str(caught.value).lower()
        or "organization" in str(caught.value).lower()
    )


# -- nothing secret ever reaches a message --------------------------------


def test_no_token_code_or_verifier_appears_in_any_error():
    """Errors get read, pasted into issues, and logged. Secrets must not ride along."""
    secret_code = "SECRET-CODE-9999"
    secret_verifier = "SECRET-VERIFIER-" + "v" * 30
    post, _ = _answers({"error": "invalid_grant", "error_description": "bad"}, 400)

    with pytest.raises(AuthError) as caught:
        exchange_code(
            client_id=CLIENT_ID,
            code=secret_code,
            code_verifier=secret_verifier,
            post=post,
        )
    assert secret_code not in str(caught.value)
    assert secret_verifier not in str(caught.value)

    post, _ = _answers({"error": "invalid_grant"}, 400)
    with pytest.raises(AuthError) as caught:
        refresh_access_token(
            client_id=CLIENT_ID, refresh_token="SECRET-REFRESH", post=post
        )
    assert "SECRET-REFRESH" not in str(caught.value)


def test_an_unreachable_token_endpoint_is_a_transport_error_not_an_auth_one():
    """ "Your credential is wrong" sends the operator to re-do a login that was fine."""

    def post(url, data):
        raise OSError("Network is unreachable")

    with pytest.raises(TransportError):
        refresh_access_token(client_id=CLIENT_ID, refresh_token="rt", post=post)


def test_an_answer_that_is_not_json_is_reported_as_a_protocol_error():
    def post(url, data):
        raise ValueError("Expecting value: line 1 column 1")

    with pytest.raises(ProtocolError):
        exchange_code(
            client_id=CLIENT_ID, code="1234567", code_verifier="v" * 43, post=post
        )


def test_login_request_is_frozen_so_a_verifier_cannot_be_swapped_mid_flow():
    request = start_login(client_id=CLIENT_ID, scopes=SCOPES)
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.code_verifier = "something else"  # type: ignore[misc]
    assert isinstance(request, LoginRequest)
