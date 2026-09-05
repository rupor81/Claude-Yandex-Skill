---
title: 'Story 1.3 — Query events over a date range'
type: 'feature'
created: '2026-09-05'
status: 'done'
review_loop_iteration: 0
baseline_commit: '50c19001592bc58056cd731093ac6da00ad56d96'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-1-2-verify-a-configured-account.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Claude can see which calendars exist but nothing about what happens in them.
Most meetings here recur, so returning recurrence rules rather than real dates would push
the hardest part of the problem onto the caller.

**Approach:** `calendar_events_list` takes a required date range and returns concrete
occurrences, each with its own start and end, its series UID, and a recurrence id when it
belongs to a series. Series are expanded locally, in the client layer: Yandex's own search
is documented to return unsorted and extra events and cannot be trusted to return all.

## Boundaries & Constraints

**Always:**
- `start` and `end` are required; no window is ever guessed.
- Expansion happens in `client/` via `recurring-ical-events`; `tools/` receives plain
  occurrence records and never sees an `RRULE`.
- Results are re-filtered locally against the requested range — the server returns extras.
- Ordering is deterministic: start, then series UID, then recurrence id.
- The cursor names the last occurrence returned, not its index, so a set that changes
  between pages cannot silently skip or duplicate.
- Every timestamp is timezone-aware with an explicit offset. All-day events stay dates and
  are never turned into midnight.
- A range wider than the documented maximum is refused by name, not silently narrowed.

**Ask First:**
- Any dependency beyond what `caldav` already brings.
- Server-side expansion or server-side text search.
- Returning event bodies, attendees, or attachments — story 1.4 owns full detail.

**Never:**
- A text-match parameter on the client; filtering is the tool layer's job.
- Reporting a partial result as complete.
- Silently dropping an occurrence that failed to parse.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Single events | Range covering non-recurring events | One occurrence each, no recurrence id | N/A |
| Recurring series | Range covering several instances | One occurrence per instance, all sharing the series UID, each with its own recurrence id | N/A |
| Cancelled instance | Series with `EXDATE` in range | That instance is absent; the rest remain | N/A |
| Modified instance | Series with a `RECURRENCE-ID` override | Returned in its modified form, once, not twice | N/A |
| All-day event | `DTSTART` is a date | Returned as a date, not midnight in some zone | N/A |
| Server returns extras | Yandex answers with events outside the range | They are filtered out locally | N/A |
| Truncation | More occurrences than `limit` | `complete: false`, cursor names the last occurrence | N/A |
| Page resumption | Cursor from a previous call | Resumes after that occurrence, even if events were added or removed meanwhile | Foreign cursor is a `ProtocolError` |
| Title filter | `title_contains` given | Applied in the tool layer over the fetched range | A filter over a truncated fetch yields `complete: false` |
| Range too wide | Span beyond the maximum | Refused, naming the maximum | Never silently narrowed |
| Inverted range | `end` before `start` | Refused before any request | Validation error |
| Naive timestamps | `start` or `end` without an offset | Refused at construction | Validation error |
| Unparseable event | One malformed `VEVENT` among good ones | The good ones are returned and the result says one could not be read | Never a silent drop |

</frozen-after-approval>

## Code Map

- `packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/recurrence.py` -- new: expands fetched calendar data into occurrence records via `recurring-ical-events`; owns all iCalendar knowledge
- `.../client/caldav_client.py:80` -- add a range fetch beside `list_calendars`, using the CalDAV time-range primitive; returns occurrences, accepts no text parameter
- `.../client/caldav_client.py:120` -- reuse: the existing translation boundary already converts CalDAV and HTTP failures into the core taxonomy
- `.../tools/events.py` -- new: `calendar_events_list` beside `tools/calendars.py`; validation, local filtering, ordering, pagination (see the change log for why it is its own module)
- `packages/yandex-core/src/yandex_core/paging.py` -- extend: a cursor payload that names a position rather than an index, keeping the existing tool-identity stamping
- `packages/yandex-core/src/yandex_core/risk.py` -- register the new tool read-only; an unregistered tool still refuses to start
- `tests/unit/test_recurrence.py`, `tests/unit/test_calendar_events_list.py` -- new
- `tests/live/test_calendar_live.py` -- extend: one real range query against the configured account

## Tasks & Acceptance

**Execution:**
- [x] `client/recurrence.py` -- occurrence record and expansion, including `EXDATE`, `RECURRENCE-ID` overrides, and all-day events
- [x] `client/caldav_client.py` -- range fetch returning expanded occurrences
- [x] `core/paging.py` -- position-based cursor
- [x] `tools/events.py` -- `calendar_events_list`: validation, range filter, title filter, ordering, pagination
- [x] `core/risk.py` -- register the tool
- [x] tests -- every matrix row against fixture calendar data; one live range query

**Acceptance Criteria:**
- Given single events and recurring series in range, when the tool is called, then each occurrence carries its own start and end, the series UID, and a recurrence id when it belongs to a series.
- Given `EXDATE` cancellations and `RECURRENCE-ID` modifications, when the range covers them, then cancelled instances are absent and modified instances appear once, in their modified form.
- Given more occurrences than `limit`, when the tool returns, then `complete` is false and the cursor names the last occurrence returned.
- Given a cursor and a set that changed since it was issued, when the next page is fetched, then no occurrence is skipped or repeated because of the change.
- Given a title filter, when it is applied, then the client received no text parameter, and a filter over a truncated fetch yields `complete: false`.
- Given a naive or inverted range, or one wider than the maximum, when the tool is called, then it is refused before any request, naming the reason.
- Given one malformed event among valid ones, when the tool returns, then the valid ones are present and the result states that one could not be read.

## Spec Change Log

- **Finding (implementation):** the code map placed the new tool beside
  `calendar_list` in `tools/calendars.py`, but that module's docstring, its
  limits, and its index cursor are all about calendars, and the event tool
  shares none of them.
  **Amendment:** `calendar_events_list` lives in `tools/events.py`.
  **Avoids:** one module holding two unrelated pagination models, where the
  index cursor the design notes call unsafe for events sits next to the
  position cursor that replaces it.
  **KEEP:** `calendar_list` keeps its index cursor; calendars are a stable set
  and rewriting it was not this story's business.

- **Finding (implementation):** the matrix asks for a title filter over a
  *truncated fetch*, but nothing in the design truncated a fetch -- the range
  bound alone does not, and an unbounded expansion would breach the epic's "no
  call may return an unbounded set".
  **Amendment:** expansion carries a ceiling (`recurrence.DEFAULT_CEILING`,
  2000 occurrences) applied *after* ordering. Hitting it sets `truncated`, and
  any result built on a truncated expansion is `complete: false` whether or not
  a title filter was applied.
  **Avoids:** a ceiling applied during generation, which would drop arbitrary
  occurrences and could stall a cursor that can never advance past it.
  **KEEP:** the ceiling is a safety valve an order of magnitude above `limit`,
  not a page size; ordering before cutting is what keeps paging monotonic.

- **Finding (implementation):** an unreadable event makes an answer partial in
  a way no cursor can repair, and `Page` has no way to say so.
  **Amendment:** `EventPage` extends `Page` with an `unreadable` count, and any
  non-zero count forces `complete: false` with `next_cursor: null`.
  **Avoids:** widening the core `Page` for a concern only this connector has,
  and equally avoids reporting a result complete when an event in the range was
  never read.
  **KEEP:** the count is not silently folded into the flag -- both are present,
  because "cut short by a limit" and "one event is corrupt" need different
  actions from the caller.

- **Finding (implementation):** a floating `DTSTART` (no `TZID`, no `Z`) has no
  offset to report, and the boundary rule admits no naive timestamp.
  **Amendment:** such a series is counted `unreadable` and reported, rather
  than being given an invented offset or dropped.
  **Avoids:** guessing a zone, which silently moves the event for every reader
  at a different offset -- the same failure all-day coercion causes.
  **KEEP:** reported, never dropped; "Never silently drop an occurrence that
  failed to parse" covers this case too.

- **Finding (implementation):** the matrix names the inverted range but not the
  zero-width one, which can never contain anything.
  **Amendment:** `end` must be strictly after `start`; both cases are refused
  by the same message.
  **Avoids:** an empty `complete: true` page for a range that was a typo.

- **Finding (implementation):** the tool had no way to name a calendar, so
  every query would fan out across the whole account.
  **Amendment:** an optional `calendar_url`, taking the URL `calendar_list`
  already returns; omitted, every calendar is searched as before.
  **Avoids:** a synthesised calendar id, and a required argument the caller
  would have to fetch first for the common "what is on my week" question.

- **Finding (implementation):** the documented maximum range had no value.
  **Amendment:** `MAX_RANGE_DAYS = 366`, named in the refusal message and in
  the `end` parameter description.
  **Avoids:** a year-long question failing on a leap year.

## Design Notes

Yandex's search returns unsorted and extra events, so neither its ordering nor its range
filtering is load-bearing: the client fetches by time-range and everything else is
re-established locally. Server-side expansion is avoided for the same reason — an
occurrence the server declines to expand is invisible, which is what NFR3 exists to catch.

`recurring-ical-events` arrives with `caldav` and handles `RRULE`, `RDATE`, `EXDATE` and
`RECURRENCE-ID`. Hand-rolling it is a known source of off-by-one-instance bugs at DST
boundaries.

The index cursor from `calendar_list` was recorded as safe only at calendar scale and
load-bearing once events arrived. They have. Occurrences are derived, not stored, and the
set shifts as meetings are booked; a cursor naming the last occurrence survives that churn
where an index cannot.

All-day events are dates, not midnight in a zone — coercing them moves events across day
boundaries for anyone at a different offset.

## Verification

**Commands:**
- `env -u PYTHONPATH uv run --no-sync pytest tests/unit -q` -- expected: all pass, no network
- `env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run --no-sync pytest tests/live -q` -- expected: a real range query against the configured account
- `uv sync -q --no-editable` then a stdio `tools/list` -- expected: two tools, both `readOnlyHint: true`

## Suggested Review Order

**Turning rules into dates**

- Expansion, grouped by calendar and UID so an override never yields a duplicate.
  [`recurrence.py:119`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/recurrence.py#L119)

- One window semantics, decided and enforced: start inclusive, end exclusive, on the occurrence's own start.
  [`recurrence.py:311`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/recurrence.py#L311)

- Cancelled instances excluded — the library removes EXDATE, this removes STATUS:CANCELLED.
  [`recurrence.py:317`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/recurrence.py#L317)

**Paging that cannot lie**

- The cursor names the last occurrence and the query it belongs to, not a position.
  [`paging.py:85`](../../../packages/yandex-core/src/yandex_core/paging.py#L85)

- Validation, filtering, ordering, and the reason a page is short.
  [`events.py:166`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/tools/events.py#L166)

**Losing nothing quietly**

- A failing calendar is counted, not fatal; the others still answer.
  [`caldav_client.py:152`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L152)
