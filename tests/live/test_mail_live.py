"""One real call against a real Yandex mailbox.

Skipped unless `YANDEX_MCP_LIVE_TESTS=1` *and* the profile has been authorised,
because everything else in this suite runs with no network and no credentials.

    YANDEX_MCP_LIVE_TESTS=1 uv run pytest tests/live -q

What these assert is the *contract*, never a claim about the operator's mail.
"How many folders exist" and "what they are called" are facts about somebody's
mailbox; a real account that happens to have one odd folder must not fail a test
of code that has nothing wrong with it. Nothing here reads a message, and
nothing here writes anything at all.
"""

from __future__ import annotations

import os

import anyio
import pytest

from yandex_core.config import load_profile
from yandex_core.credentials import get_secret
from yandex_core.errors import YandexError
from yandex_core.oauth import refresh_access_token
from yandex_core.results import Page
from yandex_mail_mcp.client.imap_client import IMAPMailClient
from yandex_mail_mcp.tools.folders import build_mail_folders_list

pytestmark = pytest.mark.skipif(
    os.environ.get("YANDEX_MCP_LIVE_TESTS") != "1",
    reason="live tests need YANDEX_MCP_LIVE_TESTS=1 and an authorised mailbox",
)


def _authorised_profile():
    """The profile, or a skip naming exactly what has not been done yet.

    A hard failure here would read as "the mail connector is broken" on a
    machine where nobody has run the login, which is a different thing entirely.
    """
    try:
        profile = load_profile()
    except YandexError as exc:
        pytest.skip(f"no profile: {exc}")
    if not profile.oauth_client_id:
        pytest.skip(
            "the profile has no OAuth client_id -- register an application at "
            "https://oauth.yandex.ru and run `yandex-mcp login mail`"
        )
    try:
        get_secret("mail", profile.name)
    except YandexError as exc:
        pytest.skip(f"the mailbox is not authorised: {exc}")
    return profile


def _client():
    profile = _authorised_profile()

    async def access_token() -> str:
        return refresh_access_token(
            client_id=profile.oauth_client_id or "",
            refresh_token=get_secret("mail", profile.name),
            profile=profile.name,
        ).access_token

    return profile, IMAPMailClient(
        host=profile.imap_host,
        port=profile.imap_port,
        login=profile.login,
        access_token_provider=access_token,
    )


def test_a_real_refresh_token_yields_a_live_access_token():
    """The renewal is what makes the stored grant worth storing."""
    profile = _authorised_profile()

    tokens = refresh_access_token(
        client_id=profile.oauth_client_id or "",
        refresh_token=get_secret("mail", profile.name),
        profile=profile.name,
    )

    assert tokens.access_token
    assert tokens.access_token != get_secret("mail", profile.name)


def test_a_real_mailbox_lists_its_real_folders():
    """The vertical slice: OAuth, XOAUTH2, LIST and STATUS, end to end."""
    _, client = _client()

    async def call():
        tool = build_mail_folders_list(lambda: _ready(client))
        return await tool(limit=10)

    page = anyio.run(call)

    assert isinstance(page, Page)
    assert page.items, "a real mailbox listed no folders at all"
    for folder in page.items:
        assert folder.name, "a folder came back with no name"
        # Modified UTF-7 leaks look like this. If the library ever stops
        # decoding, the operator sees mojibake and cannot tell it from a bug.
        assert not (folder.name.startswith("&") and folder.name.endswith("-")), (
            f"folder name {folder.name!r} is still modified UTF-7"
        )
        if folder.messages is None:
            assert folder.counts_note, f"{folder.name} has no counts and no reason"
        else:
            assert folder.messages >= 0
    print(f"\nfolders: {[f.name for f in page.items]}")


def test_the_hierarchy_delimiter_this_server_really_uses_is_reported():
    """Documented as `|`. Asserted as "whatever it is, it is reported", because
    what matters to a caller is that they need not guess it."""
    _, client = _client()

    async def call():
        tool = build_mail_folders_list(lambda: _ready(client))
        return await tool(limit=10)

    page = anyio.run(call)

    delimiters = {folder.delimiter for folder in page.items}
    assert delimiters, "no folders to report a delimiter"
    assert all(delimiters), f"a folder reported no delimiter at all: {delimiters}"
    print(f"\ndelimiters in use: {delimiters}")


def test_paging_a_real_folder_list_terminates_and_never_dead_ends():
    """NFR3 against a real mailbox: every page is honest about what it left out."""
    _, client = _client()

    async def call():
        tool = build_mail_folders_list(lambda: _ready(client))
        seen: list[str] = []
        cursor = None
        for _ in range(20):  # a bound, so a broken cursor cannot loop forever
            page = await tool(limit=2, cursor=cursor)
            seen.extend(folder.name for folder in page.items)
            if page.complete:
                assert page.next_cursor is None
                return seen, True
            assert page.next_cursor, "an incomplete page carried no cursor"
            cursor = page.next_cursor
        return seen, False

    seen, finished = anyio.run(call)

    assert finished, "paging did not terminate within 20 pages"
    assert len(seen) == len(set(seen)), "a folder was returned on two pages"


async def _ready(client: IMAPMailClient) -> IMAPMailClient:
    return client
