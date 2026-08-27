---
title: Addendum — Yandex MCP Connectors
status: draft
created: 2026-08-26
updated: 2026-08-27
---

# Addendum

Depth that belongs to the PRD and architecture rather than the brief.

## Verified protocol detail

**Disk.** `GET /v1/disk/resources/files` accepts `limit`, `offset`, `media_type`,
`fields`, `preview_size`, `preview_crop`, and returns a flat list of every file in
alphabetical order. There is no filtering by name or content and no sort control.
Confirmed against the official reference. Locating a file by name therefore costs a full
enumeration — roughly one request per thousand files at `limit=1000`.

**Mail.** IMAP authenticates with XOAUTH2, the same mechanism Gmail uses; the
authorization string is `user=<login>\001auth=Bearer <token>\001\001`. Scopes are
`mail:imap_full` for read and delete, `mail:smtp` for sending. Date criteria (`SINCE`,
`BEFORE`, `ON`) are dependable. Text criteria combining `FROM` and `SUBJECT` are widely
reported to misreport on Cyrillic — unverified, and worth testing early.

Subjects arrive as MIME encoded-words and must be decoded client-side before matching,
which is a second, independent reason to filter locally.

**Calendar.** CalDAV at `caldav.yandex.ru`, Basic auth with an app password from Yandex
ID; OAuth bearer tokens are not accepted. `calendar-query` with a `time-range` filter is
the dependable primitive. `text-match` on `SUMMARY` may work but should not be relied on —
filtering locally over a date window cannot silently under-return.

Recurring events are stored as a single `VEVENT` carrying `RRULE`, with `RECURRENCE-ID`
overrides for modified instances and `EXDATE` for cancelled ones. Expanding a series into
concrete occurrences is the client's responsibility. Concurrency is managed with ETags.

## How the required primitives were derived

A concrete question was traced through the protocols to establish which tools must exist:
*"find the results of the meeting with R-Pharm about Accord"*, where the calendar holds an
event titled `R-Pharm Accord`.

| Step | Operation | Primitive required | Reliability |
|---|---|---|---|
| 1 | Fetch events over a date window | CalDAV `calendar-query` + `time-range` | High |
| 2 | Keep events whose title matches | Caller-side string match | High |
| 3 | Read date, attendees, UID | iCalendar fields | High |
| 4 | Fetch mail headers around that date | IMAP `SINCE` / `BEFORE` | High |
| 5 | Keep messages tied to the meeting | Caller-side match on decoded subject and sender | Weakest |
| 6 | Retrieve message bodies | IMAP `FETCH` | High |

Two conclusions. First, the connectors need date-ranged queries and nothing more exotic —
no step requires Disk search or IMAP text search, precisely the two capabilities Yandex
lacks or implements poorly. Second, every filtering step belongs to the caller, not the
server: Claude composes these calls itself, and the connectors stay ignorant of any
notion of "project".

## Framings considered and set aside

**Feature parity with the Google connector set.** Rejected as the organising principle.
That set reflects what Google's REST APIs expose cheaply, not what a Yandex user needs.
A further reason is specific to MCP: every additional tool consumes context and increases
the chance the model picks the wrong one. Fewer, better-described tools beat broader
coverage.

**Scenario-first design.** Considered and rejected. Making cross-service project history
the centrepiece would have pulled a project key, join logic, and a local index into the
servers. The connectors are the deliverable; scenarios are composed at runtime and serve
as acceptance tests. The trace above is retained for what it produced — the list of
required primitives — not as a feature specification.

**A local search index.** Out of scope. It would only be needed to make Disk searchable
by name, and that need is better met by an honest enumeration with clear limits than by a
cache that can go stale, needs invalidation, and hides its own errors.

**A bespoke confirmation layer.** Rejected as duplication. MCP clients already prompt for
approval on every tool call. The protocol's `readOnlyHint` and `destructiveHint`
annotations let the server declare intent honestly and leave presentation to the client.
`dry_run` survives only on bulk operations, where previewing the affected set is the
actual requirement.

**Semantic or embedding search.** Out of scope for the same reason as the index: it adds
storage, freshness, and correctness problems the stated needs do not require.

## Authentication mechanics

One registered Yandex OAuth application covers Disk and Mail. Token acquisition runs the
Authorization Code flow against a transient local listener on
`http://localhost:8765/callback` opened by a setup command; the refresh token is stored
and renewed automatically.

Calendar takes a separate app password, entered during setup.

Secrets live in the system keychain via `keyring`, falling back to a `0600` file under
`~/.config/yandex-mcp/`. They are never passed as command-line arguments and never logged.

Account profiles are declared in `~/.config/yandex-mcp/config.toml` and selected with
`YANDEX_MCP_PROFILE`. Server hostnames are identical for personal and Yandex 360
accounts; the distinction exists so failures caused by organisation policy — an
administrator disabling app passwords or external clients — are reported clearly rather
than as an opaque authentication error.
