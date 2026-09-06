---
title: 'Story 1.6 — Create an event'
type: 'feature'
created: '2026-09-06'
status: 'done'
review_loop_iteration: 0
baseline_commit: '38a05e0c720980890a10784c42f5d21e7a630223'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-1-5-inspect-busy-time.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Everything built so far reads. Scheduling can be proposed but not completed,
so the connector can tell you when you are free and then stop.

**Approach:** `calendar_event_create` writes one event into a named calendar and returns
what the server actually stored, together with its ETag, so a later change can be made
safely. It is the first tool in this project that alters the operator's data.

## Boundaries & Constraints

**Always:**
- **Test-first.** Every matrix row and acceptance criterion begins as a failing test named
  for the harm it prevents; the report states how each failed before the code existed.
- The target calendar is named explicitly. This account has four, the server marks none of
  them as default, and a meeting written into the wrong one is not obviously recoverable.
- The event is read back after writing and the response reports the stored values, not the
  requested ones. This server is reported to adjust properties on write, and a caller
  told what it asked for rather than what exists has been told nothing.
- The ETag is returned, read as a property. Measured: creation answers 201 and the ETag is
  available immediately.
- Creation is additive and is annotated as such — not destructive — and it never replaces
  an existing object: the write carries a guard that fails rather than overwrite.
- Timestamps are timezone-aware; all-day events are dates; `end` is after `start`.

**Ask First:**
- Any new dependency.
- Inviting attendees, which sends mail on the operator's behalf.
- Creating recurring events. One-off events only in this story.

**Never:**
- Choosing a calendar for the caller.
- Reporting success without confirming the event exists.
- Retrying a write that may already have succeeded, without first checking.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Plain event | Calendar, summary, start, end | Created; UID, href and ETag returned, with the stored start, end and summary | N/A |
| Optional detail | Description and location supplied | Stored and echoed back as stored | N/A |
| All-day event | Dates rather than timestamps | Stored as an all-day event and reported as one | N/A |
| Server adjusted it | Server alters a stored value | The response shows what the server holds, and says it differs from the request | N/A |
| Unknown calendar | `calendar_url` naming nothing | Not-found naming the URL, distinguished from a calendar that refused | Nothing written |
| Read-only calendar | Server refuses the write | Reported as a permission failure, naming the calendar | Never reported as success |
| Naive timestamps | `start` or `end` without offset | Refused before any request | Validation error |
| Inverted or zero range | `end` at or before `start` | Refused before any request | Validation error |
| Empty summary | Blank or whitespace title | Refused; an untitled event is unfindable later | Validation error |
| Href collision | The generated href already exists | The write fails rather than overwriting | Never silently replaces |
| Write succeeded, readback failed | Event stored but cannot be re-read | Reported as created with the readback failure stated | Never reported as failed |
| Write outcome unknown | Connection lost mid-write | Says the outcome is unknown and names the UID to check | Never retried blindly |

</frozen-after-approval>

## Code Map

- `packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py:179` -- add a create beside `get_event`; addresses the calendar by its real href, since a URL this server hands back is not always the object's actual address
- `.../client/recurrence.py` -- reuse: `read_event` already turns a stored document into a record; the readback goes through it so created and read events cannot describe themselves differently
- `.../tools/events.py:738` -- add `calendar_event_create` beside the read tools
- `packages/yandex-core/src/yandex_core/risk.py` -- register as a write, not read-only and not destructive
- `tests/unit/test_calendar_event_create.py` -- new
- `tests/live/test_calendar_live.py` -- extend: create in a throwaway calendar, read it back, delete it, and verify the account is unchanged

## Tasks & Acceptance

**Execution:**
- [x] Failing tests for every matrix row, before any implementation
- [x] `client/caldav_client.py` -- create with a collision guard, then read back through the existing reader
- [x] `tools/events.py` -- `calendar_event_create` with validation and stored-value reporting
- [x] `core/risk.py` -- register as a write operation
- [x] `tests/live` -- a real create and cleanup in a calendar made for the test

**Acceptance Criteria:**
- Given a calendar and a valid event, when the tool is called, then the event exists on the server and the response carries its UID, ETag and stored values.
- Given the server stored something other than what was asked, when the tool returns, then the difference is stated rather than hidden.
- Given a naive, inverted or untitled request, when the tool is called, then it is refused before anything is written.
- Given an href that already exists, when the write runs, then it fails rather than replacing what is there.
- Given a write whose outcome cannot be determined, when the tool returns, then it says so and names the UID to check, and does not retry.
- Given the tool's annotations, when they are read, then it declares itself a write and not read-only.

## Spec Change Log

- **Finding (implementation):** the code map puts the write in
  `caldav_client.py` and reuses `recurrence.py` for the readback, but neither is
  a place to *compose* a document: `recurrence.py` exists to turn fetched data
  into occurrences, and `caldav_client.py` deliberately imports no iCalendar
  library at all.
  **Amendment:** a third client module, `client/compose.py`, owns the bytes that
  are written -- and only those.
  **Avoids:** teaching the module that talks to the wire what an iCalendar
  property is, which is the coupling the layering rules exist to prevent.
  **KEEP:** the readback still goes through `read_event`, so a created event and
  a read one cannot describe themselves differently.

- **Finding (implementation):** iCalendar has no way to spell a UTC offset. A
  local time needs a `TZID` naming a `VTIMEZONE` that would have to be shipped
  with every event, and a floating one means a different moment to every reader.
  **Amendment:** a timed event is written in UTC, as `...Z`; an all-day event
  stays `VALUE=DATE` at both ends.
  **Avoids:** an event written at the right number and the wrong moment. The
  instant is preserved exactly, and the answer reports the stored instant rather
  than this spelling.

- **Finding (live, measured):** this server stores an event to the whole minute
  and drops the seconds. A create asked for 14:33:41 comes back holding 14:33:00.
  **Amendment:** none needed -- this is the matrix's "server adjusted it" row,
  observed for real. The live suite now asserts it: a create carrying seconds
  must report `differs_from_request` and name `start`, rather than echoing back
  what was asked for.
  **Avoids:** the assertion that would have been written by habit -- "stored
  equals requested" -- which is a claim about the server, and which this server
  falsifies within a minute of being asked.

## Design Notes

Three facts were measured on this account before writing any of this, using a calendar
created and destroyed for the purpose.

Creation works: 201, with the ETag available immediately.

An earlier probe returned 409 Conflict on every write and looked like the documented
Yandex write quirk. It was not. The URL this server returns from creating a calendar is
not that calendar's real address, so the writes were going somewhere else — and, worse,
a delete aimed at that same wrong address answered success while removing nothing. Any
URL this server hands back is therefore treated as a hint, not an address; the real one
comes from the listing.

That is also why the event is read back before success is reported. A write whose
confirmation comes only from the write's own response has been confirmed by the party
least able to be trusted about it.

## Verification

**Commands:**
- `env -u PYTHONPATH uv run --no-sync pytest tests/unit -q` -- expected: all pass, no network
- `env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run --no-sync pytest tests/live -q` -- expected: creates and removes its own calendar, leaving the account's four untouched
- `uv sync -q --no-editable --reinstall-package yandex-calendar-mcp --reinstall-package yandex-core` then a stdio `tools/list` -- expected: five tools, four read-only and one declaring itself a write

**Measured:** 466 unit tests pass with no network; the live suite passes against
the real account (10 passed, 1 skipped) and leaves it holding exactly the four
calendars it began with; `tools/list` over stdio returns the five tools, with
`calendar_event_create` alone reporting `readOnlyHint: false` and every tool
reporting `destructiveHint: false`.

## Suggested Review Order

**Writing to the right place, once**

- The calendar is never chosen for the caller, and the address comes from the listing.
  [`caldav_client.py:264`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L264)

- Only 201 is a creation; a 204 means something was replaced, which is the outcome the guard exists to prevent.
  [`caldav_client.py:1008`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L1008)

**Saying what actually happened**

- The one document this server writes, with its invariants enforced where a plain script would hit them.
  [`compose.py:69`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/compose.py#L69)

- Comparing the stored event against what was sent, so the server is blamed only for what it changed.
  [`events.py:1219`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/tools/events.py#L1219)
