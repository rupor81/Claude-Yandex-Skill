"""`mail_message_get`: one message's text, and an honest account of how much.

The fake serves BODY.PEEK[section] with raw, still transfer-encoded bytes, as the
server does, so decoding is exercised end to end.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from conftest import FakeMailBox, FakeMessage, install_fake_mailbox, with_messages

from yandex_core.errors import NotFound, ProtocolError
from yandex_core.results import Chunk
from yandex_mail_mcp.tools.message import (
    DEFAULT_MAX_CHARS,
    TOOL_NAME,
    build_mail_message_get,
)

MSK = timezone(timedelta(hours=3))
WHEN = datetime(2026, 9, 24, 15, 0, tzinfo=MSK)

PLAIN = b'("text" "plain" ("charset" "utf-8") NIL NIL "base64" 900 20 NIL NIL NIL NIL)'
EMPTY_PLAIN_FIRST = (
    b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 0 1 NIL NIL NIL NIL)'
    b'("text" "plain" ("charset" "utf-8") NIL NIL "base64" 900 20 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "~") NIL NIL NIL)'
)
HTML_ONLY = (
    b'("text" "html" ("charset" "koi8-r") NIL NIL "base64" 900 20 NIL NIL NIL NIL)'
)
ATTACHMENT_ONLY = (
    b'(("application" "pdf" ("name" "~") NIL NIL "base64" 88120 NIL ("attachment" ("filename" "~")) NIL NIL)'
    b' "mixed" ("boundary" "~") NIL NIL NIL)'
)
BLANK_PLAIN_THEN_HTML = (
    b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 4 1 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "utf-8") NIL NIL "7bit" 60 2 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "~") NIL NIL NIL)'
)


def _b64(text: str, charset: str = "utf-8") -> bytes:
    return base64.b64encode(text.encode(charset))


def _message(uid=7, structure=PLAIN, parts=None, **kwargs):
    return FakeMessage(
        uid=uid,
        when=WHEN,
        subject="=?utf-8?B?0JjRgtC+0LPQuCDQstGB0YLRgNC10YfQuA==?=",
        structure=structure,
        parts=parts if parts is not None else {"1": _b64("Короткое письмо.")},
        **kwargs,
    )


def _get(box, monkeypatch, **call):
    install_fake_mailbox(monkeypatch, box)

    async def password():
        return "app-password"

    from yandex_mail_mcp.client.imap_client import IMAPMailClient

    client = IMAPMailClient(
        host="imap.yandex.ru",
        port=993,
        login="me@yandex.ru",
        password_provider=password,
    )

    async def provider():
        return client

    args = {"uid": 7}
    args.update(call)
    return anyio.run(lambda: build_mail_message_get(provider)(**args))


def _box(*messages, **kwargs):
    return with_messages(FakeMailBox(), messages=messages, **kwargs)


# -- the plain case --------------------------------------------------------------------


def test_a_short_message_comes_back_whole(monkeypatch):
    result = _get(_box(_message()), monkeypatch)

    assert isinstance(result, Chunk)
    assert result.text.strip() == "Короткое письмо."
    assert result.complete is True
    assert result.next_cursor is None
    assert result.format == "plain"
    assert result.subject == "Итоги встречи"
    assert result.uid == 7


def test_reading_never_marks_the_message_read(monkeypatch):
    box = _box(_message())

    _get(box, monkeypatch)

    assert box.selections == [("INBOX", True)]
    for _, items in box.client.fetched:
        assert "BODY[" not in items.replace("BODY.PEEK[", ""), f"not a PEEK: {items}"


def test_only_the_text_part_is_fetched_never_the_whole_message(monkeypatch):
    """Measured: a 28 MB message's text is 14 KB in 43 ms; the whole is 2.4 s."""
    box = _box(
        _message(
            structure=b'(("text" "plain" ("charset" "utf-8") NIL NIL "base64" 900 20 NIL NIL NIL NIL)("application" "pdf" ("name" "~") NIL NIL "base64" 28000000 NIL ("attachment" ("filename" "~")) NIL NIL) "mixed" ("boundary" "~") NIL NIL NIL)',
            parts={"1": _b64("Текст.")},
        )
    )

    _get(box, monkeypatch)

    fetched = [items for _, items in box.client.fetched]
    assert "BODY.PEEK[]" not in " ".join(fetched), "the whole message was fetched"
    assert any("BODY.PEEK[1]" in items for items in fetched)


# -- which part --------------------------------------------------------------------------


def test_an_empty_plain_part_first_does_not_make_an_empty_letter(monkeypatch):
    box = _box(
        _message(
            structure=EMPTY_PLAIN_FIRST, parts={"1": b"", "2": _b64("Настоящий текст.")}
        )
    )

    result = _get(box, monkeypatch)

    assert result.text.strip() == "Настоящий текст."


def test_a_blank_plain_part_falls_back_to_the_html(monkeypatch):
    box = _box(
        _message(
            structure=BLANK_PLAIN_THEN_HTML,
            parts={"1": b"  \r\n", "2": "<p>Из HTML</p>".encode()},
        )
    )

    result = _get(box, monkeypatch)

    assert result.text.strip() == "Из HTML"
    assert result.format == "html"


def test_html_only_mail_is_rendered_and_says_so(monkeypatch):
    """60% of the operator's mail, measured."""
    html = "<html><head><style>p{}</style></head><body><p>Итоги:</p><ul><li>срок</li></ul></body></html>"
    box = _box(_message(structure=HTML_ONLY, parts={"1": _b64(html, "koi8-r")}))

    result = _get(box, monkeypatch)

    assert "Итоги:" in result.text and "- срок" in result.text
    assert "<" not in result.text
    assert result.format == "html"
    assert any("HTML" in note for note in result.notes)


def test_a_message_with_no_text_says_so_rather_than_looking_blank(monkeypatch):
    box = _box(_message(structure=ATTACHMENT_ONLY, parts={}))

    result = _get(box, monkeypatch)

    assert result.text == ""
    assert result.format == "none"
    assert result.complete is True
    assert result.notes, "an empty text came back with no explanation"


# -- truncation ------------------------------------------------------------------------------


def _long(words=4000):
    return " ".join(f"слово{i}" for i in range(words))


def test_a_long_message_is_cut_with_a_marker_and_a_cursor(monkeypatch):
    box = _box(_message(parts={"1": _b64(_long())}))

    result = _get(box, monkeypatch, max_chars=2000)

    assert result.complete is False
    assert result.next_cursor
    assert "truncated" in result.text.lower()
    assert f"of {result.total_chars}" in result.text


def test_the_cut_falls_between_words(monkeypatch):
    box = _box(_message(parts={"1": _b64(_long())}))

    result = _get(box, monkeypatch, max_chars=2000)

    body = result.text.split("\n\n[")[0]
    assert body.rstrip().split()[-1].startswith("слово")
    assert body.rstrip().split()[-1][len("слово") :].isdigit(), "a word was cut in half"


def test_following_cursors_reassembles_the_exact_text(monkeypatch):
    """NFR2: nothing skipped, nothing repeated, across every segment."""
    original = _long(3000)
    box = _box(_message(parts={"1": _b64(original)}))
    pieces, cursor = [], None
    for _ in range(50):
        result = _get(box, monkeypatch, max_chars=1500, cursor=cursor)
        pieces.append(
            result.text.split("\n\n[... truncated")[0]
            if not result.complete
            else result.text
        )
        if result.complete:
            break
        cursor = result.next_cursor

    assert "".join(pieces).strip() == original


def test_the_default_limit_returns_most_messages_whole(monkeypatch):
    """Measured p90 of plain text is 11k characters; the default covers it."""
    assert DEFAULT_MAX_CHARS >= 12_000
    box = _box(_message(parts={"1": _b64("x " * 5500)}))
    assert _get(box, monkeypatch).complete is True


# -- stripping quotes ------------------------------------------------------------------------


def test_quotes_are_kept_unless_asked(monkeypatch):
    body = 'Согласен.\n\n25.09.2026, 10:00, "Иван" <ivan@example.ru>:\n> Предлагаю.\n'
    box = _box(_message(parts={"1": _b64(body)}))

    result = _get(box, monkeypatch)

    assert "Предлагаю" in result.text


def test_stripping_says_how_much_was_removed(monkeypatch):
    body = 'Согласен.\n\n25.09.2026, 10:00, "Иван" <ivan@example.ru>:\n> Предлагаю.\n'
    box = _box(_message(parts={"1": _b64(body)}))

    result = _get(box, monkeypatch, strip_quotes=True)

    assert result.text.strip() == "Согласен."
    assert any("removed" in note for note in result.notes), "content vanished silently"


# -- what cannot be answered -------------------------------------------------------------


def test_an_unknown_uid_is_not_found_and_names_the_listing_tool(monkeypatch):
    with pytest.raises(NotFound) as caught:
        _get(_box(_message(uid=7)), monkeypatch, uid=8)
    assert "mail_messages_list" in str(caught.value)


def test_a_cursor_for_another_message_is_refused(monkeypatch):
    box = _box(
        _message(uid=7, parts={"1": _b64(_long())}),
        _message(uid=8, parts={"1": _b64(_long())}),
    )
    first = _get(box, monkeypatch, uid=7, max_chars=2000)

    with pytest.raises(ProtocolError):
        _get(box, monkeypatch, uid=8, cursor=first.next_cursor)


def test_a_cursor_with_quotes_toggled_is_refused(monkeypatch):
    """Offsets into the stripped text mean nothing in the unstripped one."""
    box = _box(_message(parts={"1": _b64(_long())}))
    first = _get(box, monkeypatch, max_chars=2000)

    with pytest.raises(ProtocolError):
        _get(box, monkeypatch, strip_quotes=True, cursor=first.next_cursor)


def test_a_cursor_after_the_folder_was_renumbered_is_refused(monkeypatch):
    first = _get(
        _box(_message(parts={"1": _b64(_long())})), monkeypatch, max_chars=2000
    )

    with pytest.raises(ProtocolError):
        _get(
            _box(_message(parts={"1": _b64(_long())}), uidvalidity=5),
            monkeypatch,
            cursor=first.next_cursor,
        )


def test_the_tool_is_registered_read_only():
    from yandex_core.risk import RISK_REGISTRY, RiskClass

    assert RISK_REGISTRY[TOOL_NAME] is RiskClass.READ
