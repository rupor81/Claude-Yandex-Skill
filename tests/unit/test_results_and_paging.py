"""`Page` cannot be silently incomplete, and cursors cannot be guessed at."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from yandex_core.errors import ProtocolError
from yandex_core.paging import (
    decode_cursor,
    decode_position_cursor,
    encode_cursor,
    encode_position_cursor,
)
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


# -- position cursors ------------------------------------------------------

POSITION_FIELDS = ("start", "uid", "recurrence_id")


def position(**overrides):
    base = {"start": "2026-06-08T09:00:00+03:00", "uid": "standup-1", "recurrence_id": None}
    base.update(overrides)
    return base


def test_a_position_cursor_names_the_item_rather_than_an_offset():
    """The point of the shape: a derived set can shift without invalidating it."""
    cursor = encode_position_cursor(position(), tool="calendar_events_list")

    decoded = decode_position_cursor(
        cursor, tool="calendar_events_list", fields=POSITION_FIELDS
    )

    assert decoded == position()
    assert "offset" not in decode_cursor(cursor, tool="calendar_events_list")


def test_a_position_cursor_is_opaque():
    cursor = encode_position_cursor(position(), tool="calendar_events_list")
    assert "standup-1" not in cursor


def test_a_position_cursor_still_carries_its_issuing_tool():
    cursor = encode_position_cursor(position(), tool="calendar_events_list")
    with pytest.raises(ProtocolError):
        decode_position_cursor(cursor, tool="calendar_list", fields=POSITION_FIELDS)


def test_an_index_cursor_is_not_accepted_as_a_position():
    cursor = encode_cursor({"offset": 3}, tool="calendar_events_list")
    with pytest.raises(ProtocolError):
        decode_position_cursor(
            cursor, tool="calendar_events_list", fields=POSITION_FIELDS
        )


def test_a_position_cursor_of_a_different_shape_is_refused():
    """A cursor from another version of the tool resumes from nowhere trustworthy."""
    cursor = encode_position_cursor({"uid": "standup-1"}, tool="calendar_events_list")
    with pytest.raises(ProtocolError):
        decode_position_cursor(
            cursor, tool="calendar_events_list", fields=POSITION_FIELDS
        )


def test_a_non_string_position_field_is_refused_at_encoding():
    with pytest.raises(ProtocolError):
        encode_position_cursor({"uid": 7}, tool="calendar_events_list")


# -- the shared limit check ------------------------------------------------


def test_checked_limit_accepts_the_documented_range():
    from yandex_core.paging import checked_limit

    assert checked_limit(1, minimum=1, maximum=200) == 1
    assert checked_limit(200, minimum=1, maximum=200) == 200


@pytest.mark.parametrize("bad", [0, 201, -1, "10", True, 1.0, None])
def test_checked_limit_refuses_anything_else(bad):
    from yandex_core.paging import checked_limit

    with pytest.raises(ProtocolError):
        checked_limit(bad, minimum=1, maximum=200)


def test_both_listing_tools_use_the_one_limit_check():
    """It was copied verbatim between them; a copy drifts, a shared one cannot."""
    import inspect

    from yandex_calendar_mcp.tools import calendars, events

    for module in (calendars, events):
        source = inspect.getsource(module)
        assert "def _checked_limit" not in source
        assert "checked_limit(" in source
