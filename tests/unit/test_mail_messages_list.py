"""`mail_messages_list`: headers over a date range, honest about what it read.

The fake mailbox answers SEARCH and FETCH in the shapes Yandex was seen to send;
see `conftest.py`. One row of the spec's matrix per test, named for the harm.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import anyio
import pytest
from conftest import FakeMailBox, FakeMessage, install_fake_mailbox, with_messages

from yandex_core.errors import NotFound, ProtocolError
from yandex_mail_mcp.client.imap_client import IMAPMailClient
from yandex_mail_mcp.tools.messages import (
    SCAN_BUDGET,
    TOOL_NAME,
    MessagePage,
    build_mail_messages_list,
)

MSK = timezone(timedelta(hours=3))
DAY = datetime(2026, 9, 20, 0, 0, tzinfo=MSK)


def _msgs(count, *, start=DAY, step=timedelta(minutes=10), **kwargs):
    return [
        FakeMessage(uid=100 + i, when=start + i * step, **kwargs) for i in range(count)
    ]


def _run(box, monkeypatch, **call):
    install_fake_mailbox(monkeypatch, box)

    async def password():
        return "app-password"

    client = IMAPMailClient(
        host="imap.yandex.ru",
        port=993,
        login="me@yandex.ru",
        password_provider=password,
    )

    async def provider():
        return client

    tool = build_mail_messages_list(provider)
    args = {"start": DAY.isoformat(), "end": (DAY + timedelta(days=1)).isoformat()}
    args.update(call)
    return anyio.run(lambda: tool(**args))


def _box(messages, **kwargs):
    return with_messages(FakeMailBox(), messages=messages, **kwargs)


# -- the range is required and must mean one thing ---------------------------------


@pytest.mark.parametrize("missing", ["start", "end"])
def test_a_missing_bound_is_refused_before_any_request(missing, monkeypatch):
    """FR2.2: no all-history default. A mailbox of years is not a default answer."""
    box = _box(_msgs(3))
    call = {"start": DAY.isoformat(), "end": (DAY + timedelta(days=1)).isoformat()}
    call[missing] = None

    with pytest.raises(ProtocolError):
        _run(box, monkeypatch, **call)
    assert box.client.searched == [], "the server was asked anyway"


def test_a_naive_instant_is_refused(monkeypatch):
    box = _box(_msgs(3))
    with pytest.raises(ProtocolError) as caught:
        _run(box, monkeypatch, start="2026-09-20T00:00:00")
    assert "offset" in str(caught.value).lower()


def test_an_inverted_range_is_refused(monkeypatch):
    box = _box(_msgs(3))
    with pytest.raises(ProtocolError):
        _run(
            box,
            monkeypatch,
            start=(DAY + timedelta(days=1)).isoformat(),
            end=DAY.isoformat(),
        )


# -- what the server is asked -------------------------------------------------------


def test_the_server_is_asked_for_dates_and_nothing_text_shaped(monkeypatch):
    """AD-12: filtering is ours. A filter given to the tool never reaches SEARCH."""
    box = _box(_msgs(3))

    _run(box, monkeypatch, subject_contains="встреча", from_contains="ivan")

    (criteria,) = box.client.searched
    words = {str(c).upper() for c in criteria if isinstance(c, str)}
    assert {"SINCE", "BEFORE"} <= words
    assert not words & {"SUBJECT", "FROM", "TEXT", "BODY", "CHARSET", "TO"}


def test_listing_never_marks_a_message_read(monkeypatch):
    """The folder is examined, not selected, and headers are PEEKed."""
    box = _box(_msgs(3))

    _run(box, monkeypatch)

    assert box.selections == [("INBOX", True)], "the folder was not opened read-only"
    for _, items in box.client.fetched:
        if "HEADER" in items:
            assert "BODY.PEEK[" in items, f"headers fetched without PEEK: {items}"


# -- the page -----------------------------------------------------------------------


def test_a_small_range_comes_back_whole_and_newest_first(monkeypatch):
    box = _box(_msgs(5))

    page = _run(box, monkeypatch)

    assert isinstance(page, MessagePage)
    assert [m.uid for m in page.items] == [104, 103, 102, 101, 100]
    assert page.complete is True
    assert page.next_cursor is None


def test_each_item_carries_what_the_story_promises(monkeypatch):
    box = _box(
        [
            FakeMessage(
                uid=7,
                when=DAY + timedelta(hours=2),
                subject="=?utf-8?B?0KDQtdC30YPQu9GM0YLQsNGC0Ysg0LLRgdGC0YDQtdGH0Lg=?=",
                sender="=?utf-8?B?0JjQstCw0L0g0J/QtdGC0YDQvtCy?= <ivan@example.ru>",
                flags=("encrypted",),
                size=4411,
                attachment=True,
            )
        ]
    )

    (item,) = _run(box, monkeypatch).items

    assert item.uid == 7
    assert item.subject == "Результаты встречи"
    assert item.sender.name == "Иван Петров"
    assert item.sender.address == "ivan@example.ru"
    assert item.to[0].address == "me@yandex.ru"
    assert item.size == 4411
    assert item.has_attachments is True
    assert item.unread is True
    assert datetime.fromisoformat(item.date) == DAY + timedelta(hours=2)
    assert item.folder == "INBOX"


def test_yandex_s_own_encrypted_keyword_does_not_make_mail_look_encrypted(monkeypatch):
    """Measured: Yandex puts `encrypted` on every message -- 275 of 275.

    It is a storage marker, not a property of the mail. `unread` is derived from
    `\\Seen` alone, and the keyword is left visible but explained.
    """
    box = _box(
        [
            FakeMessage(
                uid=1, when=DAY + timedelta(hours=1), flags=("\\Seen", "encrypted")
            )
        ]
    )

    (item,) = _run(box, monkeypatch).items

    assert item.unread is False
    assert "encrypted" in item.flags
    from yandex_mail_mcp.tools.messages import MessageSummary

    assert "encrypted" in MessageSummary.model_fields["flags"].description


def test_the_range_end_is_exclusive_to_the_second(monkeypatch):
    """SEARCH is day-granular; exact instants are filtered here."""
    end = DAY + timedelta(hours=5)
    box = _box(
        [
            FakeMessage(uid=1, when=DAY - timedelta(seconds=1)),  # just before start
            FakeMessage(uid=2, when=DAY),  # at start: in
            FakeMessage(uid=3, when=end - timedelta(seconds=1)),  # in
            FakeMessage(uid=4, when=end),  # at end: out
        ]
    )

    page = _run(box, monkeypatch, end=end.isoformat())

    assert [m.uid for m in page.items] == [3, 2]


def test_a_message_near_midnight_in_another_timezone_is_not_lost(monkeypatch):
    """The day window is widened, so a server comparing dates in its own zone
    cannot drop a message the exact range includes."""
    utc_start = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)  # 01:00 next day in MSK
    box = _box(
        [FakeMessage(uid=9, when=(utc_start + timedelta(minutes=30)).astimezone(MSK))]
    )

    page = _run(
        box,
        monkeypatch,
        start=utc_start.isoformat(),
        end=(utc_start + timedelta(hours=1)).isoformat(),
    )

    assert [m.uid for m in page.items] == [9]


def test_an_empty_range_is_an_empty_complete_page(monkeypatch):
    box = _box([])

    page = _run(box, monkeypatch)

    assert page.items == []
    assert page.complete is True


# -- more than fits in one answer ----------------------------------------------------


def test_more_than_limit_is_cut_with_a_cursor(monkeypatch):
    box = _box(_msgs(12))

    page = _run(box, monkeypatch, limit=5)

    assert [m.uid for m in page.items] == [111, 110, 109, 108, 107]
    assert page.complete is False
    assert page.next_cursor


def test_following_the_cursor_reaches_every_message_exactly_once(monkeypatch):
    box = _box(_msgs(12))
    seen, cursor = [], None
    for _ in range(10):
        page = _run(box, monkeypatch, limit=5, cursor=cursor)
        seen += [m.uid for m in page.items]
        if page.complete:
            break
        cursor = page.next_cursor

    assert sorted(seen) == list(range(100, 112))
    assert len(seen) == len(set(seen)), "a message was returned twice"


def test_a_filter_over_more_than_the_budget_is_not_called_complete(monkeypatch):
    """The harm this story is about: a filtered page that read part of the range
    and said nothing. Reading headers costs ~40 ms a message cold, measured, so
    one call reads at most SCAN_BUDGET of them -- and says it stopped."""
    box = _box(_msgs(SCAN_BUDGET + 30, step=timedelta(seconds=30)))

    page = _run(box, monkeypatch, subject_contains="nothing matches this")

    assert page.items == []
    assert page.complete is False, "a partial scan was reported as the whole answer"
    assert page.next_cursor
    assert page.remaining == 30


def test_a_filtered_scan_continues_exactly_where_it_stopped(monkeypatch):
    """Newest first: the first call reads the newest SCAN_BUDGET, the second the rest."""
    msgs = _msgs(SCAN_BUDGET + 30, step=timedelta(seconds=30))
    newest_match, oldest_match = msgs[-5], msgs[10]
    newest_match.subject = "Р-Фарм Аккорд: итоги"
    oldest_match.subject = "итоги по Р-Фарм"
    box = _box(msgs)

    first = _run(box, monkeypatch, subject_contains="р-фарм")
    second = _run(box, monkeypatch, subject_contains="р-фарм", cursor=first.next_cursor)

    assert [m.uid for m in first.items] == [newest_match.uid]
    assert first.complete is False
    assert [m.uid for m in second.items] == [oldest_match.uid]
    assert second.complete is True
    assert second.remaining == 0


def test_filters_match_decoded_text_case_insensitively(monkeypatch):
    box = _box(
        [
            FakeMessage(
                uid=1,
                when=DAY + timedelta(hours=1),
                subject="=?utf-8?B?0KAt0KTQsNGA0Lw=?= =?utf-8?B?INCQ0LrQutC+0YDQtA==?=",
            ),
            FakeMessage(uid=2, when=DAY + timedelta(hours=2), subject="Unrelated"),
            FakeMessage(
                uid=3,
                when=DAY + timedelta(hours=3),
                sender="=?utf-8?B?0JjQstCw0L0g0J/QtdGC0YDQvtCy?= <ivan@example.ru>",
            ),
        ]
    )

    assert [m.uid for m in _run(box, monkeypatch, subject_contains="АККОРД").items] == [
        1
    ]
    assert [m.uid for m in _run(box, monkeypatch, from_contains="петров").items] == [3]
    assert [m.uid for m in _run(box, monkeypatch, from_contains="IVAN@").items] == [3]


# -- cursors that no longer mean what they meant -------------------------------------


def test_a_cursor_after_the_folder_was_renumbered_is_refused(monkeypatch):
    """UIDVALIDITY changing means every UID was reassigned. Continuing would skip
    or repeat messages with nothing to show for it."""
    box = _box(_msgs(12))
    first = _run(box, monkeypatch, limit=5)

    renumbered = _box(_msgs(12), uidvalidity=999)
    with pytest.raises(ProtocolError) as caught:
        _run(renumbered, monkeypatch, limit=5, cursor=first.next_cursor)
    assert "again" in str(caught.value).lower()


def test_a_cursor_for_a_different_question_is_refused(monkeypatch):
    box = _box(_msgs(12))
    first = _run(box, monkeypatch, limit=5)

    with pytest.raises(ProtocolError):
        _run(box, monkeypatch, limit=5, cursor=first.next_cursor, subject_contains="x")


def test_a_cursor_from_another_tool_is_refused(monkeypatch):
    from yandex_core.paging import encode_cursor

    box = _box(_msgs(3))
    with pytest.raises(ProtocolError):
        _run(box, monkeypatch, cursor=encode_cursor({"below": 1}, tool="calendar_list"))


# -- attachments and failures ---------------------------------------------------------


def test_structure_is_fetched_only_for_the_messages_returned(monkeypatch):
    """BODYSTRUCTURE is the dearest item per message, measured. Only what is
    returned pays for it."""
    box = _box(_msgs(12))

    page = _run(box, monkeypatch, limit=3)

    structure_fetches = [
        u for u, items in box.client.fetched if "BODYSTRUCTURE" in items
    ]
    (asked,) = structure_fetches
    asked_uids = {
        int(x)
        for x in (asked.decode() if isinstance(asked, bytes) else asked).split(",")
    }
    assert asked_uids == {m.uid for m in page.items}


def test_an_unreadable_structure_is_unknown_with_a_reason_not_false(monkeypatch):
    box = _box(
        [FakeMessage(uid=1, when=DAY + timedelta(hours=1), structure=b'"broken"')]
    )

    (item,) = _run(box, monkeypatch).items

    assert item.has_attachments is None
    assert item.attachments_note


def test_an_unknown_folder_is_not_found_and_names_the_listing_tool(monkeypatch):
    box = _box(_msgs(3))

    with pytest.raises(NotFound) as caught:
        _run(box, monkeypatch, folder="No-Such-Folder")
    assert "mail_folders_list" in str(caught.value)


def test_the_tool_is_registered_read_only():
    from yandex_core.risk import RISK_REGISTRY, RiskClass

    assert RISK_REGISTRY[TOOL_NAME] is RiskClass.READ


def test_a_message_stored_in_a_zone_behind_the_query_is_not_lost(monkeypatch):
    """The dangerous direction of the day-granular search.

    The range starts 00:30 Moscow on the 21st. A message received at 00:45 Moscow
    but stored by the server with a -05:00 offset carries the date of the 20th
    there. A SEARCH window starting on the 21st drops it before this code ever
    sees it -- silently. Widening the window by a day is what keeps it.
    """
    start = datetime(2026, 9, 21, 0, 30, tzinfo=MSK)
    stored = (start + timedelta(minutes=15)).astimezone(timezone(timedelta(hours=-5)))
    assert stored.date() < start.date()  # the premise of the test
    box = _box([FakeMessage(uid=42, when=stored)])

    page = _run(
        box,
        monkeypatch,
        start=start.isoformat(),
        end=(start + timedelta(hours=1)).isoformat(),
    )

    assert [m.uid for m in page.items] == [42]
