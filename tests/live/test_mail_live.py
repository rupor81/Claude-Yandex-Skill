"""One real call against a real Yandex mailbox.

Skipped unless `YANDEX_MCP_LIVE_TESTS=1` *and* a mail app password is stored,
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
from yandex_core.results import Page
from yandex_mail_mcp.client.imap_client import IMAPMailClient
from yandex_mail_mcp.tools.folders import build_mail_folders_list

pytestmark = pytest.mark.skipif(
    os.environ.get("YANDEX_MCP_LIVE_TESTS") != "1",
    reason="live tests need YANDEX_MCP_LIVE_TESTS=1 and an authorised mailbox",
)


def _mail_profile():
    """The profile, or a skip naming exactly what has not been done yet.

    A hard failure here would read as "the mail connector is broken" on a
    machine where nobody has run the setup, which is a different thing entirely.
    """
    try:
        profile = load_profile()
        get_secret("mail", profile.name)
    except YandexError as exc:
        pytest.skip(f"mail is not set up -- run `yandex-mcp setup mail`: {exc}")
    return profile


def _client():
    profile = _mail_profile()

    async def password() -> str:
        return get_secret("mail", profile.name)

    return profile, IMAPMailClient(
        host=profile.imap_host,
        port=profile.imap_port,
        login=profile.login,
        password_provider=password,
    )


def test_a_real_mailbox_lists_its_real_folders():
    """The vertical slice: app password, LOGIN, LIST and STATUS, end to end."""
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


# -- story 2.2: headers over a date range ----------------------------------------


def _unseen(profile) -> int:
    """UNSEEN in INBOX, by a read-only STATUS -- itself changes nothing."""
    from imap_tools import MailBox

    with MailBox(profile.imap_host, profile.imap_port, timeout=30).login(
        profile.login, get_secret("mail", profile.name), initial_folder=None
    ) as box:
        return box.folder.status("INBOX", ["UNSEEN"])["UNSEEN"]


def _messages_tool(client):
    from yandex_mail_mcp.tools.messages import build_mail_messages_list

    return build_mail_messages_list(lambda: _ready(client))


def test_listing_a_real_week_marks_nothing_read():
    """The promise that matters most on somebody's real mailbox."""
    from datetime import UTC, datetime, timedelta

    profile, client = _client()
    before = _unseen(profile)
    end = datetime.now(UTC)
    start = end - timedelta(days=7)

    page = anyio.run(
        lambda: _messages_tool(client)(
            start=start.isoformat(), end=end.isoformat(), limit=50
        )
    )

    assert _unseen(profile) == before, "listing changed how many messages are unread"
    assert page.items, "a real week of INBOX returned nothing"
    for item in page.items:
        when = datetime.fromisoformat(item.date)
        assert start <= when < end, "a message fell outside its own range"
        assert "�" not in item.subject, "a subject decoded to replacement marks"
        assert item.has_attachments is not None, "a real structure could not be read"
    print(
        f"\nweek: {len(page.items)} returned, remaining={page.remaining}, complete={page.complete}"
    )


def test_a_filtered_month_pages_to_its_end_without_repeats():
    """NFR3 on real mail: every page honest, the last one complete, nothing twice."""
    from datetime import UTC, datetime, timedelta

    _, client = _client()
    end = datetime.now(UTC)
    start = end - timedelta(days=30)
    tool = _messages_tool(client)

    async def walk():
        seen, cursor, calls = [], None, 0
        while calls < 20:
            calls += 1
            page = await tool(
                start=start.isoformat(),
                end=end.isoformat(),
                from_contains="@",
                limit=100,
                cursor=cursor,
            )
            seen += [m.uid for m in page.items]
            if page.complete:
                return seen, calls, True
            assert page.next_cursor
            cursor = page.next_cursor
        return seen, calls, False

    seen, calls, finished = anyio.run(walk)

    assert finished, "paging did not reach the end of a month"
    assert len(seen) == len(set(seen)), "a message was returned twice"
    print(f"\nmonth, filtered: {len(seen)} messages over {calls} calls")
