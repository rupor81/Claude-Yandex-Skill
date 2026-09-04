# Epic 1 Context: Calendar connector, end to end

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Deliver a working Calendar MCP server that lets Claude read and manage a real Yandex calendar: list calendars, query events over a date range with recurring series expanded into concrete occurrences, inspect busy time, and create, modify, and delete events under an explicit `occurrence` or `series` scope — plus the setup and verification commands needed to connect a real account and confirm it works. Calendar goes first because it is the only service whose authentication does not require OAuth: an app password is sufficient, so the whole connector can be finished and used before any OAuth flow exists. This epic therefore also lays the shared `yandex_core` foundation (result types, error taxonomy, risk registry, cursors, credentials, config) — built to exactly what Calendar needs and no further. A later epic explicitly revisits the core once a second protocol reveals the real commonality; do not generalise speculatively here.

## Stories

- Story 1.1: Connect a calendar and list it
- Story 1.2: Verify a configured account
- Story 1.3: Query events over a date range
- Story 1.4: Read one event in full
- Story 1.5: Inspect busy time
- Story 1.6: Create an event
- Story 1.7: Update an event with an explicit scope
- Story 1.8: Delete an event with an explicit scope

## Requirements & Constraints

**Bounded, honest output.** Every listing tool accepts `limit` with a documented default and returns at most that many items; no call may return an unbounded set. Any result cut short — by limit, by a character cap, or by a maximum query range — says so explicitly and carries a cursor for the remainder. A query that cannot be answered completely must report that fact rather than return a partial set that looks complete; in particular, filtering locally over a source that was itself truncated yields a result marked incomplete.

**Honest errors.** Failures name an actionable cause (wrong or revoked app password, organisation policy, rate limit, network, not found). Raw protocol errors are wrapped, never surfaced bare, and never converted into an empty successful result. A missing UID is a not-found error, not an empty success.

**Determinism and declared risk.** Identical arguments produce identical results, and nothing happens the caller did not request. Tools carry truthful MCP `readOnlyHint` / `destructiveHint` annotations: reads are read-only, create is a non-destructive write, update and delete are destructive.

**Credential hygiene.** The calendar app password lives in the system keychain with a `0600` fallback file under the config directory; it never appears in tool arguments, command-line arguments, logs, error messages, or the repository. Log formatting redacts by field name.

**Setup and verification.** Setup must explain, not assume, the manual app-password step: Yandex CalDAV rejects OAuth bearer tokens, so the password is created by hand in Yandex ID. Verification checks each configured service independently, reports per-service reachability with actionable causes, reports unconfigured services as unconfigured rather than failed, and completes its full report even when the network is down. When a Yandex 360 administrator has disabled app passwords, the failure is reported as organisation policy rather than as a generic authentication error.

**Independent failure.** Each server starts, fails, and is configured on its own; an unusable Calendar credential must not affect the other connectors. Profiles for personal Yandex ID and Yandex 360 accounts are declared in config and switched without editing code.

**Success criteria.** Setup, including the manual app-password step, completes in one sitting from written instructions; no tool call floods the context window; credentials never reach logs, arguments, or the repository.

## Technical Decisions

**Layering.** Three layers per server: entrypoint (`server.py`, transport and MCP application only) → `tools/` (MCP contracts, validation, annotations, filtering, pagination) → `client/` (one wire protocol, nothing about MCP). `tools/` never imports a protocol library; `client/` never imports `mcp` and stays usable from a plain script. Dependencies are one-way: servers depend on the core, the core depends on no server, no server imports another.

**Async boundary.** Every tool function is `async def`. The CalDAV library is blocking and is wrapped exactly once, inside `client/`, via `anyio.to_thread.run_sync`. No blocking protocol call appears in `tools/`.

**Result types.** Collections return `Page[T]`, truncatable text returns `Chunk`; both are defined in the core with `complete: bool` and `next_cursor: str | None` as required fields. No tool returns a bare list or a bare string. Cursors are opaque base64 strings encoded and decoded by the core; callers never parse them.

**Errors.** One hierarchy in the core (`AuthError`, `PermissionError`, `NotFound`, `Conflict`, `RateLimited`, `TransportError`, `ProtocolError`). Client code translates protocol exceptions at its own boundary; no protocol exception type crosses out of `client/`.

**Annotations from a registry.** Each tool declares a risk class in a single table in the core, and annotations are derived from it — never written inline. A tool with no registry entry fails at server start rather than registering unannotated.

**Credentials and config.** One core module is the sole reader of keychain, fallback file, and environment. Profiles live in the config TOML and are selected by an environment variable at server start, not per call.

**Time.** Every datetime crossing a boundary is timezone-aware ISO 8601 with an explicit offset; naive datetimes are rejected at construction, before any request is sent. Date-only values are typed as dates, never as midnight.

**Naming and identifiers.** Tools are named `<server>_<object>_<verb>`, with the object segment omitted only where the server itself is the object. Calendar identifies events by `uid` plus optional `recurrence_id` — never a synthesised composite id.

**Recurrence.** A CalDAV series is one record carrying an `RRULE`. Expansion into concrete occurrences — RRULE/RDATE expansion, `EXDATE` cancellations omitted, `RECURRENCE-ID` overrides applied — is done with the `recurring-ical-events` library rather than hand-rolled logic, in a dedicated client module.

**Mutation safety.** Calendar mutations take a required `scope` of `occurrence` or `series` with no default, because at the protocol level "delete the event" is genuinely ambiguous and one interpretation is catastrophic. An occurrence-scoped update is an added `RECURRENCE-ID` override; an occurrence-scoped delete is an added `EXDATE`. Updates send the ETag read earlier as a precondition; a mismatch raises a conflict and changes nothing. `this-and-following` is deliberately deferred. No permanent-delete call exists anywhere in the client layer, so no tool can reach one.

**Filtering.** Clients fetch using only dependable server-side filters — for CalDAV, the `time-range` filter. All other filtering (e.g. by title) happens in `tools/`, which owns the resulting completeness flag; the CalDAV client accepts no text-match parameter. CalDAV `text-match` is avoided because it cannot be proven never to under-return, and a silently short result is indistinguishable from a complete one to a model.

**Environment.** Greenfield `uv` workspace, no starter template; the epic delivers the core, calendar-server, and CLI packages. The host system Python is too old for the MCP SDK, so the interpreter is pinned to 3.13 through `uv` without touching the system Python. The MCP SDK is on its v2 line, where `FastMCP` is renamed `MCPServer`, `mcp.server.fastmcp.*` moved to `mcp.server.mcpserver.*`, and `get_context()` is replaced by a declared `ctx` parameter — nearly every online example targets v1 and will not run.

**Logging and tests.** Logging goes to stderr only; stdout carries the MCP protocol on stdio transport. Tests split into unit tests against fakes with no network, and live tests skipped unless an environment flag is set.

## Cross-Story Dependencies

- Story 1.1 is foundational: the workspace, core package, credential storage, profile selection, risk registry, and server entrypoint it establishes are prerequisites for every other story in the epic.
- Story 1.2's verification command is extended by each later epic to cover its own service; build it so adding a service is additive.
- Stories 1.7 and 1.8 depend on the ETag and `recurrence_id` surfaced by Stories 1.3 and 1.4 — read paths must land before write paths.
- Later epics depend on this epic only for the shared core, which they are expected to correct once a second protocol exists. Nothing in Epic 1 may depend on later epics.
