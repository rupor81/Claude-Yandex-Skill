---
title: 'Story 1.4 — Read one event in full'
type: 'feature'
created: '2026-09-06'
status: 'done'
review_loop_iteration: 0
baseline_commit: '89381878acaf6e7a52173ea166b6e9b75f482ad5'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-1-3-query-events-over-a-date-range.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Listing answers "what is on my calendar"; it does not answer "what is this
meeting". Attendees, description, location and the ETag are absent, and the ETag is what
story 1.7 will need to change an event without overwriting someone else's edit.

**Approach:** `calendar_event_get` takes a UID, optionally a `recurrence_id` to select
one instance of a series, and returns that event in full. It addresses the object
directly by UID; it never searches for it.

## Boundaries & Constraints

**Always:**
- **Test-first.** Every row of the matrix below and every acceptance criterion starts as
  a failing test, named for the harm it prevents rather than the mechanism it exercises.
  The report must state that each failed before the code existed, and how it failed.
- Fetch by addressing the object by UID. Never by searching for the UID: measured against
  the live account, a UID search returns the entire calendar — 1759 objects for one UID —
  so a search-based lookup would confidently return the wrong event.
- The ETag is read as a property in its own right, not from the response header and not
  from the library's cached attribute. Measured: the attribute is empty, the property
  gives `1788415102079`, and the header gives that same value with a `--gzip` suffix.
  Story 1.7 will send this value back as a precondition, so the two spellings must never
  be mixed.
- A UID that cannot be found is a not-found error naming it — never an empty success.
- Timestamps stay timezone-aware; all-day events stay dates.

**Ask First:**
- Any new dependency.
- Returning raw iCalendar text alongside the parsed fields.

**Never:**
- Mutating anything. This tool reads.
- Falling back to a range search when the UID lookup finds nothing — a plausible wrong
  event is worse than an honest miss.
- Inventing an ETag when the server does not supply one.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Standalone event | UID of a one-off event | Full detail: summary, description, location, attendees with their response status, organiser, start, end, ETag | N/A |
| Series without recurrence_id | UID of a recurring series | The series' own detail, stated to be the series rather than one instance | N/A |
| One instance | UID plus `recurrence_id` | That instance, with its own start and end | N/A |
| Modified instance | `recurrence_id` of an overridden instance | The override's values, not the series defaults | N/A |
| Cancelled instance | `recurrence_id` of an `EXDATE` or cancelled instance | Reported as cancelled, not returned as a live meeting | N/A |
| Unknown UID | UID nowhere on the account | Not-found naming the UID | Never an empty success |
| Unknown recurrence_id | Valid UID, instance not in the series | Not-found naming both, distinguishing it from an unknown UID | N/A |
| Calendar hint | `calendar_url` supplied | Only that calendar is addressed | Unknown URL is a not-found |
| No calendar hint | UID alone | Calendars are tried until the object is found | Not-found only after all were tried |
| One calendar unreachable | A calendar errors during the search | The others are still tried, and a miss says the search was incomplete | Never a plain not-found after a partial search |
| No ETag from server | Server omits the property | The field is null and the response says so | Never fabricated |
| All-day event | `DTSTART` is a date | Returned as a date | N/A |
| Attendee without a name | `ATTENDEE` with only an address | The address is returned; the name is null | N/A |

</frozen-after-approval>

## Code Map

- `packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py:152` -- add a by-UID fetch beside `_list_occurrences_blocking`; reuses `_calendars_for` for the calendar hint and the existing translation boundary
- `.../client/recurrence.py:119` -- reuse: expansion already resolves overrides; selecting one instance by recurrence id belongs beside it
- `.../tools/events.py:166` -- add `calendar_event_get` beside `calendar_events_list`
- `packages/yandex-core/src/yandex_core/risk.py` -- register the tool read-only
- `tests/unit/test_calendar_event_get.py` -- new
- `tests/live/test_calendar_live.py` -- extend: fetch by a UID taken from a live listing, and assert the ETag is present

## Tasks & Acceptance

**Execution:**
- [x] Failing tests for every matrix row, before any implementation
- [x] `client/caldav_client.py` -- by-UID fetch with the ETag read as a property
- [x] `client/recurrence.py` -- select one instance by recurrence id, overrides applied
- [x] `tools/events.py` -- `calendar_event_get` with validation and the not-found distinctions
- [x] `core/risk.py` -- register the tool
- [x] `tests/live` -- a real fetch by UID, asserting a non-empty ETag

**Acceptance Criteria:**
- Given a standalone event's UID, when the tool is called, then it returns attendees, description, location and an ETag.
- Given a UID and a recurrence id, when the tool is called, then it returns that instance, with an override's values when one exists.
- Given a UID that does not exist, when the tool is called, then it raises not-found naming the UID and never returns an empty success.
- Given a valid UID with an unknown recurrence id, when the tool is called, then the error distinguishes that from an unknown UID.
- Given a calendar that errors while the account is being searched, when the object is not found, then the answer says the search was incomplete rather than reporting a plain miss.
- Given a server that supplies no ETag, when the tool returns, then the field is null and says so, and no value is invented.

## Spec Change Log

- **Finding (implementation):** the matrix names a series and an instance but
  not a one-off event, and "stated to be the series rather than one instance"
  needs a field to say it in.
  **Amendment:** `scope` carries three values -- `single`, `series`,
  `occurrence` -- alongside `is_series`.
  **Avoids:** a one-off event described as a `series` of one, which would
  invite a caller to ask for instances that do not exist.
  **KEEP:** `recurrence_id` stays null for both `single` and `series`, so the
  presence of an instance id still means exactly one thing.

- **Finding (implementation):** the client's translation boundary rewrote
  *every* exception, so the `NotFound` this story raises deliberately inside it
  came back out as "unexpected failure talking to Yandex".
  **Amendment:** an exception already in the core taxonomy passes through
  `_translate` unchanged.
  **Avoids:** the one distinction this story exists to make -- unknown UID
  versus unknown instance versus incomplete search -- being erased two lines
  before it reached the caller.

- **Finding (implementation):** the matrix treats "a calendar errors during the
  search" as a partial loss, but a rejected app password errors on *every*
  calendar, and counting it as one unreadable collection would turn a wrong
  credential into "that event does not exist".
  **Amendment:** a transport failure, and an authorisation failure that is not
  a 403, are raised rather than counted. A 403 stays a per-calendar loss, which
  is the shared-calendar case the fan-out is meant to survive.
  **Avoids:** sending an operator to look for an event that is really there,
  with a not-found that was really an auth error.

- **Finding (implementation):** an EXDATE instance is absent from the
  expansion, so "cancelled" and "never existed" are the same answer to the
  library.
  **Amendment:** the series' `EXDATE` values are read directly and matched
  before the expansion is consulted; a hit is returned cancelled, with the
  series' duration applied to that instant.
  **Avoids:** reporting a cancelled meeting as a missing one -- which reads as
  "you have the wrong id" rather than "it is off".
  **KEEP:** the two spellings of a cancellation, `EXDATE` and a
  `STATUS:CANCELLED` override, answer identically to the caller.

- **Finding (implementation):** the tool needed a way to say the ETag was
  genuinely absent, as the matrix requires, without inventing a value.
  **Amendment:** `etag` is null and `etag_note` carries one sentence naming the
  consequence for a later update. It is null whenever an ETag was returned, so
  the two fields cannot disagree.
  **Avoids:** a caller reading a null `etag` as a bug in this server and
  retrying, or worse, updating without a precondition and not knowing it.

## Design Notes

Three facts were measured against the live account rather than assumed, because the
project's own research had flagged all three as uncertain.

UID lookup works — the earlier report that Yandex breaks fetch-by-UID did not reproduce.
UID *search* is broken in the worst way: it returns every object in the calendar while
looking like a successful filtered query.

ETags exist but only as a property. The library's cached attribute is empty, and the GET
header carries a `--gzip` suffix the property does not. Mixing the two would make story
1.7's concurrency check fail on events nobody had touched.

A miss after a partial search is not the same as a miss. If one calendar errored while
the account was scanned, the event may be in the one that failed, and saying "not found"
would assert something unverified.

## Verification

**Commands:**
- `env -u PYTHONPATH uv run --no-sync pytest tests/unit -q` -- expected: all pass, no network
- `env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run --no-sync pytest tests/live -q` -- expected: a real fetch by UID with a non-empty ETag
- `uv sync -q --no-editable --reinstall-package yandex-calendar-mcp --reinstall-package yandex-core` then a stdio `tools/list` -- expected: three tools, all read-only

## Suggested Review Order

**Addressing an event without searching for one**

- Addressed href first, then a UID-verified lookup; a search would return the whole calendar.
  [`caldav_client.py:179`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L179)

- The ETag as a property in its own right, and the difference between absent and unreadable.
  [`caldav_client.py:523`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L523)

**Reading one event out of what came back**

- Components gathered across documents, so a stored-apart override is not read as the series.
  [`recurrence.py:627`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/recurrence.py#L627)

- Validation, the four distinct misses, and the bounded description.
  [`events.py:738`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/tools/events.py#L738)
