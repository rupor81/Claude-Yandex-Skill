# Deferred Work

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md`
  summary: Calendar paging fetches the entire list on every call and resumes by index over a server-defined order, so a set that changes mid-traversal can silently skip or duplicate entries.
  evidence: Real, but harmless at calendar scale (a handful of entries, rarely changing). Binding the cursor to a snapshot or resuming by identity is the fix; it becomes load-bearing for event listing, where the set is large and volatile.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md`
  summary: No README, LICENSE, CI workflow, py.typed markers, or linter configuration, despite the source carrying noqa codes nothing enforces.
  evidence: Real repository hygiene gaps for something meant to be installed and wired into an MCP client. Out of scope for story 1.1, which the spec limits to one vertical slice; belongs to epic completion.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-verify-a-configured-account.md`
  summary: Editable installs are unusable here — something re-applies the macOS UF_HIDDEN flag to the .venv .pth files within seconds, and Python 3.13's site.addpackage silently skips hidden .pth files, so every console script fails with ModuleNotFoundError.
  evidence: Root cause read directly from site.py in the installed interpreter. Neither uv nor file creation reliably sets the flag; it returns between two shell prompts, so no chflags remedy holds. Worked around by installing the workspace non-editable (`uv sync --no-editable`), which removes the .pth mechanism entirely and survives a deliberately hidden .pth. The cost is real: source edits no longer take effect until the next sync, so development and day-to-day use now want different install modes. A durable choice — UV_NO_EDITABLE for operators, editable plus pytest's pythonpath for development — should be made and documented rather than left to whoever last ran a sync.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md`
  summary: On macOS the editable-install .pth files under .venv intermittently acquire the UF_HIDDEN flag, which Python 3.13 skips, breaking every import until chflags nohidden is run.
  evidence: Reproduced directly — console scripts and imports failed in a clean environment, and unhiding fixed them. Tests are made independent of this via pytest's pythonpath, but console entry points remain exposed. A permanent fix (non-editable installs, or documented remedy) needs a decision beyond this story.
