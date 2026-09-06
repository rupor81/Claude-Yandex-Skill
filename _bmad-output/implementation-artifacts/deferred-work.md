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

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-query-events-over-a-date-range.md`
  summary: The live paging test fails intermittently when run after the rest of the live suite and passes on its own, because it makes about thirty sequential account-wide queries into a server with aggressive rate limiting.
  evidence: Observed twice. The code is not at fault — the same test passes in isolation in about 100 seconds — but a test that is red in the suite and green alone teaches everyone to stop reading red, which costs more than the coverage is worth. Walking fewer pages would check the same three properties (it terminates, never dead-ends, never repeats) without provoking the limit; that is a change to the test's cost, not to what it asserts.
