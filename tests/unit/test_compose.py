"""The bytes this server PUTs, tested where they are made.

``client/compose.py`` is the only module in the project that produces a document
rather than reading one, and it is reachable from a plain script: its guards are
the last thing between a naive or inverted event and somebody's real calendar.
Every test here is named for what reaches the account without it.

Nothing here opens a socket -- there is nothing to open one to. The composer is
pure: a draft in, iCalendar text out.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from yandex_calendar_mcp.client.compose import (
    PRODID,
    EventDraft,
    build_event_document,
    new_uid,
)
from yandex_core.errors import ProtocolError

MOSCOW = timezone(timedelta(hours=3))
START = datetime(2026, 6, 8, 9, 0, tzinfo=MOSCOW)
END = START + timedelta(hours=1)
STAMP = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)


def draft(**kwargs) -> EventDraft:
    values = dict(uid="test-uid", summary="Design review", start=START, end=END)
    values.update(kwargs)
    return EventDraft(**values)


def lines(document: str) -> list[str]:
    """The document's unfolded lines, as a reader on the other end sees them."""
    return document.replace("\r\n ", "").replace("\r\n\t", "").splitlines()


# -- what is actually written ---------------------------------------------


def test_a_timed_event_is_written_as_an_instant_nobody_has_to_interpret():
    """A floating local time means a different moment to every reader.

    iCalendar cannot spell an offset, so the alternative to UTC is a TZID naming
    a VTIMEZONE that would have to be shipped with the event.
    """
    document = build_event_document(draft())

    assert "DTSTART:20260608T060000Z" in lines(document)
    assert "DTEND:20260608T070000Z" in lines(document)
    assert "TZID" not in document


def test_an_all_day_event_stays_dates_at_both_ends():
    """Coercing a day to midnight moves the day for everybody not on UTC."""
    document = build_event_document(
        draft(start=date(2026, 6, 8), end=date(2026, 6, 9))
    )

    assert "DTSTART;VALUE=DATE:20260608" in lines(document)
    assert "DTEND;VALUE=DATE:20260609" in lines(document)
    assert "T000000" not in document


def test_the_document_carries_the_uid_it_was_given():
    """The UID is how the caller addresses the event ever again."""
    document = build_event_document(draft(uid="a-particular-uid"))

    assert "UID:a-particular-uid" in lines(document)


def test_the_document_is_stamped_as_this_servers_work():
    """An event this server created is identifiable later without guessing."""
    document = build_event_document(draft())

    assert f"PRODID:{PRODID}" in lines(document)
    assert "VERSION:2.0" in lines(document)
    assert "CALSCALE:GREGORIAN" in lines(document)


def test_busy_time_and_sequence_are_stated_rather_than_left_to_a_default():
    """Whoever reads this event must answer the busy question the same way."""
    document = build_event_document(draft())

    assert "TRANSP:OPAQUE" in lines(document)
    assert "SEQUENCE:0" in lines(document)


def test_the_stamp_can_be_supplied_so_the_document_is_reproducible():
    """Without `now=`, nothing about the write is testable byte for byte."""
    document = build_event_document(draft(), now=STAMP)

    assert "DTSTAMP:20260102T030405Z" in lines(document), (
        "the stamp was not the one given, or its microseconds reached the wire"
    )


def test_the_stamp_defaults_to_now_in_utc():
    before = datetime.now(timezone.utc).replace(microsecond=0)
    document = build_event_document(draft())
    after = datetime.now(timezone.utc)

    (stamp,) = [line for line in lines(document) if line.startswith("DTSTAMP:")]
    written = datetime.strptime(
        stamp.removeprefix("DTSTAMP:"), "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=timezone.utc)
    assert before <= written <= after


def test_an_omitted_optional_field_is_absent_rather_than_empty():
    """An empty DESCRIPTION reads as though somebody meant to say nothing."""
    document = build_event_document(draft())

    assert "DESCRIPTION" not in document
    assert "LOCATION" not in document


def test_optional_detail_is_written_when_it_was_given():
    document = build_event_document(
        draft(description="Bring the sketches", location="Room 4")
    )

    assert "DESCRIPTION:Bring the sketches" in lines(document)
    assert "LOCATION:Room 4" in lines(document)


# -- text that would otherwise break the document apart -------------------


def test_a_summary_containing_a_comma_stays_one_value():
    """An unescaped comma is a value separator: the title would be split in two."""
    document = build_event_document(draft(summary="Design, review"))

    assert "SUMMARY:Design\\, review" in lines(document)


def test_a_summary_containing_a_newline_does_not_become_two_properties():
    """A raw newline ends the property; the rest of the title becomes garbage --
    or, worse, a line a reader takes for another property."""
    document = build_event_document(draft(summary="Design review\nsecond line"))

    assert "SUMMARY:Design review\\nsecond line" in lines(document)
    assert "second line" not in [line.strip() for line in lines(document)]


def test_a_semicolon_in_a_description_stays_inside_the_value():
    document = build_event_document(draft(description="one; two"))

    assert "DESCRIPTION:one\\; two" in lines(document)


def test_a_long_description_is_folded_and_unfolds_to_what_was_given():
    """A line over 75 octets is folded; a reader must get the original back."""
    body = "x" * 400
    document = build_event_document(draft(description=body))

    assert f"DESCRIPTION:{body}" in lines(document)
    assert max(len(line) for line in document.split("\r\n")) <= 75


# -- the guards, which are here as well as in tools/ ----------------------


@pytest.mark.parametrize("summary", ["", "   ", "\t\n"])
def test_an_untitled_event_is_refused_here_too(summary):
    """This layer runs from a plain script, and an untitled event is unfindable."""
    with pytest.raises(ProtocolError) as caught:
        build_event_document(draft(summary=summary))
    assert "summary" in str(caught.value).lower()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 6, 8, 9, 0), END),
        (START, datetime(2026, 6, 8, 10, 0)),
    ],
)
def test_a_naive_timestamp_never_reaches_the_wire(start, end):
    """A moment with no offset is a different moment to every reader, and the
    event is wrong on somebody's calendar until they notice."""
    with pytest.raises(ProtocolError) as caught:
        build_event_document(draft(start=start, end=end))
    assert "offset" in str(caught.value)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 6, 8), END),
        (START, date(2026, 6, 9)),
    ],
)
def test_half_an_all_day_event_is_refused_rather_than_repaired(start, end):
    """There is no correction here that is not a guess about what was meant."""
    with pytest.raises(ProtocolError) as caught:
        build_event_document(draft(start=start, end=end))
    assert "all-day" in str(caught.value).lower()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (START, START),
        (START, START - timedelta(minutes=30)),
        (date(2026, 6, 8), date(2026, 6, 8)),
        (date(2026, 6, 9), date(2026, 6, 8)),
    ],
)
def test_an_event_that_does_not_end_after_it_starts_is_refused(start, end):
    with pytest.raises(ProtocolError) as caught:
        build_event_document(draft(start=start, end=end))
    assert "`end`" in str(caught.value)


def test_an_event_spanning_an_offset_change_is_ordered_as_instants():
    """09:00+03:00 is before 07:30+00:00; comparing the clock faces would refuse
    a perfectly good meeting."""
    document = build_event_document(
        draft(start=START, end=datetime(2026, 6, 8, 7, 30, tzinfo=timezone.utc))
    )

    assert "DTEND:20260608T073000Z" in lines(document)


# -- the identifier -------------------------------------------------------


def test_two_uids_are_never_the_same():
    """Two meetings with the same title at the same time are a normal thing to
    want; a derived UID would make the second one replace the first."""
    assert len({new_uid() for _ in range(1000)}) == 1000


def test_a_uid_is_not_derived_from_the_event_it_names():
    assert new_uid() != new_uid()


def test_a_uid_is_safe_in_an_href():
    """The UID becomes the object's address; a space or a slash in one would
    change which object a later update addresses."""
    uid = new_uid()
    assert uid == uid.strip()
    assert not set(uid) - set("0123456789abcdef-")
