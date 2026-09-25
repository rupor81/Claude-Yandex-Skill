"""The IMAP client on its own, without a tool in front of it.

`tools/` happens to filter containers out before asking for counts, which means
the client's own guard against them is invisible from there -- a mutation proved
it. A client is a thing another caller may use, so what it promises is tested
where it is promised.
"""

from __future__ import annotations

import anyio
import pytest
from conftest import FakeFolderInfo, FakeMailBox, install_fake_mailbox

from yandex_core.errors import ProtocolError
from yandex_mail_mcp.client.imap_client import IMAPMailClient

LOGIN = "me@yandex.ru"


def _client(**kwargs):
    async def password_provider():
        return "app-password"

    return IMAPMailClient(
        host="imap.yandex.ru",
        port=993,
        login=LOGIN,
        password_provider=password_provider,
        **kwargs,
    )


def _folders(box, monkeypatch, count_for):
    install_fake_mailbox(monkeypatch, box)
    client = _client()
    return anyio.run(lambda: client.list_folders(count_for=count_for))


def test_a_container_is_never_asked_for_counts_even_when_it_is_named(monkeypatch):
    """A `\\Noselect` folder cannot be selected, so STATUS on it is a wasted request.

    The rule is a fact about IMAP, not about paging, so the client holds it for
    every caller rather than trusting each one to filter first.
    """
    box = FakeMailBox(
        folders=[
            FakeFolderInfo("Проекты", flags=("\\Noselect", "\\HasChildren")),
            FakeFolderInfo("Проекты|Аккорд"),
        ],
        status_by_folder={"Проекты|Аккорд": {"MESSAGES": 4, "UNSEEN": 1}},
    )

    folders = _folders(box, monkeypatch, lambda listed: [f.name for f in listed])

    assert box.statused == ["Проекты|Аккорд"], f"asked {box.statused}"
    container = next(f for f in folders if f.name == "Проекты")
    assert container.selectable is False
    assert container.messages is None


def test_a_name_the_listing_did_not_contain_costs_nothing(monkeypatch):
    """A stale cursor can name a folder that has since been renamed away."""
    box = FakeMailBox(folders=["INBOX"], status_by_folder={"INBOX": {"MESSAGES": 1}})

    folders = _folders(box, monkeypatch, lambda listed: ["INBOX", "Gone"])

    assert box.statused == ["INBOX"]
    assert [f.name for f in folders] == ["INBOX"]


def test_the_selector_sees_every_folder_before_any_is_dropped(monkeypatch):
    """Paging decides the window, so it has to be shown the whole listing."""
    seen: list = []
    box = FakeMailBox(folders=["A", "B", "C"])

    _folders(box, monkeypatch, lambda listed: seen.extend(f.name for f in listed) or [])

    assert seen == ["A", "B", "C"]
    assert box.statused == [], "counts were taken for folders nobody chose"


def test_a_server_that_will_not_list_is_an_error_not_a_mailbox_with_no_folders(
    monkeypatch,
):
    from imap_tools.errors import ImapToolsError

    box = FakeMailBox(list_raises=ImapToolsError("LIST refused"))

    with pytest.raises(ProtocolError) as caught:
        _folders(box, monkeypatch, lambda listed: [])

    assert LOGIN in str(caught.value)


def test_the_connection_is_closed_even_when_the_listing_fails(monkeypatch):
    """One TLS connection per call, and it is not left to the collector."""
    from imap_tools.errors import ImapToolsError

    box = FakeMailBox(list_raises=ImapToolsError("LIST refused"))
    with pytest.raises(ProtocolError):
        _folders(box, monkeypatch, lambda listed: [])
    assert box.logged_out == 1

    box = FakeMailBox(folders=["INBOX"])
    _folders(box, monkeypatch, lambda listed: [])
    assert box.logged_out == 1
