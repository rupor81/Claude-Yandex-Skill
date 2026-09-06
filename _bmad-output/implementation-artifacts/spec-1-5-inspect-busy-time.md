---
title: 'Story 1.5 — Inspect busy time'
type: 'feature'
created: '2026-09-06'
status: 'done'
review_loop_iteration: 0
baseline_commit: 'aa7f9ccf96170ed175248d8314558347b9f62f4e'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-1-4-read-one-event-in-full.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Finding a free slot by listing events means reading every meeting's contents
to answer a question about time. It also gets the answer wrong, because having an event
is not the same as being busy.

**Approach:** `calendar_freebusy_query` returns merged busy intervals over a required
range, each carrying what kind of busy it is. Intervals are computed from the expanded
occurrences this server already produces, because the protocol's own free-busy report is
rejected by this server.

## Boundaries & Constraints

**Always:**
- **Test-first.** Every matrix row and every acceptance criterion begins as a failing
  test named for the harm it prevents. The report states how each failed before the code.
- Busy time is computed from expanded occurrences. Measured: this server answers the
  CalDAV free-busy report with 400 Bad Request on every calendar, so there is nothing to
  fall back to and no point attempting it.
- An event that does not consume time does not produce a busy interval: transparent
  events, and invitations the operator declined.
- Uncertain commitment is reported as its own kind of busy, never collapsed into
  certainty. Measured on the live account: of 218 invitations carrying the operator's
  response, 28 are tentative and 22 unanswered — a quarter of them. Deciding for the
  caller would silently move a quarter of the calendar.
- Overlapping intervals of the same kind merge; different kinds never merge into each other.
- Timestamps are timezone-aware; all-day events occupy their whole day.
- A range wider than the documented maximum is refused by name — never silently narrowed.

**Ask First:**
- Any new dependency.
- Reading anyone's availability but the configured account's.

**Never:**
- Returning event titles, descriptions or attendees. This tool answers about time only.
- Attempting the free-busy report and reporting its failure as "no busy time".
- Guessing a single busy/free answer where the underlying data is uncertain.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Plain meetings | Range with accepted, opaque events | One busy interval each, kind `busy` | N/A |
| Adjacent meetings | Two events touching end-to-start | Merged into one interval | N/A |
| Overlapping meetings | Two overlapping accepted events | One merged interval spanning both | N/A |
| Transparent event | `TRANSP:TRANSPARENT` | No interval; it does not consume time | N/A |
| Declined invitation | Operator's response is declined | No interval | N/A |
| Tentative invitation | Operator's response is tentative | Interval of kind `busy-tentative`, not merged with `busy` | N/A |
| Unanswered invitation | Operator has not responded | Interval of its own kind, distinguishable from tentative | N/A |
| Recurring series | Range covering several instances | One interval per instance | N/A |
| All-day event | `DTSTART` is a date | Occupies the whole day in the account's offset | N/A |
| Event crossing the edge | Starts before `start`, ends inside | Clipped to the range, and the clipping is stated | N/A |
| Nothing in range | Empty but valid range | An empty list reported complete — not an error | N/A |
| Range too wide | Span beyond the maximum | Refused, naming the maximum | Never silently narrowed |
| Inverted or naive range | `end` before `start`, or no offset | Refused before any request | Validation error |
| Calendar unreadable | One calendar errors | The others still answer and the count is reported | Never a plain empty answer |
| Unreadable events | A malformed event in range | Counted and reported, as the listing tool does | Never silently dropped |

</frozen-after-approval>

## Code Map

- `packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/recurrence.py:119` -- extend `Occurrence` with transparency and the operator's own participation status; both are read from the component, so the knowledge stays in `client/`
- `.../client/caldav_client.py:152` -- reuse: `list_occurrences` already fetches and expands the range; the operator's address comes from the configured profile
- `.../tools/freebusy.py` -- new: classification, clipping, merging, and the tool itself
- `.../server.py` -- register the tool
- `packages/yandex-core/src/yandex_core/risk.py` -- register read-only
- `tests/unit/test_freebusy.py` -- new; `tests/live/test_calendar_live.py` -- extend with one real query

## Tasks & Acceptance

**Execution:**
- [x] Failing tests for every matrix row, before any implementation
- [x] `client/recurrence.py` -- expose transparency and the operator's participation status per occurrence
- [x] `tools/freebusy.py` -- classify, clip, merge, and report
- [x] `core/risk.py`, `server.py` -- register the tool read-only
- [x] `tests/live` -- a real busy query over a week

**Acceptance Criteria:**
- Given accepted opaque meetings in range, when the tool is called, then each yields a busy interval and touching or overlapping ones are merged.
- Given a transparent event or a declined invitation, when the tool is called, then neither produces a busy interval.
- Given a tentative or unanswered invitation, when the tool is called, then its interval carries its own kind and is not merged with certain busy time.
- Given a recurring series in range, when the tool is called, then each instance yields its own interval.
- Given an event starting before the range, when the tool is called, then its interval is clipped to the range and the clipping is stated.
- Given a range beyond the maximum, or inverted, or without offsets, when the tool is called, then it is refused before any request, naming the reason.
- Given no event titles are needed to answer, when the result is inspected, then it contains none.

## Spec Change Log

- **Finding (implementation):** the expansion the matrix says to compute from
  returns only occurrences whose own *start* falls inside the range, so "starts
  before `start`, ends inside" was not merely unclipped -- it was invisible.
  **Amendment:** `expand` takes an `overlap` flag; the free-busy path asks for
  occurrences that intersect the window, the listing path keeps the start rule.
  **Avoids:** reporting the range's first hour free while a meeting that began
  the night before is still running.
  **KEEP:** `calendar_events_list` is untouched -- an occurrence still belongs
  to exactly one of two adjacent ranges.

- **Finding (implementation):** "the account's offset" has nowhere to come
  from: a profile carries a login and a CalDAV URL, not a timezone, so an
  all-day date had no offset to be given.
  **Amendment:** an all-day event occupies its day in the offset of the `start`
  the query was asked with, and both the field description and the tool
  docstring say so.
  **Avoids:** reading a bare date as midnight UTC, which blocks the wrong 24
  hours for everybody not on UTC -- and avoids inventing a config field whose
  value nothing would ever verify.

- **Finding (implementation):** the epic requires every result cut short to
  carry a cursor, but the two ways this answer can be short are not alike.
  **Amendment:** a page cut short by `limit` carries a cursor naming the last
  interval, bound to the range; a range whose *expansion* hit its ceiling is
  reported `range_truncated` with no cursor and an instruction to narrow the
  range.
  **Avoids:** a cursor that resumes at an occurrence rather than an interval,
  which would split a merge across a page boundary and report a gap in the
  middle of one meeting. The requirement that matters is kept: such an answer
  is never `complete`.

- **Finding (implementation):** a status this server has never seen -- or an
  event with no `ATTENDEE` line for the operator at all, which is every event
  they created -- fits none of the four rows in the matrix.
  **Amendment:** only `DECLINED` frees time and only `TENTATIVE` and
  `NEEDS-ACTION` soften it; everything else is `busy`.
  **Avoids:** quietly freeing an hour that is taken, which is the one direction
  in which a wrong answer sends somebody to a meeting they are not at.

- **Finding (implementation):** two tools now validate the same date range, and
  the spec's own design note insists they must not disagree about what an
  over-wide one means.
  **Amendment:** the range rules moved to `tools/timerange.py`; `events.py`
  imports them instead of keeping its own copies.
  **Avoids:** the two refusals drifting apart, so that a caller learns the
  maximum from one tool and is refused differently by the other.

## Design Notes

The protocol's free-busy report is not an option here: measured against the live account,
every calendar answers it with 400 Bad Request. Attempting it and reporting the failure as
"no busy time" would be the worst outcome, so it is not attempted at all.

The interesting decision is what counts as busy. On the live account, 350 events are
opaque and 2 transparent; of the invitations carrying the operator's own response, 168
are accepted, 28 tentative and 22 unanswered. A boolean answer would silently resolve a
quarter of the calendar in one direction. iCalendar already has the vocabulary for this,
and the tool uses it rather than inventing certainty.

Clipping is stated rather than silent: an interval reported as starting at the range's
edge is a different fact from a meeting that genuinely starts then, and a caller planning
around it should be able to tell.

The epic's wording for an over-wide range says "truncated and says so". This refuses
instead, matching `calendar_events_list`, because two adjacent tools disagreeing about
what an over-wide range means is worse than either rule. Both satisfy the requirement
that matters: never silently narrowed.

## Verification

**Commands:**
- `env -u PYTHONPATH uv run --no-sync pytest tests/unit -q` -- expected: all pass, no network
- `env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run --no-sync pytest tests/live -q` -- expected: a real week of busy intervals
- `uv sync -q --no-editable --reinstall-package yandex-calendar-mcp --reinstall-package yandex-core` then a stdio `tools/list` -- expected: four tools, all read-only

## Suggested Review Order

**Deciding what counts as busy**

- Classification, clipping and merging; the tool itself.
  [`freebusy.py:234`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/tools/freebusy.py#L234)

- Whose reply is being read — a stranger sharing a local part is not the account.
  [`recurrence.py:464`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/recurrence.py#L464)

**Saying what is missing**

- Merging preserves clipping in both directions, so busy time never appears to stop at the edge.
  [`freebusy.py:483`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/tools/freebusy.py#L483)

- Several shortfalls can be true at once; the answer lists them rather than picking one.
  [`timerange.py:183`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/tools/timerange.py#L183)
