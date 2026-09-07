# Deferred Work

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md`
  summary: No README, LICENSE, CI workflow, py.typed markers, or linter configuration, despite the source carrying noqa codes nothing enforces.
  evidence: Real repository hygiene gaps for something meant to be installed and wired into an MCP client. Out of scope for story 1.1, which the spec limits to one vertical slice; belongs to epic completion.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-verify-a-configured-account.md`
  summary: Editable installs are unusable here — something re-applies the macOS UF_HIDDEN flag to the .venv .pth files within seconds, and Python 3.13's site.addpackage silently skips hidden .pth files, so every console script fails with ModuleNotFoundError.
  evidence: Root cause read directly from site.py in the installed interpreter. Neither uv nor file creation reliably sets the flag; it returns between two shell prompts, so no chflags remedy holds. Worked around by installing the workspace non-editable (`uv sync --no-editable`), which removes the .pth mechanism entirely and survives a deliberately hidden .pth. The cost is real: source edits no longer take effect until the next sync, so development and day-to-day use now want different install modes. A durable choice — UV_NO_EDITABLE for operators, editable plus pytest's pythonpath for development — should be made and documented rather than left to whoever last ran a sync.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-query-events-over-a-date-range.md`
  summary: Every page of an event query re-opens a TLS connection and re-fetches the whole range, so paging a wide range at a small limit repeats the full account-wide fetch once per page.
  evidence: Correct but wasteful, and invisible at present scale — a 60-day window over the real account returns 422 occurrences in one page. It becomes load-bearing for wide ranges or a small limit; a short-lived expansion cache keyed by the cursor's query stamp is the natural fix, and the stamp needed for it already exists.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-7-update-an-event-with-an-explicit-scope.md`
  summary: The live suite cannot be run back to back. This server's rate limit is per account and does not reset between runs, so the second and third run in quick succession fail somewhere — and never in the same place twice.
  evidence: Diagnosed properly the second time. Story 1.6 narrowed two sixty-day windows and a five-item page size, which halved the suite and made two consecutive runs green; that was a real improvement and a wrong diagnosis. The cause is the shared budget, not any one test: after four runs inside an hour the failure simply moves to whichever test is unlucky, and each of those tests passes alone in ten to fifteen seconds. There is nothing to fix in the code. What is needed is a rule — leave several minutes between full live runs — and, if that ever stops being enough, a session-scoped fixture that spaces the tests rather than a cheaper test.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-8-delete-an-event-with-an-explicit-scope.md`
  summary: A deletion cannot be made conditional on this server, so the most destructive operation here is the least protected one.
  evidence: Measured against the live account: a DELETE carrying a stale `If-Match` was answered 204 and the object was removed anyway. No header this client can send turns the race into a refusal. What is available is narrowing: the ETag is read again immediately before the delete and compared, which shrinks the window between the caller's read and the removal at the cost of one request, and the answer says in words that this is a check rather than a guarantee -- and says when even that check could not be made. Nothing further is available without server support; the entry exists so nobody later reads the comparison as a precondition and builds on it.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-8-delete-an-event-with-an-explicit-scope.md`
  summary: Deleting every event in a calendar, deleting a calendar itself, and `this-and-following` are all deliberately absent, and an event stored across several CalDAV objects is refused rather than partly removed.
  evidence: The first two are the spec's "ask first" items and have no tool at all, which is the right shape for them: each is a decision an operator makes once, not a thing a model should reach for mid-conversation. `this-and-following` was deferred at epic level and stays deferred -- the reader does not resolve `RANGE=THISANDFUTURE` either, so offering to write one would create documents this server cannot read back. The several-objects refusal matches the update path: one request covers one object, and removing one of several while reporting the whole event is the failure this project exists to avoid. Each would need its own explicit-scope treatment rather than a flag on this tool.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-8-delete-an-event-with-an-explicit-scope.md`
  summary: A CalDAV object holding more than one event is refused for `scope: series`, not rewritten without the event being deleted.
  evidence: A DELETE removes the object, and an object may hold several unrelated events -- clients that batch produce them. Review demonstrated the harm by running it: an object holding `standup` and `board`, deleted by UID `standup`, left zero events while the tool reported a confirmed, scoped success. The refusal is the fix. The alternative -- removing only the named components and PUTting the remainder back -- is a real option and is deliberately not taken here: it turns the least recoverable path into a write whose correctness depends on this server's composer round-tripping somebody else's document, including components and properties it does not model. Doing it would need its own story, with the round trip measured against the live account rather than against our own composer.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-8-delete-an-event-with-an-explicit-scope.md`
  summary: A UID present in more than one of the account's calendars is refused rather than disambiguated, and there is no "delete it from all of them".
  evidence: Calendars were searched in listing order and the first hit taken, so which calendar a meeting was removed from depended on an accident of ordering, silently. Every calendar is now searched and two hits are a refusal naming both URLs, with `calendar_url` as the way through. What is deliberately absent is any way to say "both": one request removes one object, the answer describes one object, and a tool that removed two while reporting one would be the failure this project exists to avoid. A caller who means both makes two calls.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-8-delete-an-event-with-an-explicit-scope.md`
  summary: "Does this series still happen" is answered by a bounded walk, and the bound can be reached -- in which case the answer is "not decided", never "none left".
  evidence: An endless rule every one of whose instances is cancelled supplies skipped instances forever; measured, `has_occurrences` was still spinning after thirty seconds against a 1.5-second suite, and it is reachable from the tool. The walk now stops after 500 skipped instances and returns `None`, and the tool says in words that this says nothing against the series. Deciding it properly means reasoning about the rule rather than expanding it -- an RRULE with no UNTIL and no COUNT whose every instance is excluded is a question about the exclusion set, not about the expansion -- and that belongs with whatever story next needs recurrence understood rather than expanded.

## Resolved

- Calendar paging by index (raised in story 1.1, closed 2026-09-06). `calendar_list` now
  uses the same position cursor as `calendar_events_list`: it names the calendar it
  stopped at, sorted by name with the URL breaking ties. Closed because the two tools had
  drifted to two different cursor contracts in one server, and because a list that changed
  between pages silently omitted an entry. The "cursor past the end" error is gone with
  it — that rule existed only to compensate for an index that could not tell a shrunken
  list from a finished one.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-4-read-one-event-in-full.md`
  summary: A recurring series whose master and whose RECURRENCE-ID override are stored at unrelated hrefs cannot be read completely, so such an instance returns the series' unmodified time.
  evidence: A genuine platform limit rather than an omission. Enumerating every object sharing a UID needs a UID search, and this server's UID search returns the entire calendar — 1759 objects for one UID, measured. The library's by-UID lookup returns exactly one object by construction. The addressed href plus that lookup covers every shape seen on the live account; only an override filed under an unrelated href escapes, and no safe mechanism reaches it.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-5-inspect-busy-time.md`
  summary: An all-day event's busy interval is anchored to the offset of the range the caller asked with, and for a login written without a domain the account's own domains are inferred from the CalDAV host rather than read from the principal.
  evidence: Both are defensible defaults with no better source available today. A profile carries a login and a URL, not a timezone, so inventing a config field would create a value nothing verifies. The principal's calendar-user-address-set is the authoritative answer to "which addresses are this account" and would replace the host-derived guess; it is one extra request at connect time and worth doing when a second connector needs the same answer.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-6-create-an-event.md`
  summary: Creating an event cannot invite attendees and cannot create a recurring series; both were deliberately excluded from the first write story.
  evidence: Inviting sends mail on the operator's behalf, which is a different kind of act from writing to their own calendar and deserves its own decision. Recurrence multiplies the validation surface — RRULE, EXDATE, overrides — and the reading side of that took a whole story to get right. Neither is blocked by anything in the code; both are scope held back on purpose.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-6-create-an-event.md`
  summary: A write whose outcome is unknown names the UID and tells the caller to check, but nothing yet performs that check for them.
  evidence: Correct and honest as far as it goes — the alternative, retrying blindly, is what creates duplicate meetings. A follow-up could read by UID and report whether the write landed, turning "check this yourself" into an answer. That belongs with the update story, which needs the same read-then-decide shape.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-7-update-an-event-with-an-explicit-scope.md`
  summary: A conditional update cannot be made to refuse an event that was deleted between the read and the write, so such an event is resurrected rather than refused.
  evidence: Measured against the live account: a PUT carrying `If-Match: <etag>` to an href holding nothing answered 201 and created the object. This server does not evaluate the precondition for a resource that does not exist, so no header this client can send turns the race into a refusal — the earlier claim that such a write must be answered 412 was wrong and has been corrected in the spec's change log. What is available is narrowing, not closing: a HEAD or PROPFIND immediately before the PUT shrinks the window without removing it, and would cost a request on every change. Stated plainly in the tool's documentation instead, so nobody builds on a guarantee this server does not give.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-7-update-an-event-with-an-explicit-scope.md`
  summary: An event cannot be moved between calendars, a series' recurrence cannot be changed, attendees cannot be added or removed, and a text field cannot be cleared through the tool.
  evidence: Named only in a docstring until now, which is not where scope decisions belong. Moving an event between calendars is a delete and a create, and this server has no delete — doing it as a copy would leave the original behind on any partial failure. Changing an RRULE rewrites every future instance of a series other people are also in, and needs the same explicit-scope treatment the times got. Attendees send mail on the operator's behalf, held back from story 1.6 for the same reason. Clearing a field is the one that is nearly free: `client/compose.py` already distinguishes "not named" from `None` and now removes the property for `None`, but the tool's parameters use `None` as "omitted", so a second spelling — a sentinel, or an explicit `clear` list — has to be chosen before it can be offered.

## Resolved

- The live suite's rate-limit flake (raised in story 1.5, closed 2026-09-06). It was not one
  bad test: the whole live suite shares one budget with this server, and whichever test ran
  last paid for the others. Two sixty-day windows and a five-item page size were doing the
  spending. Narrowing them to what each question actually needs cut the suite from three
  minutes to ninety seconds and made two consecutive full runs green. Disabling the caldav
  library's automatic retry for writes, done for correctness in story 1.6, is what moved the
  symptom onto the write test and made the aggregate cause visible.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-8-delete-an-event-with-an-explicit-scope.md`
  summary: A live test left a calendar on the real account when the network dropped mid-run, because its cleanup could not reach the server either.
  evidence: Found by reading the account rather than by any assertion. The tests now create their throwaway calendar inside the block whose cleanup removes it, which closes the ordinary case, but no `finally` survives a network that is gone. A stale-calendar sweep at the start of a live run — remove anything named `yandex-mcp-live-*` older than an hour — would make the suite self-healing instead of relying on every run ending well.
