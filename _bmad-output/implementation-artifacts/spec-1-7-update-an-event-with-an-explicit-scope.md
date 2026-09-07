---
title: 'Story 1.7 — Update an event with an explicit scope'
type: 'feature'
created: '2026-09-07'
status: 'done'
review_loop_iteration: 0
baseline_commit: '3e4afeccfff71ecc6405dc349a3cd5e593a80e64'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-1-6-create-an-event.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** An event can be created but not corrected. On a calendar where most meetings
recur, "change this meeting" is ambiguous at the protocol level — it can mean one
occurrence or every one of them — and the two are not recoverable from each other.

**Approach:** `calendar_event_update` changes an event, requiring the caller to say which
of the two they mean and requiring the ETag they last read. The write is conditional, so a
change made by someone else in between is refused rather than overwritten.

## Boundaries & Constraints

**Always:**
- **Test-first.** Every matrix row and acceptance criterion begins as a failing test named
  for the harm it prevents; the report states how each failed before the code existed.
- `scope` is required and has no default. `occurrence` changes one instance;
  `series` changes them all. Guessing would be wrong a fraction of the time and
  catastrophic in one direction.
- The ETag the caller last read is required and sent as a precondition. Measured: this
  server honours it and answers 412 for a stale one.
- The stored object is read, modified and written back whole. A series and the overrides
  of its instances live in one object here — measured — so replacing rather than editing
  would silently delete a moved instance while appearing to succeed.
- Nothing outside what was asked to change is altered: other components, unmentioned
  fields, and the event's identity all survive.
- The event is read back afterwards and the answer reports the stored values.
- The tool declares itself destructive, because it overwrites what was there.

**Ask First:**
- Changing an event's calendar, which is a move rather than an edit.
- Changing a recurrence rule, which redefines which instances exist.
- Adding or removing attendees, which sends mail on the operator's behalf.

**Never:**
- Writing without a precondition, or retrying a refused write with a fresh ETag to force
  it through.
- Dropping components the caller did not mention.
- Reporting success without confirming what the server now holds.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Change one occurrence | UID, recurrence id, `scope: occurrence` | Only that instance changes; the others keep their times | N/A |
| Change the series | UID, `scope: series` | Every instance changes | N/A |
| Series with an existing override | Series changed while one instance was already moved | The moved instance survives with its own values | Never silently dropped |
| Change a one-off event | UID of a non-recurring event, `scope: series` | The event changes | N/A |
| Scope omitted | No `scope` | Refused before any request, naming both meanings | Validation error |
| Occurrence without a recurrence id | `scope: occurrence`, no id | Refused; there is no instance to change | Validation error |
| Recurrence id with series scope | Both supplied | Refused as contradictory rather than one being ignored | Validation error |
| Stale ETag | Someone else changed it first | Refused, nothing written, and the answer says to re-read | Never overwritten |
| Missing ETag | No precondition supplied | Refused before any request | Validation error |
| Unknown UID or instance | Nothing to change | Not-found naming which of the two was missing | Nothing written |
| Nothing actually changes | Every supplied value equals the stored one | Reported as a no-op, and no write is sent | N/A |
| Server adjusted the result | Stored value differs from the request | Reported as a difference, as creation does | N/A |
| Write outcome unknown | Connection lost mid-write | Says so, names the UID, and does not retry | Never retried blindly |
| Readback fails after writing | Written but not re-readable | Reported as changed, with the readback failure stated | Never reported as failed |

</frozen-after-approval>

## Code Map

- `packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/compose.py:69` -- extend: edit an existing document rather than only building a new one; every component the caller did not name is carried through untouched
- `.../client/caldav_client.py:264` -- add an update beside `create_event`; conditional PUT with the caller's ETag, the same status discipline, and the same unknown-outcome handling
- `.../client/recurrence.py:627` -- reuse: `read_event` already locates a series' master and an instance's override
- `.../tools/events.py:1219` -- add `calendar_event_update`; reuse the difference reporting that creation established
- `packages/yandex-core/src/yandex_core/risk.py` -- register as destructive
- `tests/unit/test_calendar_event_update.py` -- new; `tests/live/test_calendar_live.py` -- extend inside the throwaway calendar

## Tasks & Acceptance

**Execution:**
- [x] Failing tests for every matrix row, before any implementation
- [x] `client/compose.py` -- edit a stored document, preserving everything unmentioned
- [x] `client/caldav_client.py` -- conditional update with precondition failure reported as a conflict
- [x] `tools/events.py` -- `calendar_event_update` with the scope rules and difference reporting
- [x] `core/risk.py` -- register destructive
- [x] `tests/live` -- change one instance of a real series and confirm the others are untouched

**Acceptance Criteria:**
- Given a recurring series and `scope: occurrence`, when one instance is changed, then only that instance differs and the rest keep their times.
- Given a series that already has a moved instance, when the series is changed, then the moved instance keeps its own values.
- Given a stale ETag, when the update runs, then it is refused, nothing is written, and the answer says to re-read the event.
- Given a missing scope, a missing ETag, an occurrence scope without an instance, or an instance with series scope, when the tool is called, then it is refused before any request.
- Given values identical to what is stored, when the tool is called, then it reports a no-op and sends no write.
- Given the tool's annotations, when they are read, then it declares itself destructive.

## Spec Change Log

- **Finding (implementation):** the matrix says nothing about moving one end of
  an event. Honouring `start` without `end` either stretches the meeting or
  inverts it, and which was meant cannot be read off the request.
  **Amendment:** `start` and `end` must be given together; one without the
  other is refused before any request, as a date paired with a timestamp
  already was.
  **Avoids:** the two silent outcomes -- a meeting quietly made longer, or a
  document written with `end` before `start`. An update that names nothing at
  all is refused for the same reason: it would rewrite the event to say what it
  already says, and bump its version for nobody.

- **Finding (implementation):** an instance removed by `EXDATE` is not in the
  series' expansion, but it can still be named by `recurrence_id`. Writing an
  override for it puts a meeting that was called off back on the calendar, and
  leaves the stored object saying the instance is both cancelled and not.
  **Amendment:** an occurrence-scoped change to a cancelled instance is refused,
  naming the cancellation.
  **Avoids:** reviving a cancelled meeting as a side effect of editing it, which
  no caller asked for and no answer would have mentioned.

- **Finding (implementation):** one ETag is a guard over one CalDAV object. A
  UID that really is stored across several objects cannot be changed safely:
  editing one and sending that single precondition claims a guard over
  documents it never covered.
  **Amendment:** such an event is refused, and nothing is written.
  **Avoids:** reporting the whole event as changed when half of it was not.

- **Finding (live, measured):** the address built from the UID and the one the
  library's UID lookup reports are the *same object* under two spellings -- they
  differ only in whether the `@` in the principal's path segment is
  percent-encoded -- and the two documents they return are not even equal as
  text, because the server re-stamps `DTSTAMP` per response. The first version
  of the "several objects" guard therefore refused every change to a perfectly
  ordinary event.
  **Amendment:** the two addresses are compared unquoted, once, so one object
  seen twice counts once.
  **Avoids:** a guard against a rare hazard that instead blocked the ordinary
  case -- and did so with a message about the operator's data being unusual,
  which it was not.

- **Finding (live, measured):** this server answers a *successful* conditional
  update with **201**, not 204 -- on an object that plainly existed a moment
  earlier, and whose change did take effect.
  **Amendment:** for a conditional update, 200, 201 and 204 are all acceptance.
  201 stays a failure on a create, where the guard is `If-None-Match: *`.
  **Avoids:** reporting "nothing was there, something new was created" about a
  change that was applied exactly as asked. 201 stays a failure on a create,
  where the guard is the opposite question, `If-None-Match: *`, and 201 is the
  only answer that is not a replacement.

- **Finding (live, measured -- corrects the entry above):** the justification
  first recorded for accepting 201 was **false**. It claimed `If-Match` against
  an href holding nothing must be answered 412, so a 201 under that header could
  not mean "there was nothing there". Measured against the live account, a PUT
  carrying `If-Match: <etag>` to an empty href answered **201 and created the
  object**: this server does not evaluate the precondition for a resource that
  does not exist.
  **Amendment:** the reason for accepting 201 is that this server answers a
  successful conditional update with it -- measured -- and nothing more. The
  claim about 412 is withdrawn, and no other rule may be derived from it.
  **Avoids:** a later change reasoning from a guarantee this server does not
  give. The practical consequence is stated rather than hidden: an event deleted
  between our read and our write is **resurrected** by the update, not refused,
  and the answer reports it as a change. Preconditions cannot close that race
  here, so the tool's own documentation says so and `deferred-work.md` carries
  it as an open limitation.

## Design Notes

Two facts were measured on this account before designing any of this, in a calendar
created and destroyed for the purpose.

Preconditions work: a conditional write with the current ETag succeeds and the ETag
changes; the same write with the stale one answers 412. AD-11's concurrency rule is
therefore implementable rather than aspirational.

A series and the overrides of its instances are stored in a single object — writing an
override into the same object answered 201, and the calendar still held exactly one
object afterwards. That is why an update edits the stored document instead of composing a
replacement: a replacement would take the moved instance with it, and the caller would see
a successful edit with no sign that anything was lost.

## Verification

**Commands:**
- `env -u PYTHONPATH uv run --no-sync pytest tests/unit -q` -- expected: all pass, no network
- `env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run --no-sync pytest tests/live -q` -- expected: works inside its own throwaway calendar and leaves the account's four untouched
- `uv sync -q --no-editable --reinstall-package yandex-calendar-mcp --reinstall-package yandex-core` then a stdio `tools/list` -- expected: six tools, one destructive

**Measured:** 590 unit tests pass with no network -- 57 of them new, and every
one of them failing before the code it names existed. The live suite passes
against the real account (11 passed, 1 skipped) and leaves it holding exactly
the four calendars it began with; its new test writes a real daily series into a
calendar it makes for itself, moves one instance, expands the series to confirm
the other two kept their times, renames the series and confirms the moved
instance survived with its own values, has a stale ETag refused with nothing
written, and gets a no-op for a change that changes nothing. `tools/list` over
stdio returns six tools, with `calendar_event_update` alone reporting
`destructiveHint: true` and it and `calendar_event_create` alone reporting
`readOnlyHint: false`.

## Suggested Review Order

**Editing rather than replacing**

- The stored document is edited in place; everything the caller did not name survives.
  [`compose.py:280`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/compose.py#L280)

- A derived override carries the master's reminders and none of its recurrence.
  [`compose.py:547`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/compose.py#L547)

**Refusing before writing**

- Scope, ETag and the event's real state are settled before anything is sent.
  [`caldav_client.py:806`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L806)

- What each answer to a conditional write means — and what this server does not honour.
  [`caldav_client.py:1536`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L1536)
