"""OAuth for the remote connector: Claude signs in here, the operator proves who
they are with Yandex ID, and only allowed logins get a token.

Stateless by design -- a serverless host keeps no memory between requests, so
every artefact this server issues (client registration, authorisation code,
access and refresh token) is its own content, encrypted and authenticated with
one server key. Nothing is stored; rotating the key revokes everything.

Yandex ID is used for identity only (`login:info`). What the tools use to reach
mail and calendar are the app passwords in the host's secret settings -- the
same credentials the local connectors use.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass

import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

__all__ = [
    "YANDEX_AUTHORIZE",
    "YANDEX_INFO",
    "YANDEX_TOKEN",
    "RemoteSettings",
    "YandexIdProvider",
]

YANDEX_AUTHORIZE = "https://oauth.yandex.ru/authorize"
YANDEX_TOKEN = "https://oauth.yandex.ru/token"
YANDEX_INFO = "https://login.yandex.ru/info?format=json"

CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
STATE_TTL = 600

#: Where an MCP client may be sent back to. Claude's callbacks only: a stateless
#: server accepts any registration, so the redirect is what keeps a code from
#: being delivered to somebody else's site.
DEFAULT_REDIRECT_HOSTS = ("claude.ai", "claude.com")


@dataclass(frozen=True)
class RemoteSettings:
    base_url: str
    secret: str
    yandex_client_id: str
    yandex_client_secret: str
    allowed_logins: frozenset[str]
    redirect_hosts: tuple[str, ...] = DEFAULT_REDIRECT_HOSTS

    @classmethod
    def from_env(cls) -> RemoteSettings:
        missing = [
            name
            for name in (
                "YANDEX_MCP_REMOTE_BASE_URL",
                "YANDEX_MCP_REMOTE_SECRET",
                "YANDEX_OAUTH_CLIENT_ID",
                "YANDEX_OAUTH_CLIENT_SECRET",
                "YANDEX_MCP_ALLOWED_LOGINS",
            )
            if not os.environ.get(name, "").strip()
        ]
        if missing:
            raise RuntimeError(
                f"Remote connector is not configured: set {', '.join(missing)}."
            )
        return cls(
            base_url=os.environ["YANDEX_MCP_REMOTE_BASE_URL"].rstrip("/"),
            secret=os.environ["YANDEX_MCP_REMOTE_SECRET"],
            yandex_client_id=os.environ["YANDEX_OAUTH_CLIENT_ID"],
            yandex_client_secret=os.environ["YANDEX_OAUTH_CLIENT_SECRET"],
            allowed_logins=_logins(os.environ["YANDEX_MCP_ALLOWED_LOGINS"]),
        )

    @property
    def callback_url(self) -> str:
        return f"{self.base_url}/yandex/callback"


def _logins(raw: str) -> frozenset[str]:
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


class YandexIdProvider:
    """The SDK's OAuthAuthorizationServerProvider, with Yandex ID as the login."""

    def __init__(self, settings: RemoteSettings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self._fernet = Fernet(settings.secret.encode())
        self._http = http

    # -- sealing ------------------------------------------------------------------

    def seal(self, kind: str, payload: dict, ttl: int) -> str:
        body = {**payload, "k": kind, "exp": int(time.time()) + ttl}
        return self._fernet.encrypt(
            json.dumps(body, separators=(",", ":")).encode()
        ).decode()

    def open(self, kind: str, token: str) -> dict | None:
        """The sealed payload if it is ours, of this kind, and unexpired; else None."""
        try:
            body = json.loads(self._fernet.decrypt(token.encode()))
        except (InvalidToken, ValueError, TypeError):
            return None
        if body.get("k") != kind or body.get("exp", 0) < time.time():
            return None
        return body

    # -- clients ------------------------------------------------------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            parts = urllib.parse.urlsplit(str(uri))
            if (
                parts.scheme != "https"
                or parts.hostname not in self.settings.redirect_hosts
            ):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"Redirect URI {uri} is not allowed on this server.",
                )
        # ponytail: relies on the SDK returning this same object to the client after
        # the call -- held by test_registration_is_stateless. The client_id becomes
        # the whole registration, sealed, so get_client needs no storage.
        record = client_info.model_dump(mode="json", exclude={"client_id"})
        client_info.client_id = self.seal("client", record, ttl=10 * 365 * 24 * 3600)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        record = self.open("client", client_id)
        if record is None:
            return None
        record = {k: v for k, v in record.items() if k not in {"k", "exp"}}
        return OAuthClientInformationFull.model_validate(
            {**record, "client_id": client_id}
        )

    # -- authorisation --------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        state = self.seal(
            "state",
            {
                "cid": client.client_id,
                "redirect": str(params.redirect_uri),
                "explicit": params.redirect_uri_provided_explicitly,
                "challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [],
                "resource": params.resource,
            },
            ttl=STATE_TTL,
        )
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.yandex_client_id,
                "redirect_uri": self.settings.callback_url,
                "state": state,
                "force_confirm": "no",
            }
        )
        return f"{YANDEX_AUTHORIZE}?{query}"

    async def finish_yandex_login(self, code: str, state: str) -> str:
        """The Yandex callback: prove who signed in, then send Claude its code.

        Returns the URL to redirect the browser to.

        Raises:
            PermissionError: the state is not ours, Yandex refused, or the login
                is not allowed.
        """
        pending = self.open("state", state)
        if pending is None:
            raise PermissionError(
                "This sign-in link has expired or is not ours. Start again from Claude."
            )
        login = await self._yandex_login(code)
        if login not in self.settings.allowed_logins:
            raise PermissionError(
                f"The Yandex account {login} is not allowed on this connector."
            )
        our_code = self.seal(
            "code",
            {
                "cid": pending["cid"],
                "redirect": pending["redirect"],
                "explicit": pending["explicit"],
                "challenge": pending["challenge"],
                "scopes": pending["scopes"],
                "resource": pending["resource"],
                "sub": login,
            },
            ttl=CODE_TTL,
        )
        query = {"code": our_code}
        if pending.get("state"):
            query["state"] = pending["state"]
        sep = "&" if "?" in pending["redirect"] else "?"
        return f"{pending['redirect']}{sep}{urllib.parse.urlencode(query)}"

    async def _yandex_login(self, code: str) -> str:
        http = self._http or httpx.AsyncClient(timeout=20)
        try:
            token = await http.post(
                YANDEX_TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": self.settings.yandex_client_id,
                    "client_secret": self.settings.yandex_client_secret,
                },
            )
            if token.status_code != 200 or "access_token" not in token.json():
                raise PermissionError(
                    "Yandex did not confirm the sign-in. Start again from Claude."
                )
            info = await http.get(
                YANDEX_INFO,
                headers={"Authorization": f"OAuth {token.json()['access_token']}"},
            )
            data = info.json() if info.status_code == 200 else {}
        finally:
            if self._http is None:
                await http.aclose()
        login = (data.get("default_email") or data.get("login") or "").strip().lower()
        if not login:
            raise PermissionError("Yandex did not say who signed in.")
        # A login without a domain is the Yandex ID login; allow either form.
        if "@" not in login and f"{login}@yandex.ru" in self.settings.allowed_logins:
            return f"{login}@yandex.ru"
        return login

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        body = self.open("code", authorization_code)
        if body is None or body["cid"] != client.client_id:
            return None
        # ponytail: stateless, so a code can be replayed within its 5 minutes; PKCE
        # binds it to the verifier only the real client holds. A store closes it.
        return AuthorizationCode(
            code=authorization_code,
            scopes=body["scopes"],
            expires_at=body["exp"],
            client_id=body["cid"],
            code_challenge=body["challenge"],
            redirect_uri=body["redirect"],
            redirect_uri_provided_explicitly=body["explicit"],
            resource=body["resource"],
            subject=body["sub"],
        )

    def _tokens(self, client_id: str, subject: str, scopes: list[str]) -> OAuthToken:
        claims = {"cid": client_id, "sub": subject, "scopes": scopes}
        return OAuthToken(
            access_token=self.seal("access", claims, ACCESS_TTL),
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=self.seal("refresh", claims, REFRESH_TTL),
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return self._tokens(
            client.client_id,
            authorization_code.subject or "",
            authorization_code.scopes,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        body = self.open("access", token)
        # Re-checked on every call: removing a login from the list revokes it.
        if body is None or body["sub"] not in self.settings.allowed_logins:
            return None
        return AccessToken(
            token=token,
            client_id=body["cid"],
            scopes=body["scopes"],
            expires_at=body["exp"],
            subject=body["sub"],
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        body = self.open("refresh", refresh_token)
        if (
            body is None
            or body["cid"] != client.client_id
            or body["sub"] not in self.settings.allowed_logins
        ):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=body["cid"],
            scopes=body["scopes"],
            expires_at=body["exp"],
            subject=body["sub"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes) <= set(refresh_token.scopes):
            raise TokenError(
                error="invalid_scope", error_description="Scopes exceed the grant."
            )
        return self._tokens(
            client.client_id,
            refresh_token.subject or "",
            scopes or refresh_token.scopes,
        )

    async def revoke_token(self, token: object) -> None:
        # ponytail: stateless tokens cannot be revoked one by one; rotate
        # YANDEX_MCP_REMOTE_SECRET to revoke all, or drop the login from the list.
        return None

    async def exchange_identity_assertion(
        self, *args: object, **kwargs: object
    ) -> OAuthToken:
        raise TokenError(
            error="unsupported_grant_type", error_description="Not supported."
        )
