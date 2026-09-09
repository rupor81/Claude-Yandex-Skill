"""The OAuth Authorization Code flow with PKCE, shared by Mail and Disk.

It lives in the core rather than in the mail package because epic 3 needs the
identical flow for Disk, differing only in scopes -- and a server may not import
another server (AD-2).  Epic 1's rule against speculative generalisation applies
to shapes nobody has seen twice; this one has two named consumers in the plan.

Two measured facts shape everything here.

**This connector is a public client.**  Yandex accepts ``code_verifier`` in place
of a client secret, so there is no application secret -- none to store, none to
leak, and none for a reader to helpfully add later.

**The redirect URI is not ours to choose.**  FR4.1 specified a transient local
listener; the registration form refuses ``http://localhost:8765/callback``, and
API-access applications have a fixed, non-editable redirect.  So Yandex displays
the code and the operator pastes it.  The side effect is a flow that works on a
machine with no browser, which a listener never would have.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import AuthError, PolicyError, ProtocolError, TransportError

__all__ = [
    "AUTHORIZE_URL",
    "TOKEN_URL",
    "VERIFICATION_REDIRECT",
    "LoginRequest",
    "Tokens",
    "checked_state",
    "default_post",
    "exchange_code",
    "extract_code",
    "refresh_access_token",
    "start_login",
]

AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"

#: The only redirect this platform will deliver to. Measured, not chosen.
VERIFICATION_REDIRECT = "https://oauth.yandex.ru/verification_code"

#: RFC 7636 puts the verifier's floor at 43 characters. 32 random bytes in
#: base64url is 43, which is the floor and is plenty.
_VERIFIER_BYTES = 32

#: What a caller must do when the stored refresh token is no longer any good.
_RELOGIN = "yandex-mcp login mail"

#: ``(url, form_fields) -> (status_code, parsed_json_body)``.
Post = Callable[[str, dict[str, str]], "tuple[int, dict[str, Any]]"]


@dataclass(frozen=True, slots=True)
class LoginRequest:
    """One login in flight: what to show the operator, and what to check after.

    Frozen because the verifier and the state are only worth anything if they
    are the same ones the URL was built from.  A flow that let either be
    reassigned halfway could send a challenge for one verifier and exchange
    another, and the failure would look like Yandex being wrong.
    """

    url: str
    state: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class Tokens:
    """What the token endpoint answered.

    ``refresh_token`` is optional because a refresh may legitimately answer
    without issuing a new one, and inventing one there would store a value
    nobody granted.
    """

    access_token: str
    expires_in: int | None = None
    refresh_token: str | None = None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def start_login(*, client_id: str, scopes: Sequence[str]) -> LoginRequest:
    """Build the URL the operator opens, and the secrets that prove it was ours.

    The scopes are in the URL in plain sight on purpose: the operator is about
    to hand them over, and an authorization request they cannot read before
    approving is not one they can meaningfully consent to.
    """
    if not client_id:
        raise ProtocolError(
            "No OAuth client_id is configured. Register an application at "
            "https://oauth.yandex.ru with the rights `mail:imap_full` and "
            "`mail:smtp`, then run `yandex-mcp login mail` again."
        )
    verifier = _b64url(secrets.token_bytes(_VERIFIER_BYTES))
    state = _b64url(secrets.token_bytes(16))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": VERIFICATION_REDIRECT,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return LoginRequest(
        url=f"{AUTHORIZE_URL}?{query}", state=state, code_verifier=verifier
    )


def checked_state(*, issued: str, returned: str) -> None:
    """Refuse a response that cannot be shown to belong to the login we started.

    Raises:
        ProtocolError: on any mismatch. Nothing is exchanged, because the code
            in a response we did not cause is not a code we may spend.
    """
    if not issued or not secrets.compare_digest(issued, returned):
        raise ProtocolError(
            "The authorization response carries a different `state` than this "
            "login issued, so it does not belong to it. Nothing was exchanged. "
            "Run the login again and use the page it opens."
        )


def extract_code(pasted: str) -> str:
    """The code out of whatever the operator copied from their browser.

    They are copying by hand from a page. Refusing a trailing newline, or the
    whole URL when that is what the browser put on the clipboard, would be this
    server being fussy about the one step it already asks a human to perform.
    """
    text = (pasted or "").strip()
    if not text:
        raise ProtocolError("No authorization code was entered.")
    if "://" in text:
        parts = urllib.parse.urlsplit(text)
        for blob in (parts.query, parts.fragment):
            found = dict(urllib.parse.parse_qsl(blob)).get("code")
            if found:
                return found
        raise ProtocolError(
            "That address carries no `code`. Copy the code Yandex displays, or "
            "the whole address of the page it displays it on."
        )
    if any(character.isspace() for character in text):
        raise ProtocolError("An authorization code contains no spaces.")
    return text


def default_post(url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """The real wire call.

    Public, and the module's one seam: it is what every caller gets by default,
    and it is what a test replaces to run the whole flow without a network. A
    private name here would have every caller importing an underscore.
    """
    import httpx

    response = httpx.post(url, data=data, timeout=30.0)
    return response.status_code, json.loads(response.text)


def _tokens_from(payload: dict[str, Any], *, require_refresh: bool) -> Tokens:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise ProtocolError(
            "The token endpoint answered without an access token, so there is "
            "nothing to use. Nothing was stored."
        )
    refresh = payload.get("refresh_token")
    if require_refresh and not (isinstance(refresh, str) and refresh):
        raise ProtocolError(
            "The token endpoint answered without a refresh token, so this "
            "login could not be kept. Nothing was stored -- run the login "
            "again, and check the application's rights include `mail:imap_full` "
            "and `mail:smtp`."
        )
    expires = payload.get("expires_in")
    return Tokens(
        access_token=access,
        expires_in=expires if isinstance(expires, int) else None,
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
    )


def _raise_for(payload: dict[str, Any], *, during: str, relogin_hint: str) -> None:
    """Turn the endpoint's refusal into this project's taxonomy.

    Neither the code, the verifier, nor any token is quoted here. Errors get
    pasted into issues and log files, and a message that carries the credential
    that produced it turns every one of those into a disclosure.
    """
    kind = str(payload.get("error") or "").strip()
    described = str(payload.get("error_description") or "").strip()
    haystack = f"{kind} {described}".lower()

    if "organization" in haystack or "organisation" in haystack or "policy" in haystack:
        raise PolicyError(
            f"This organisation's policy does not allow this application to "
            f"{during}. That is a Yandex 360 administrator setting, not a "
            "problem with the credential -- ask them to permit external "
            "clients, or to allow this application."
        )
    if kind == "invalid_grant":
        raise AuthError(relogin_hint)
    if kind in {"invalid_client", "unauthorized_client"}:
        raise AuthError(
            "Yandex does not recognise this application's `client_id`, so it "
            f"would not {during}. Check the `client_id` in the profile against "
            "the one at https://oauth.yandex.ru."
        )
    if kind == "invalid_scope":
        raise AuthError(
            "The application is not registered for the rights this connector "
            "asks for. Give it `mail:imap_full` and `mail:smtp` at "
            "https://oauth.yandex.ru, then run the login again."
        )
    raise ProtocolError(
        f"Yandex refused to {during} and gave a reason this connector does not "
        f"recognise ({kind or 'no error code given'}). Nothing was stored."
    )


def _call(
    post: Post, data: dict[str, str], *, during: str, relogin_hint: str
) -> dict[str, Any]:
    try:
        status, payload = post(TOKEN_URL, data)
    except (OSError, TimeoutError) as exc:
        # Not an authentication failure. Saying "your credential is wrong" here
        # sends the operator to redo a login that was never the problem.
        raise TransportError(
            f"Could not reach {TOKEN_URL} to {during}: the network or the host "
            f"is unavailable ({type(exc).__name__})."
        ) from exc
    except ValueError as exc:
        raise ProtocolError(
            f"Yandex answered the request to {during} with something that is "
            "not JSON, so it could not be read. Nothing was stored."
        ) from exc
    if not isinstance(payload, dict):
        raise ProtocolError(
            f"Yandex answered the request to {during} with something other "
            "than an object. Nothing was stored."
        )
    if status >= 400 or payload.get("error"):
        _raise_for(payload, during=during, relogin_hint=relogin_hint)
    return payload


def exchange_code(
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    post: Post = default_post,
) -> Tokens:
    """Spend the authorization code for tokens.

    A code is single-use and short-lived, so the ordinary failure here is an
    operator who took too long or pasted one they had already used. That is
    worth saying plainly, because the fix -- run the login again -- is not the
    fix for any other authentication error.

    Raises:
        AuthError: the code was wrong, spent, or expired; or the application is
            not the one Yandex knows.
        PolicyError: an organisation forbids this application.
        ProtocolError: the answer could not be read, or carried no refresh
            token, in which case nothing was stored.
        TransportError: the endpoint could not be reached.
    """
    payload = _call(
        post,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        during="exchange the authorization code",
        relogin_hint=(
            "That authorization code is wrong, already used, or expired -- they "
            "last only minutes and only once. Nothing was stored. Run the login "
            "again and paste the code from the page it opens."
        ),
    )
    return _tokens_from(payload, require_refresh=True)


def refresh_access_token(
    *,
    client_id: str,
    refresh_token: str,
    profile: str | None = None,
    post: Post = default_post,
) -> Tokens:
    """Renew the access token, without anybody being asked anything.

    This is the call that makes the stored refresh token worth storing: it runs
    inside an ordinary tool call, so it must never prompt and must never leave
    the caller guessing whether their credential is gone.

    Raises:
        AuthError: the refresh token has been revoked or expired, naming the
            profile and the command that repairs it.
        PolicyError: an organisation forbids this application.
        ProtocolError: the answer could not be read.
        TransportError: the endpoint could not be reached.
    """
    named = f" for profile {profile!r}" if profile else ""
    return _tokens_from(
        _call(
            post,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            during="renew the access token",
            relogin_hint=(
                f"The stored mail authorisation{named} is no longer valid -- it "
                f"was revoked, or it expired. Run `{_RELOGIN}` to authorise "
                "again. Nothing else is wrong."
            ),
        ),
        require_refresh=False,
    )
