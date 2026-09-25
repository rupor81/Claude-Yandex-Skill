"""`mail_folders_list`: the vertical slice that proves the mailbox is reachable.

What it asserts is the contract, never a claim about the operator's mailbox.
"""

from __future__ import annotations

import anyio
import pytest
from conftest import FakeFolderInfo, FakeMailBox, install_fake_mailbox
from imap_tools.errors import MailboxFolderStatusError, MailboxLoginError

from yandex_core.errors import (
    AuthError,
    PolicyError,
    ProtocolError,
    TransportError,
)
from yandex_core.results import Page
from yandex_mail_mcp.client.imap_client import IMAPMailClient
from yandex_mail_mcp.tools.folders import (
    NO_COUNTS_NOT_SELECTABLE,
    TOOL_NAME,
    FolderSummary,
    build_mail_folders_list,
)

LOGIN = "me@yandex.ru"

#: The names a real Russian mailbox has, as `imap_tools` hands them over --
#: already decoded from modified UTF-7, which was verified by roundtrip.
RUSSIAN_FOLDERS = ("Входящие", "Отправленные", "Спам", "Удалённые", "Черновики")


def _client(box_token="app-password", **kwargs):
    tokens: list = []

    async def password_provider():
        tokens.append(box_token)
        return box_token

    client = IMAPMailClient(
        host="imap.yandex.ru",
        port=993,
        login=LOGIN,
        password_provider=password_provider,
        **kwargs,
    )
    return client, tokens


def _list(box, monkeypatch, **kwargs):
    built = install_fake_mailbox(monkeypatch, box)
    client, tokens = _client()

    async def provider():
        return client

    tool = build_mail_folders_list(provider)
    page = anyio.run(lambda: tool(**kwargs))
    return page, built, tokens


# -- the plain case -------------------------------------------------------


def test_folders_come_back_as_a_page_with_their_counts(monkeypatch):
    box = FakeMailBox(
        folders=["INBOX", "Sent"],
        status_by_folder={
            "INBOX": {"MESSAGES": 1420, "UNSEEN": 7},
            "Sent": {"MESSAGES": 311, "UNSEEN": 0},
        },
    )

    page, built, _ = _list(box, monkeypatch)

    assert isinstance(page, Page)
    assert page.complete is True
    assert page.next_cursor is None
    by_name = {item.name: item for item in page.items}
    assert by_name["INBOX"].messages == 1420
    assert by_name["INBOX"].unseen == 7
    assert by_name["Sent"].messages == 311
    assert built == [{"host": "imap.yandex.ru", "port": 993, "timeout": 30}]


def test_cyrillic_folder_names_arrive_as_readable_text(monkeypatch):
    """The library decodes modified UTF-7; this holds it to that.

    If it ever stops, the operator sees `&BBIERQQ-BDQETwRJBDgENQ-` and has no
    way to tell whether that is their folder or a bug.
    """
    box = FakeMailBox(
        folders=list(RUSSIAN_FOLDERS),
        status_by_folder={
            name: {"MESSAGES": 1, "UNSEEN": 0} for name in RUSSIAN_FOLDERS
        },
    )

    page, _, _ = _list(box, monkeypatch, limit=10)

    assert {item.name for item in page.items} == set(RUSSIAN_FOLDERS)
    assert all("&" not in item.name for item in page.items)


def test_the_hierarchy_delimiter_is_reported_so_a_path_can_be_built(monkeypatch):
    """Yandex uses `|`, not `/`. A caller that guesses will address nothing."""
    box = FakeMailBox(folders=[FakeFolderInfo("Проекты|Аккорд", delim="|")])

    page, _, _ = _list(box, monkeypatch)

    assert page.items[0].delimiter == "|"


# -- authentication, which must never look like an empty mailbox ----------


def test_the_mailbox_signs_in_with_the_stored_app_password(monkeypatch):
    """Read per call, through the provider -- never held by the client."""
    box = FakeMailBox(folders=["INBOX"])

    _, _, tokens = _list(box, monkeypatch)

    assert tokens == ["app-password"], "the password provider was not consulted"
    assert box.authenticated_as == (LOGIN, "app-password")


def test_no_folder_is_selected_just_to_list_folders(monkeypatch):
    """`imap_tools` defaults `initial_folder='INBOX'`, which SELECTs it.

    Listing folders needs no mailbox selected, and this account's request budget
    is shared and scarce -- epic 1 measured that. A SELECT nobody asked for is
    a round trip nobody asked for, and it fails on an account with no INBOX.
    """
    box = FakeMailBox(folders=["INBOX"])

    _list(box, monkeypatch)

    assert box.initial_folder is None
    assert box.selected == [], f"selected {box.selected} to list folders"


def test_a_refused_login_is_an_error_not_a_mailbox_with_no_folders(monkeypatch):
    """Zero folders is an answer. This server may not invent it."""
    box = FakeMailBox(
        login_raises=MailboxLoginError(("NO", [b"AUTHENTICATIONFAILED"]), "NO")
    )

    with pytest.raises(AuthError):
        _list(box, monkeypatch)


def test_an_organisation_that_forbids_external_clients_says_so(monkeypatch):
    """FR4.5: 'bad credential' sends the operator to fix the wrong thing."""
    box = FakeMailBox(
        login_raises=MailboxLoginError(
            ("NO", [b"Application is disabled by the organization policy"]), "NO"
        )
    )

    with pytest.raises(PolicyError) as caught:
        _list(box, monkeypatch)

    lowered = str(caught.value).lower()
    assert "organisation" in lowered or "organization" in lowered


def test_an_unreachable_host_is_a_transport_error_not_an_empty_page(monkeypatch):
    box = FakeMailBox(connect_raises=OSError("Network is unreachable"))

    with pytest.raises(TransportError) as caught:
        _list(box, monkeypatch)

    assert "imap.yandex.ru" in str(caught.value)


def test_no_password_reaches_an_error_message(monkeypatch):
    box = FakeMailBox(
        login_raises=MailboxLoginError(("NO", [b"AUTHENTICATIONFAILED"]), "NO")
    )
    install_fake_mailbox(monkeypatch, box)
    client, _ = _client(box_token="SECRET-APP-PASSWORD")

    async def provider():
        return client

    tool = build_mail_folders_list(provider)
    with pytest.raises(AuthError) as caught:
        anyio.run(lambda: tool())

    assert "SECRET-APP-PASSWORD" not in str(caught.value)


# -- counts cost one request each, so the page is the bound ---------------


def test_counts_are_taken_only_for_the_folders_the_page_returns(monkeypatch):
    """Measured: `folder.status()` is one request per folder.

    A mailbox with fifty folders must not pay fifty requests to answer a call
    that returns five of them.
    """
    names = [f"Folder{index:02d}" for index in range(20)]
    box = FakeMailBox(
        folders=names,
        status_by_folder={name: {"MESSAGES": 1, "UNSEEN": 0} for name in names},
    )

    page, _, _ = _list(box, monkeypatch, limit=5)

    assert len(page.items) == 5
    assert page.complete is False
    assert page.next_cursor is not None
    assert box.statused == [item.name for item in page.items], (
        f"asked {len(box.statused)} folders for counts to return {len(page.items)}"
    )


def test_a_cursor_resumes_after_the_named_folder_not_at_an_index(monkeypatch):
    """A mailbox that gains a folder between pages must not drop or repeat one."""
    names = [f"Folder{index:02d}" for index in range(6)]
    box = FakeMailBox(
        folders=names, status_by_folder={name: {"MESSAGES": 0} for name in names}
    )

    first, _, _ = _list(box, monkeypatch, limit=3)
    second, _, _ = _list(box, monkeypatch, limit=3, cursor=first.next_cursor)

    assert [item.name for item in first.items] == names[:3]
    assert [item.name for item in second.items] == names[3:]
    assert second.complete is True


def test_the_order_is_this_server_s_own_because_imap_offers_no_sort(monkeypatch):
    """Measured: the CAPABILITY line carries no SORT. Order is ours or nothing."""
    box = FakeMailBox(folders=["Zebra", "alpha", "INBOX"])

    page, _, _ = _list(box, monkeypatch)

    assert [item.name for item in page.items] == sorted(["Zebra", "alpha", "INBOX"])


def test_a_cursor_from_another_tool_is_refused(monkeypatch):
    box = FakeMailBox(folders=["INBOX"])
    from yandex_core.paging import encode_position_cursor

    foreign = encode_position_cursor({"name": "INBOX"}, tool="calendar_list")

    with pytest.raises(ProtocolError):
        _list(box, monkeypatch, cursor=foreign)


# -- counts that cannot be had are absent, never zero ---------------------


def test_a_container_folder_is_listed_with_its_counts_absent(monkeypatch):
    """`\\Noselect` is a node in the hierarchy, not a mailbox.

    Answering zero would be this server inventing a fact about the operator's
    mail, and a caller told "0 messages" stops looking.
    """
    box = FakeMailBox(
        folders=[
            FakeFolderInfo("Проекты", flags=("\\Noselect", "\\HasChildren")),
            FakeFolderInfo("Проекты|Аккорд"),
        ],
        status_by_folder={"Проекты|Аккорд": {"MESSAGES": 12, "UNSEEN": 3}},
    )

    page, _, _ = _list(box, monkeypatch)

    container = next(item for item in page.items if item.name == "Проекты")
    assert container.messages is None
    assert container.unseen is None
    assert container.counts_note == NO_COUNTS_NOT_SELECTABLE
    assert container.selectable is False
    assert "Проекты" not in box.statused, "a container was asked for a count anyway"


def test_a_folder_whose_status_fails_is_still_listed_with_the_reason(monkeypatch):
    """One folder refusing STATUS must not cost the caller the whole page."""
    box = FakeMailBox(
        folders=["INBOX", "Broken"],
        status_by_folder={"INBOX": {"MESSAGES": 5, "UNSEEN": 1}},
        status_raises={
            "Broken": MailboxFolderStatusError(("NO", [b"SERVERBUG"]), "NO")
        },
    )

    page, _, _ = _list(box, monkeypatch)

    by_name = {item.name: item for item in page.items}
    assert by_name["INBOX"].messages == 5
    assert by_name["Broken"].messages is None
    assert by_name["Broken"].counts_note, "the folder is silent about why"
    assert by_name["Broken"].selectable is True


def test_a_status_answer_missing_a_key_is_absent_rather_than_zero(monkeypatch):
    box = FakeMailBox(folders=["INBOX"], status_by_folder={"INBOX": {"MESSAGES": 9}})

    page, _, _ = _list(box, monkeypatch)

    assert page.items[0].messages == 9
    assert page.items[0].unseen is None


# -- the contract itself --------------------------------------------------


def test_the_tool_is_named_and_shaped_the_way_the_registry_expects():
    from yandex_core.risk import RISK_REGISTRY, RiskClass

    assert TOOL_NAME == "mail_folders_list"
    assert RISK_REGISTRY[TOOL_NAME] is RiskClass.READ


def test_a_folder_summary_says_what_it_does_not_know():
    """Fields that may be absent are optional in the schema, not defaulted to 0."""
    fields = FolderSummary.model_fields
    for name in ("messages", "unseen"):
        assert fields[name].default is None, f"{name} defaults to something"


def test_a_client_that_ignores_the_window_is_refused_rather_than_answered(monkeypatch):
    """The window is how both the limit and the request cost are bounded.

    A client that never consults it fetched counts for everything or for
    nothing. Re-deriving the window here would hand back a page that looks
    correct while neither bound was ever applied.
    """

    class IgnoresTheSelector:
        async def list_folders(self, *, count_for):
            return []

    async def provider():
        return IgnoresTheSelector()

    tool = build_mail_folders_list(provider)
    with pytest.raises(ProtocolError) as caught:
        anyio.run(lambda: tool())

    assert "selector" in str(caught.value).lower()


def test_a_refused_login_names_both_causes_yandex_gives(monkeypatch):
    """Measured live: Yandex answers one message for two different problems.

    "[AUTHENTICATIONFAILED] LOGIN invalid credentials or IMAP is disabled" -- and
    the two fixes are in different places. An app password created for another
    service (Calendar, say) is refused by IMAP; IMAP access itself is a switch in
    the mailbox's settings. Naming only one sends the operator to re-create a
    password that was never the problem, or the other way round.
    """
    box = FakeMailBox(
        login_raises=MailboxLoginError(
            (
                "NO",
                [
                    b"[AUTHENTICATIONFAILED] LOGIN invalid credentials or IMAP is disabled"
                ],
            ),
            "NO",
        )
    )

    with pytest.raises(AuthError) as caught:
        _list(box, monkeypatch)

    message = str(caught.value)
    assert "Почта" in message or "Mail" in message, "the password type is not named"
    assert "IMAP" in message, "the IMAP switch is not named"
    assert "yandex-mcp setup mail" in message
