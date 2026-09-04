"""`Page` cannot be silently incomplete, and cursors cannot be guessed at."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from yandex_core.errors import ProtocolError
from yandex_core.paging import decode_cursor, encode_cursor
from yandex_core.results import Page


def test_completeness_fields_are_required():
    with pytest.raises(ValidationError):
        Page[str](items=["a"])
    with pytest.raises(ValidationError):
        Page[str](items=["a"], complete=True)
    with pytest.raises(ValidationError):
        Page[str](items=["a"], next_cursor=None)


def test_whole_page_is_complete_with_no_cursor():
    page = Page[str].whole(["a", "b"])
    assert page.complete is True
    assert page.next_cursor is None


def test_cursor_round_trips_and_is_opaque():
    cursor = encode_cursor({"offset": 7}, tool="calendar_list")
    assert "offset" not in cursor
    assert "=" not in cursor
    assert decode_cursor(cursor, tool="calendar_list") == {"offset": 7}


@pytest.mark.parametrize("bad", ["", "not-a-cursor", "!!!!", "eyJ2Ijo5OSwicCI6e319"])
def test_foreign_cursors_are_rejected(bad):
    with pytest.raises(ProtocolError):
        decode_cursor(bad, tool="calendar_list")


def test_a_cursor_minted_by_another_tool_is_refused():
    """Without an issuer, one listing's offset would be honoured by another."""
    cursor = encode_cursor({"offset": 7}, tool="event_list")
    with pytest.raises(ProtocolError) as caught:
        decode_cursor(cursor, tool="calendar_list")
    assert "calendar_list" in str(caught.value)


def test_a_cursor_with_no_issuer_at_all_is_refused():
    """A payload shaped like ours but unstamped is still not ours."""
    import base64
    import json

    raw = json.dumps({"v": 1, "p": {"offset": 7}}).encode("utf-8")
    forged = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    with pytest.raises(ProtocolError):
        decode_cursor(forged, tool="calendar_list")


def test_the_same_tool_still_reads_its_own_cursor():
    for tool in ("calendar_list", "event_list"):
        assert decode_cursor(encode_cursor({"offset": 1}, tool=tool), tool=tool) == {
            "offset": 1
        }
