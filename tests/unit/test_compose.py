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


# -- editing a document the server already holds ---------------------------
#
# The other half of this module, and the dangerous one: a creation that goes
# wrong leaves a bad event, a change that goes wrong destroys a good one.
# Every fixture below is written out rather than composed, so what is asserted
# is the editor's reading of a stored document and not its agreement with the
# composer beside it.

MASTER_WITH_ALARM = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\nEXDATE:20260612T060000Z\r\n"
    "RDATE:20260613T060000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Standup\r\n"
    "TRIGGER:-PT10M\r\nEND:VALARM\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)

NINTH = datetime(2026, 6, 9, 6, 0, tzinfo=timezone.utc)


def edited(ics, **kwargs):
    from yandex_calendar_mcp.client.compose import apply_event_edit

    call = dict(uid="standup", scope="series", recurrence_id=None)
    call.update(kwargs)
    return apply_event_edit(ics, **call)


def components_of(document: str):
    """The VEVENTs of a written document, parsed as a reader would."""
    import icalendar

    return list(icalendar.Calendar.from_ical(document).walk("VEVENT"))


def override_in(document: str):
    """The single derived override in a written document."""
    overrides = [
        component
        for component in components_of(document)
        if component.get("RECURRENCE-ID") is not None
    ]
    assert len(overrides) == 1, f"expected one override, got {len(overrides)}"
    return overrides[0]


def test_deriving_an_override_carries_the_reminder_the_series_gave_it(monkeypatch):
    """Moving one meeting must not silently remove its alarm.

    A VALARM is a subcomponent, not a property: an override copied property by
    property has no reminder at all, and the caller who moved a standup is
    never told the notification went with it.
    """
    from yandex_calendar_mcp.client.compose import EventEdit

    result = edited(
        MASTER_WITH_ALARM,
        scope="occurrence",
        recurrence_id=NINTH,
        edit=EventEdit(
            start=datetime(2026, 6, 9, 7, 0, tzinfo=timezone.utc),
            end=datetime(2026, 6, 9, 7, 30, tzinfo=timezone.utc),
        ),
    )

    assert result.changed is True
    override = override_in(result.document)
    alarms = list(override.walk("VALARM"))
    assert len(alarms) == 1, "the moved instance lost its reminder"
    assert str(alarms[0].get("TRIGGER").dt) == "-1 day, 23:50:00"
    # And the series keeps its own.
    assert result.document.count("BEGIN:VALARM") == 2


def test_a_derived_override_is_not_a_second_series(monkeypatch):
    """An override carrying the series' RRULE is a whole second recurrence.

    Counted rather than looked for: the master satisfies a `RRULE:` substring
    on its own, so presence proves nothing about the component beside it.
    """
    from yandex_calendar_mcp.client.compose import EventEdit

    result = edited(
        MASTER_WITH_ALARM,
        scope="occurrence",
        recurrence_id=NINTH,
        edit=EventEdit(
            start=datetime(2026, 6, 9, 7, 0, tzinfo=timezone.utc),
            end=datetime(2026, 6, 9, 7, 30, tzinfo=timezone.utc),
        ),
    )

    document = result.document
    assert document.count("RRULE") == 1, "the override duplicated the recurrence"
    assert document.count("EXDATE") == 1, "the override carried the series' EXDATE"
    assert document.count("RDATE") == 1, "the override carried the series' RDATE"
    override = override_in(document)
    for name in ("RRULE", "EXDATE", "RDATE"):
        assert override.get(name) is None, f"the override carries {name}"


DURATION_MASTER = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:sprint-sync\r\nSUMMARY:Sprint sync\r\n"
    "DTSTART:20260608T060000Z\r\nDURATION:PT45M\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_moving_an_event_timed_by_duration_leaves_only_one_answer_for_its_end():
    """Yandex writes DURATION. A moved event carrying DTEND *and* DURATION lets
    two readers disagree about when the meeting finishes."""
    from yandex_calendar_mcp.client.compose import EventEdit

    result = edited(
        DURATION_MASTER,
        uid="sprint-sync",
        edit=EventEdit(
            start=datetime(2026, 6, 8, 8, 0, tzinfo=timezone.utc),
            end=datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc),
        ),
    )

    assert result.changed is True
    assert "DURATION" not in result.document, "the event states its length twice"
    assert "DTEND:20260608T090000Z" in result.document


ONE_OFF_WITH_LOCATION = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:design-review\r\nSUMMARY:Design review\r\n"
    "LOCATION:Room 4\r\nDESCRIPTION:Bring the sketches\r\n"
    "X-YANDEX-THING:keep-me\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T070000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nLAST-MODIFIED:20260601T000000Z\r\nSEQUENCE:0\r\n"
    "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Design review\r\n"
    "TRIGGER:-PT15M\r\nEND:VALARM\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_clearing_a_field_removes_it_rather_than_storing_the_word_none():
    """`None` is documented as "it has none", and this layer is usable from a
    plain script. Writing the literal string is the one outcome nobody meant."""
    from yandex_calendar_mcp.client.compose import EventEdit

    result = edited(
        ONE_OFF_WITH_LOCATION,
        uid="design-review",
        edit=EventEdit(location=None),
    )

    assert result.changed is True
    assert "LOCATION:None" not in result.document
    assert "LOCATION" not in result.document
    assert result.sent["location"] is None


TWO_MASTERS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260608T060000Z\r\nDTEND:20260608T063000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:standup\r\nSUMMARY:Standup elsewhere\r\n"
    "DTSTART:20260609T060000Z\r\nDTEND:20260609T063000Z\r\n"
    "DTSTAMP:20260601T000000Z\r\nSEQUENCE:0\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_two_definitions_of_one_event_are_refused_rather_than_half_changed():
    """Editing the first and reporting the whole event as changed is a lie the
    caller has no way to detect."""
    from yandex_calendar_mcp.client.compose import EventEdit

    with pytest.raises(ProtocolError) as caught:
        edited(TWO_MASTERS, edit=EventEdit(summary="Daily standup"))

    message = str(caught.value)
    assert "standup" in message
    assert "nothing was written" in message.lower()


@pytest.mark.parametrize(
    "value", ["2026-06-08T09:00:00+03:00", None, 20260608, object()]
)
def test_a_boundary_that_is_not_a_date_is_refused_as_a_protocol_error(value):
    """A caller of this layer gets the documented refusal, not an AttributeError
    from three frames down."""
    from yandex_calendar_mcp.client.compose import EventEdit, check_event_edit

    with pytest.raises(ProtocolError):
        check_event_edit(EventEdit(start=value, end=value))


def test_the_round_trip_through_the_parser_keeps_what_it_does_not_understand():
    """The whole stored document is re-serialised on every edit. An unknown
    X- property and a VALARM are exactly what a lossy round trip drops."""
    from yandex_calendar_mcp.client.compose import EventEdit

    result = edited(
        ONE_OFF_WITH_LOCATION,
        uid="design-review",
        edit=EventEdit(summary="Design review II"),
    )

    assert "X-YANDEX-THING:keep-me" in result.document
    assert "BEGIN:VALARM" in result.document
    assert "TRIGGER:-PT15M" in result.document
    assert "DESCRIPTION:Bring the sketches" in result.document


def test_the_revision_stamp_can_be_pinned_and_moves_forward():
    """Without a clock the caller can pin, every written body differs from the
    last and nothing can assert that LAST-MODIFIED actually advanced."""
    from yandex_calendar_mcp.client.compose import EventEdit

    result = edited(
        ONE_OFF_WITH_LOCATION,
        uid="design-review",
        edit=EventEdit(summary="Design review II"),
        now=datetime(2026, 6, 2, 12, 30, 15, 987654, tzinfo=timezone.utc),
    )

    lines_written = lines(result.document)
    assert "LAST-MODIFIED:20260602T123015Z" in lines_written
    assert "DTSTAMP:20260602T123015Z" in lines_written
    assert "LAST-MODIFIED:20260601T000000Z" not in lines_written
    assert "SEQUENCE:1" in lines_written


def test_the_two_spellings_of_a_scope_are_the_same_two_words():
    """`compose.py` spells them so it needs nothing from the reader; nothing
    made the two agree, and a change to one would silently split the tool's
    vocabulary from the client's."""
    from yandex_calendar_mcp.client import compose, recurrence

    assert compose.SCOPE_SERIES == recurrence.SCOPE_SERIES
    assert compose.SCOPE_OCCURRENCE == recurrence.SCOPE_OCCURRENCE
