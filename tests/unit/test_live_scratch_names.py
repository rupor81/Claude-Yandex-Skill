"""The rules the live suite obeys about its own throwaway objects.

Unit-tested because they decide what gets reported as a leak on somebody's real
account, and that decision should not require a network to check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from livescratch import (
    SCRATCH_PREFIX,
    STALE_AFTER,
    new_scratch_name,
    stale_report,
    stale_scratch_calendars,
)

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def _made(delta: timedelta) -> str:
    return new_scratch_name(NOW - delta)


def test_a_new_name_carries_the_time_it_was_made():
    """The entry that asked for this proposed ageing names that carry no time.

    `yandex-mcp-live-f57b5924` cannot be aged, and CalDAV promises no creation
    date for a collection, so the rule was unimplementable until the name said so
    itself.
    """
    name = new_scratch_name(NOW)

    assert name.startswith(SCRATCH_PREFIX)
    assert "20260925T120000Z" in name

    # And the stamp is what the ageing reads back, to the second.
    (found,) = stale_scratch_calendars([name], now=NOW + STALE_AFTER)
    assert found.made == NOW


def test_two_names_made_in_the_same_second_do_not_collide():
    """Two live runs can start together; a shared name means a shared calendar."""
    assert new_scratch_name(NOW) != new_scratch_name(NOW)


def test_a_fresh_object_is_not_a_leak():
    """The suite takes about three minutes. Reporting its own working space
    would train the operator to ignore the report."""
    assert stale_scratch_calendars([_made(timedelta(minutes=3))], now=NOW) == []


def test_an_old_object_is_a_leak():
    (found,) = stale_scratch_calendars([_made(timedelta(hours=5))], now=NOW)

    assert found.made == NOW - timedelta(hours=5)
    assert "made 2026-09-25T07:00:00" in found.age_note


def test_a_name_from_before_stamps_existed_is_reported_as_unknown_not_ignored():
    """`yandex-mcp-live-f57b5924` is the real one this project actually leaked.

    Treating an unparseable name as recent would hide exactly the leak the check
    was written for.
    """
    (found,) = stale_scratch_calendars(["yandex-mcp-live-f57b5924"], now=NOW)

    assert found.made is None
    assert "age unknown" in found.age_note


def test_the_operator_s_own_calendars_are_none_of_this_check_s_business():
    names = ["Мои события", "Поездки", "Не забыть", "Мероприятия ICL SOFT", "live-test"]

    assert stale_scratch_calendars(names, now=NOW) == []


def test_a_malformed_stamp_is_reported_rather_than_crashed_on():
    (found,) = stale_scratch_calendars(
        [f"{SCRATCH_PREFIX}20261345T999999Z-abc123"], now=NOW
    )

    assert found.made is None


def test_the_report_says_what_was_found_and_refuses_to_remove_it():
    stale = stale_scratch_calendars(
        [_made(timedelta(hours=5)), "yandex-mcp-live-f57b5924"], now=NOW
    )

    report = stale_report(stale)

    assert "2" in report
    for item in stale:
        assert item.name in report
    assert "removed" in report or "remove" in report
    assert "by hand" in report, "the operator is not told who removes them"


def test_nothing_found_says_nothing_at_all():
    """A check that prints on every clean run is a check nobody reads."""
    assert stale_report([]) == ""
