# Deferred Work

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md`
  summary: Calendar paging fetches the entire list on every call and resumes by index over a server-defined order, so a set that changes mid-traversal can silently skip or duplicate entries.
  evidence: Real, but harmless at calendar scale (a handful of entries, rarely changing). Binding the cursor to a snapshot or resuming by identity is the fix; it becomes load-bearing for event listing, where the set is large and volatile.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md`
  summary: No README, LICENSE, CI workflow, py.typed markers, or linter configuration, despite the source carrying noqa codes nothing enforces.
  evidence: Real repository hygiene gaps for something meant to be installed and wired into an MCP client. Out of scope for story 1.1, which the spec limits to one vertical slice; belongs to epic completion.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md`
  summary: On macOS the editable-install .pth files under .venv intermittently acquire the UF_HIDDEN flag, which Python 3.13 skips, breaking every import until chflags nohidden is run.
  evidence: Reproduced directly — console scripts and imports failed in a clean environment, and unhiding fixed them. Tests are made independent of this via pytest's pythonpath, but console entry points remain exposed. A permanent fix (non-editable installs, or documented remedy) needs a decision beyond this story.
