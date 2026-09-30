"""The remote connector end to end: Claude registers, the operator signs in with
Yandex ID (played by a fake that answers as Yandex documents), Claude gets a
token, and tools answer only with it."""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
from contextlib import contextmanager

import anyio
import httpx
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from yandex_core.config import Profile
from yandex_remote_mcp.app import build_remote_app
from yandex_remote_mcp.auth import (
    YANDEX_INFO,
    YANDEX_TOKEN,
    RemoteSettings,
    YandexIdProvider,
)

BASE = "https://yandex-mcp.example.app"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def _settings(**kw):
    return RemoteSettings(
        base_url=BASE,
        secret=Fernet.generate_key().decode(),
        yandex_client_id="ya-client",
        yandex_client_secret="ya-secret",
        allowed_logins=frozenset({"me@yandex.ru"}),
        **kw,
    )


def _fake_yandex(login="me@yandex.ru", token_status=200):
    """Yandex's token and user-info endpoints, as documented."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(YANDEX_TOKEN):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            assert form["client_secret"] == "ya-secret"
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            return httpx.Response(
                200, json={"access_token": "ya-at", "token_type": "bearer"}
            )
        if url.startswith(YANDEX_INFO.split("?")[0]):
            assert request.headers["Authorization"] == "OAuth ya-at"
            return httpx.Response(
                200, json={"login": login.split("@")[0], "default_email": login}
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _client(settings, yandex):
    provider = YandexIdProvider(settings, http=yandex)
    app = build_remote_app(
        settings,
        profile=Profile(name="default", login="me@yandex.ru"),
        provider=provider,
    )
    return TestClient(app, base_url=BASE), provider


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def _sign_in(http, login_ok=True):
    reg = http.post(
        "/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude"}
    )
    assert reg.status_code == 201, reg.text
    client = reg.json()
    verifier, challenge = _pkce()
    auth = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "claude-state",
        },
        follow_redirects=False,
    )
    assert auth.status_code == 302, auth.text
    to_yandex = urllib.parse.urlsplit(auth.headers["location"])
    assert to_yandex.netloc == "oauth.yandex.ru"
    state = dict(urllib.parse.parse_qsl(to_yandex.query))["state"]
    back = http.get(
        "/yandex/callback",
        params={"code": "ya-code", "state": state},
        follow_redirects=False,
    )
    return client, verifier, back


def _token(http, client, verifier, back):
    to_claude = urllib.parse.urlsplit(back.headers["location"])
    q = dict(urllib.parse.parse_qsl(to_claude.query))
    assert q["state"] == "claude-state"
    tok = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"],
            "redirect_uri": CALLBACK,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.text
    return tok.json()


def _mcp(http, token, method="tools/list"):
    return http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
    )


# -- the happy path ------------------------------------------------------------------


def test_claude_signs_in_with_yandex_id_and_lists_the_tools():
    settings = _settings()
    with _ctx(settings, _fake_yandex()) as http:
        client, verifier, back = _sign_in(http)
        assert back.status_code == 302 and back.headers["location"].startswith(CALLBACK)
        tokens = _token(http, client, verifier, back)
        listed = _mcp(http, tokens["access_token"])

    assert listed.status_code == 200, listed.text
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert {"calendar_events_list", "mail_messages_list", "mail_message_get"} <= names
    assert "mail_attachment_download" not in names, (
        "a download here lands on the server"
    )


def test_discovery_metadata_points_at_this_server():
    with _ctx(_settings(), _fake_yandex()) as http:
        meta = http.get("/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"] == f"{BASE}/authorize"
    assert meta["registration_endpoint"] == f"{BASE}/register"


# -- refusals -------------------------------------------------------------------------


def test_no_token_no_tools():
    with _ctx(_settings(), _fake_yandex()) as http:
        r = http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert r.status_code == 401


def test_a_forged_token_is_refused():
    with _ctx(_settings(), _fake_yandex()) as http:
        assert _mcp(http, "not-a-real-token").status_code == 401


def test_a_token_from_another_deployment_is_refused():
    """A different server key: its tokens mean nothing here."""
    with _ctx(_settings(), _fake_yandex()) as http:
        client, verifier, back = _sign_in(http)
        token = _token(http, client, verifier, back)["access_token"]
    with _ctx(_settings(), _fake_yandex()) as other:
        assert _mcp(other, token).status_code == 401


def test_a_login_not_on_the_list_gets_no_code():
    with _ctx(_settings(), _fake_yandex(login="stranger@yandex.ru")) as http:
        _, _, back = _sign_in(http)
    assert back.status_code == 403
    assert "stranger@yandex.ru" in back.text


def test_removing_a_login_revokes_its_tokens():
    settings = _settings()
    with _ctx(settings, _fake_yandex()) as http:
        client, verifier, back = _sign_in(http)
        token = _token(http, client, verifier, back)["access_token"]
    narrowed = RemoteSettings(
        **{**settings.__dict__, "allowed_logins": frozenset({"else@yandex.ru"})}
    )
    with _ctx(narrowed, _fake_yandex()) as http:
        assert _mcp(http, token).status_code == 401


def test_yandex_refusing_the_code_gives_no_code():
    with _ctx(_settings(), _fake_yandex(token_status=400)) as http:
        _, _, back = _sign_in(http)
    assert back.status_code == 403


def test_a_callback_with_a_state_we_did_not_issue_is_refused():
    with _ctx(_settings(), _fake_yandex()) as http:
        r = http.get(
            "/yandex/callback",
            params={"code": "c", "state": "forged"},
            follow_redirects=False,
        )
    assert r.status_code == 403


def test_registration_to_a_redirect_outside_claude_is_refused():
    """Stateless registration accepts anyone -- the redirect is what keeps a code
    from being delivered to somebody else's site."""
    with _ctx(_settings(), _fake_yandex()) as http:
        r = http.post("/register", json={"redirect_uris": ["https://evil.example/cb"]})
    assert r.status_code == 400


def test_a_code_without_the_pkce_verifier_is_refused():
    settings = _settings()
    with _ctx(settings, _fake_yandex()) as http:
        client, _, back = _sign_in(http)
        q = dict(
            urllib.parse.parse_qsl(
                urllib.parse.urlsplit(back.headers["location"]).query
            )
        )
        tok = http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": q["code"],
                "redirect_uri": CALLBACK,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "code_verifier": "wrong-verifier-" + "x" * 40,
            },
        )
    assert tok.status_code == 400


def test_a_refresh_yields_a_working_token():
    settings = _settings()
    with _ctx(settings, _fake_yandex()) as http:
        client, verifier, back = _sign_in(http)
        tokens = _token(http, client, verifier, back)
        fresh = http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
            },
        )
        assert fresh.status_code == 200, fresh.text
        assert _mcp(http, fresh.json()["access_token"]).status_code == 200


def test_registration_is_stateless():
    """The SDK must hand the client the same object the provider sealed. If a
    future SDK copies it first, every sign-in would fail -- this says why."""
    provider = YandexIdProvider(_settings())
    from mcp.shared.auth import OAuthClientInformationFull

    info = OAuthClientInformationFull(
        client_id="uuid", redirect_uris=[CALLBACK], client_secret="s"
    )
    anyio.run(provider.register_client, info)
    assert info.client_id != "uuid"
    restored = anyio.run(provider.get_client, info.client_id)
    assert restored.client_secret == "s"


# -- helpers -------------------------------------------------------------------------


@contextmanager
def _ctx(settings, yandex):
    http, _ = _client(settings, yandex)
    with http:
        yield http


def test_a_refresh_token_or_a_client_id_is_not_an_access_token():
    """All are sealed with one key; only the kind tells them apart. The client_id
    is sealed for ten years and travels in URLs -- as a bearer it must be nothing."""
    with _ctx(_settings(), _fake_yandex()) as http:
        client, verifier, back = _sign_in(http)
        tokens = _token(http, client, verifier, back)
        assert _mcp(http, tokens["refresh_token"]).status_code == 401
        assert _mcp(http, client["client_id"]).status_code == 401


def test_an_expired_access_token_is_refused(monkeypatch):
    import yandex_remote_mcp.auth as auth

    with _ctx(_settings(), _fake_yandex()) as http:
        client, verifier, back = _sign_in(http)
        token = _token(http, client, verifier, back)["access_token"]
        real = auth.time.time
        monkeypatch.setattr(auth.time, "time", lambda: real() + auth.ACCESS_TTL + 5)
        assert _mcp(http, token).status_code == 401


def test_a_code_issued_to_one_client_cannot_be_spent_by_another():
    with _ctx(_settings(), _fake_yandex()) as http:
        _, verifier, back = _sign_in(http)
        other = http.post("/register", json={"redirect_uris": [CALLBACK]}).json()
        q = dict(
            urllib.parse.parse_qsl(
                urllib.parse.urlsplit(back.headers["location"]).query
            )
        )
        tok = http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": q["code"],
                "redirect_uri": CALLBACK,
                "client_id": other["client_id"],
                "client_secret": other["client_secret"],
                "code_verifier": verifier,
            },
        )
    assert tok.status_code == 400
