---
title: 'Story 1.8 — Delete an event with an explicit scope'
type: 'feature'
created: '2026-09-07'
status: 'done'
review_loop_iteration: 0
baseline_commit: '1e2f6d76c84a3297c6d9adc35e17b772286c8a17'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-1-7-update-an-event-with-an-explicit-scope.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A cancelled meeting stays on the calendar. "Delete this meeting" is the most
ambiguous request in the epic and the least recoverable: on a recurring series it can mean
one instance or every instance, and the wrong reading destroys a year of history while
looking like it worked.

**Approach:** `calendar_event_delete` requires the caller to say which they mean.
Cancelling one instance is an edit — an exclusion added to the series — and is protected
by a precondition. Removing a series is a deletion, and this server does not protect it.

## Boundaries & Constraints

**Always:**
- **Test-first.** Every matrix row and acceptance criterion begins as a failing test named
  for the harm it prevents; the report states how each failed before the code existed.
- `scope` is required and has no default, as for updates.
- Cancelling one instance adds an exclusion by conditional write. Measured: this answers
  201, keeps the exclusion, and leaves an override of a different instance intact.
- Cancelling an instance that carries an override removes that override in the same write.
  An exclusion and an override for the same moment contradict each other, and readers
  disagree about which wins.
- **A series deletion cannot be made conditional on this server.** Measured: a delete
  carrying a stale ETag was answered 204 and removed the object anyway. The ETag is read
  again immediately before deleting and compared, which narrows the race but cannot close
  it. The tool says so rather than implying a protection it does not have.
- The event is read afterwards to confirm what is now there — for an instance, that the
  others survived; for a series, that it is gone.
- The tool declares itself destructive.

**Ask First:**
- Deleting every event in a calendar, or a calendar itself.
- Deleting by anything other than a UID the caller has read — a title or a time is a
  search, and this server's search returns the whole calendar.

**Never:**
- Choosing a scope, or treating a missing one as "series".
- Removing anything the caller did not name — other instances, other components,
  or other events sharing the object.
- Reporting success without confirming what the server now holds.
- Retrying a delete whose outcome is unknown.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Cancel one instance | UID, recurrence id, `scope: occurrence` | That instance is gone; the others keep their times | N/A |
| Instance with an override | The cancelled instance had been moved | Both the exclusion and the removal of the override are written | Never leaves a contradiction |
| Series with other overrides | Another instance was moved | It survives the cancellation of a different one | Never silently dropped |
| Delete a series | UID, `scope: series` | Every instance is gone and the object is removed | N/A |
| Delete a one-off event | UID of a non-recurring event, `scope: series` | The event is removed | N/A |
| Scope omitted | No `scope` | Refused before any request, naming both meanings and which is irreversible | Validation error |
| Occurrence without a recurrence id | `scope: occurrence`, no id | Refused | Validation error |
| Recurrence id with series scope | Both supplied | Refused as contradictory | Validation error |
| Stale ETag, one instance | Someone changed it first | Refused by the precondition; nothing written | Never overwritten |
| Stale ETag, whole series | Someone changed it first | Refused by the pre-delete comparison, and the answer states this is a check rather than a guarantee | Best effort, stated |
| Already cancelled instance | The instance is already excluded | Reported as already gone; no write is sent | N/A |
| Unknown UID or instance | Nothing to delete | Not-found naming which of the two was missing | Nothing written |
| Last instance of a series | Cancelling leaves no instances | Reported: the series has no occurrences left, and the object still exists | Never silently removes it |
| Delete outcome unknown | Connection lost mid-request | Says so, names the UID, does not retry | Never retried blindly |

</frozen-after-approval>

## Code Map

- `packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/compose.py:280` -- extend: add an exclusion and drop the matching override, reusing the editing path that already preserves everything unmentioned
- `.../client/caldav_client.py:806` -- add a delete beside `update_event`; conditional write for an instance, read-compare-delete for a series
- `.../tools/events.py` -- add `calendar_event_delete`
- `packages/yandex-core/src/yandex_core/risk.py` -- register as destructive
- `tests/unit/test_calendar_event_delete.py` -- new; `tests/live/test_calendar_live.py` -- extend inside the throwaway calendar, keeping the suite's shared rate-limit budget in mind

## Tasks & Acceptance

**Execution:**
- [x] Failing tests for every matrix row, before any implementation
- [x] `client/compose.py` -- exclusion plus override removal in one edit
- [x] `client/caldav_client.py` -- instance cancellation and series removal, with their different guarantees
- [x] `tools/events.py` -- `calendar_event_delete` with the scope rules
- [x] `core/risk.py` -- register destructive
- [x] `tests/live` -- cancel one instance of a real series, confirm the others survive, then remove the series

**Acceptance Criteria:**
- Given a series and `scope: occurrence`, when one instance is cancelled, then it is gone and every other instance keeps its time.
- Given the cancelled instance had been moved, when it is cancelled, then its override is removed in the same write.
- Given `scope: series`, when the event is deleted, then the object is gone and the answer confirms it.
- Given a stale ETag, when cancelling an instance, then the precondition refuses it; when deleting a series, then the pre-delete comparison refuses it and the answer says that check is not a guarantee.
- Given a missing scope, a missing recurrence id, or an id supplied with series scope, when the tool is called, then it is refused before any request.
- Given an instance already excluded, when it is cancelled again, then no write is sent and it is reported as already gone.

## Spec Change Log

- **Finding (implementation):** the matrix asks for a missing scope to be
  refused "naming both meanings and which is irreversible", and the refusal
  the update path already had says neither of those last things: for a change,
  both readings can be undone by changing the event back.
  **Amendment:** deleting has its own scope refusal, which says that `series`
  removes the object and that this server has no undelete.
  **Avoids:** a caller choosing between two options after being told only that
  they differ.

- **Finding (implementation):** "the last instance of a series" cannot be read
  off the recurrence rule. A rule with a `COUNT` every one of whose instances
  is excluded still looks like a live series to anything that reads the rule
  alone.
  **Amendment:** what is left is answered by expanding the edited document
  forwards and stopping at the first occurrence that survives, so an endless
  series answers immediately and an exhausted one answers after a bounded walk.
  **Avoids:** reporting "the series continues" about an object that will never
  show a meeting again -- and the opposite, reporting a live series as spent
  because its next instance is far away.

- **Finding (implementation):** the ETag check before a series delete can
  itself fail -- the server may return no version for the object when it is
  read again immediately beforehand.
  **Amendment:** the answer carries whether that comparison actually happened,
  and says so plainly when it did not: the delete went ahead with no check of
  any kind.
  **Avoids:** an answer whose wording implies a check that was skipped. The
  spec's own promise is that the tool states what protection it has; a
  best-effort check that silently did not run is exactly the case that promise
  exists for.

## Design Notes

Three things were measured on this account before designing any of this.

Adding an exclusion to a series is an ordinary conditional write: 201, the exclusion
stored, and an override belonging to a different instance untouched.

A delete carrying a stale ETag was answered 204 and the object was removed. This server
honours `If-Match` on a write and ignores it on a delete, so the more destructive of the
two operations is the less protected one. Reading the ETag again immediately before
deleting narrows the window; nothing closes it. Saying that plainly is the only honest
option, because a caller who believes the precondition holds will use this tool in
situations where it does not.

Cancelling an instance that carries an override has to remove the override as well.
Leaving both would store a contradiction — this moment is excluded, and here is what
happens at it — that different readers resolve differently.

## Verification

**Commands:**
- `env -u PYTHONPATH uv run --no-sync pytest tests/unit -q` -- expected: all pass, no network
- `env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run --no-sync pytest tests/live -q` -- expected: works inside its own throwaway calendar; leave several minutes since the last live run, as the account's rate limit is shared and does not reset between runs
- `uv sync -q --no-editable --reinstall-package yandex-calendar-mcp --reinstall-package yandex-core` then a stdio `tools/list` -- expected: seven tools, two destructive

**Measured:** 648 unit tests pass with no network -- 35 of them new, and every
one of them failing before the code it names existed: the whole new file failed
to import, since nothing it names was there. The guards were then broken one at
a time to confirm the tests bite rather than merely pass -- dropping the
override removal, skipping the pre-delete ETag comparison, claiming the
readback confirmed a delete it did not, writing an exclusion that was already
there, and reporting a spent series as live each turned exactly the test named
for that harm red, and nothing else.

The live suite passes against the real account (12 passed, 1 skipped, 3m56s)
and leaves it holding exactly the four calendars it began with, confirmed by a
separate listing afterwards. Its new test writes a real daily series into a
calendar it makes for itself, cancels the middle instance, confirms by
expanding the series that exactly the other two are still there at their own
times, has a stale ETag refused on the series delete with the event still
there afterwards, removes the series with the current ETag, and confirms
through the read path -- not the delete's own answer -- that it is gone.

`tools/list` over stdio returns seven tools, with `calendar_event_update` and
`calendar_event_delete` alone reporting `destructiveHint: true`, and those two
plus `calendar_event_create` alone reporting `readOnlyHint: false`.

## Suggested Review Order

**Removing only what was named**

- A series delete refuses an object that also holds somebody else's event.
  [`caldav_client.py:1532`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L1532)

- Cancelling one instance: an exclusion and the removal of its override, in one conditional write.
  [`caldav_client.py:1340`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L1340)

- The edit itself, which never leaves an exclusion beside an override for the same moment.
  [`compose.py:417`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/compose.py#L417)

**Answering honestly about a series**

- Does it still happen — answered by walking, bounded, and admitting when the walk was cut short.
  [`recurrence.py:949`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/recurrence.py#L949)
