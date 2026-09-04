---
stepsCompleted: [1, 2, 3]
inputDocuments:
  - '_bmad-output/planning-artifacts/prds/prd-Yandex-Skills-2026-08-27/prd.md'
  - '_bmad-output/planning-artifacts/prds/prd-Yandex-Skills-2026-08-27/addendum.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-Yandex-Skills-2026-08-27/ARCHITECTURE-SPINE.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-Yandex-Skills-2026-08-27/DESIGN-NOTES.md'
---

# Yandex MCP Connectors for Claude - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for the Yandex MCP
Connectors, decomposing the PRD requirements and the architecture spine's invariants
into implementable stories.

There is no UX design contract: the product has no user interface, and its only consumer
is a language model calling tools. The UX Design Requirements section is intentionally
empty.

## Requirements Inventory

### Functional Requirements

**FR1 — Calendar connector** (CalDAV, app password)

- FR1.1: `calendar_list` returns the available calendars. Read-only.
- FR1.2: `calendar_events_list` returns events over a required date range, with recurring series expanded into concrete occurrences; each occurrence carries its own start and end, the series UID, and a `recurrence_id` when it belongs to a series. `EXDATE` cancellations are omitted; `RECURRENCE-ID` modifications are returned in modified form. Read-only.
- FR1.3: `calendar_event_get` returns full detail for one event or one occurrence. Read-only.
- FR1.4: `calendar_freebusy_query` returns busy intervals over a date range across selected calendars. Read-only.
- FR1.5: `calendar_event_create` creates an event. Write.
- FR1.6: `calendar_event_update` modifies an event, taking a mandatory `scope` of `occurrence` or `series` with no default, using the read ETag as a precondition and failing loudly on conflict. Destructive.
- FR1.7: `calendar_event_delete` removes an event, taking the same mandatory `scope`. Destructive.

**FR2 — Mail connector** (IMAP/SMTP, OAuth via XOAUTH2)

- FR2.1: `mail_folders_list` lists folders with message counts. Read-only.
- FR2.2: `mail_messages_list` returns message headers over a required date range; no all-history default. Date filtering uses IMAP `SINCE`/`BEFORE`; text criteria are applied client-side over decoded MIME subjects. Read-only.
- FR2.3: `mail_message_get` returns the plain-text body by default, truncated at a character limit with an explicit marker and a cursor for the remainder; an optional `strip_quotes` parameter removes quoted history and signatures, off by default. Read-only.
- FR2.4: `mail_attachments_list` returns attachment metadata for a message. Read-only.
- FR2.5: `mail_attachment_download` saves one attachment to a local path. Local write.
- FR2.6: `mail_flags_set` sets or clears read and flagged states. Write.
- FR2.7: `mail_draft_create` creates a draft, optionally as a reply. Write.
- FR2.8: `mail_send` sends a message; never deletes or modifies existing messages, but is irreversible. Destructive.
- FR2.9: `mail_messages_move` moves multiple messages between folders and supports `dry_run`. Destructive.
- FR2.10: `mail_messages_trash` moves multiple messages to Trash and supports `dry_run`. Destructive. No permanent delete exists.

**FR3 — Disk connector** (REST, OAuth)

- FR3.1: `disk_info` returns total and used space. Read-only.
- FR3.2: `disk_files_list` returns the contents of one folder. Read-only.
- FR3.3: `disk_files_find` finds files by name pattern or media type across the whole disk by enumerating the flat file listing and filtering locally; reports incompleteness if enumeration was truncated. Read-only.
- FR3.4: `disk_resource_get` returns metadata for one path. Read-only.
- FR3.5: `disk_file_download` downloads a file to a local path. Local write.
- FR3.6: `disk_file_upload` uploads a local file; overwriting an existing path requires an explicit flag. Write.
- FR3.7: `disk_folder_create` creates a folder. Write.
- FR3.8: `disk_resource_copy` copies a path. Write.
- FR3.9: `disk_resources_move` moves or renames multiple paths and supports `dry_run`, which returns the exact source and destination pairs without moving anything. Destructive.
- FR3.10: `disk_resources_trash` moves paths to Trash and supports `dry_run`. Destructive. No permanent delete exists.

**FR4 — Setup and authentication**

- FR4.1: A setup command runs the OAuth Authorization Code flow for Disk and Mail against a transient local listener, stores the refresh token, and renews it automatically.
- FR4.2: A setup command accepts the Calendar app password, explaining why it is needed and how to create it in Yandex ID.
- FR4.3: A verification command checks each configured service and reports per-service reachability with actionable failure causes.
- FR4.4: Profiles for personal Yandex ID and Yandex 360 accounts are created and switched without editing code.
- FR4.5: When a Yandex 360 administrator has disabled app passwords or external clients, the failure is reported as such rather than as a generic authentication error.

### NonFunctional Requirements

- NFR1: Bounded output. Every listing tool accepts `limit`, returns at most that many items, and documents its default. No call returns an unbounded set.
- NFR2: Explicit truncation. Any result cut short states so and carries a cursor for the remainder.
- NFR3: No silent under-return. A query that cannot be answered completely reports that fact rather than returning a partial set indistinguishable from a complete one. A local filter over a truncated source yields a result marked incomplete.
- NFR4: Honest errors. Failures name an actionable cause; raw protocol errors are wrapped and never converted into an empty successful result.
- NFR5: Determinism. Identical arguments produce identical results, and nothing happens that the caller did not request.
- NFR6: Declared destructiveness. Tools carry truthful MCP `readOnlyHint` and `destructiveHint` annotations. `dry_run` exists on bulk operations and performs no write.
- NFR7: Credential hygiene. Secrets live in the system keychain with a `0600` fallback file, and never appear in arguments, logs, error messages, or the repository.
- NFR8: Independent failure. Each server starts, fails, and is configured on its own; an unusable Calendar credential does not affect Disk or Mail.

### Additional Requirements

From the architecture spine and design notes:

- No starter template. Greenfield `uv` workspace with four packages plus a CLI package, per the spine's structural seed.
- AD-1: Layered paradigm. `tools/` never imports a protocol library; `client/` never imports `mcp` and is usable from a plain script.
- AD-2: One-way dependencies. Servers depend on `yandex_core`; the core depends on no server; no server imports another.
- AD-3: One async boundary. Every tool is `async def`; blocking libraries are called only through `anyio.to_thread.run_sync` and only from `client/`.
- AD-4: `Page[T]` and `Chunk` in `yandex_core` with required `complete` and `next_cursor` fields. No tool returns a bare list or bare string.
- AD-5: One error taxonomy in `yandex_core.errors`; no protocol exception type crosses out of `client/`.
- AD-6: `yandex_core.credentials` is the only module reading the keychain, fallback file, or environment.
- AD-7: All datetimes timezone-aware, ISO 8601 with explicit offset; naive datetimes rejected at construction.
- AD-8: Tool names follow `<server>_<object>_<verb>`.
- AD-9: Annotations derived from a single risk registry; a tool with no entry fails at server start.
- AD-10: Client layers expose no permanent-delete call at all.
- AD-11: Calendar mutations require explicit `scope`; ETag preconditions; Disk overwrite requires an explicit flag.
- AD-12: Filtering lives in `tools/`; clients never accept a text-match parameter.
- Logging goes to stderr only — stdout carries the MCP protocol on stdio transport.
- Tests split into `tests/unit` (fakes, no network) and `tests/live` (skipped unless `YANDEX_MCP_LIVE_TESTS=1`).
- MCP SDK 2.1.1 is the v2 line: `FastMCP` is now `MCPServer`, `mcp.server.fastmcp.*` moved to `mcp.server.mcpserver.*`, and `get_context()` is replaced by a declared `ctx` parameter. Online examples target v1 and will not run.
- The host `python3` is 3.9.6, below the `mcp` floor of 3.10; the interpreter is pinned to 3.13 through `uv` without touching the system Python.
- `recurring-ical-events` 3.8.2 performs RRULE and RDATE expansion, EXDATE handling, and RECURRENCE-ID overrides rather than hand-rolled recurrence logic.
- Calendar authentication cannot be automated: Yandex CalDAV rejects OAuth, so an app password is created by hand.

### UX Design Requirements

Not applicable. The product has no user interface; its only consumer is a language model
calling tools over MCP.

### FR Coverage Map

FR1.1–FR1.7: Epic 1 — Calendar tools, read paths before write paths
FR2.1–FR2.10: Epic 2 — Mail tools, read paths before write paths
FR3.1–FR3.10: Epic 3 — Disk tools, read paths before write paths
FR4.1: Epic 2 — OAuth Authorization Code flow, built where it is first exercised
FR4.2: Epic 1 — Calendar app password setup
FR4.3: Epic 1 — verification command, extended by each later epic to cover its service
FR4.4: Epic 1 — account profiles
FR4.5: Epic 1 — Yandex 360 policy failures reported honestly (app passwords), extended in Epic 2 (external clients)

All 32 functional requirements are mapped. NFR1–NFR8 and AD-1–AD-12 are cross-cutting and
appear as acceptance criteria throughout rather than as separate epics.

## Epic List

### Epic 1: Calendar connector, end to end

Claude can read and manage the calendar: list calendars, query events over a date range
with recurring series expanded into real occurrences, inspect busy time, and create,
modify, and delete events with an explicit `occurrence` or `series` scope. Setup and
verification exist, so a real account can be connected and confirmed working.

This epic also lays the shared foundation in `yandex_core`, because it is the first
consumer of it. Calendar goes first for a specific reason: it is the only service whose
authentication does **not** need OAuth. An app password is enough, so the connector can
be finished and used before the OAuth flow exists at all.

**FRs covered:** FR1.1, FR1.2, FR1.3, FR1.4, FR1.5, FR1.6, FR1.7, FR4.2, FR4.3, FR4.4, FR4.5

**Implementation notes:** the shared core built here — `Page` and `Chunk`, the error
taxonomy, the risk registry that derives annotations, cursors, `dry_run`, and the
credential layer — is built to what Calendar actually needs, and no further.

An earlier version of this plan required it to be "designed against all three protocols".
That requirement was dropped because nothing can verify it: with only CalDAV in front of
you, generalising to three protocols is guesswork wearing a serious expression. Epic 2
carries an explicit story to revisit the core once a second protocol exists and the real
commonality is visible. A checkable story replaces an uncheckable intention.

### Epic 2: Mail connector, end to end

Claude can work the mailbox: list folders, fetch headers over a date range, read message
bodies with honest truncation, inspect and download attachments, set flags, draft,
reply, send, move, and trash. The OAuth Authorization Code flow is built here, since Mail
is the first service that requires it.

**FRs covered:** FR2.1, FR2.2, FR2.3, FR2.4, FR2.5, FR2.6, FR2.7, FR2.8, FR2.9, FR2.10, FR4.1

**Two stories in this epic exist for reasons outside Mail itself.**

The first revisits the shared core now that a second protocol is in hand. Epic 1 built the
core against CalDAV alone; IMAP is the first evidence of what is genuinely common, and the
core is corrected against it rather than left shaped by whichever connector came first.

The second is the acceptance check, and it closes a real gap. The PRD's acceptance
scenario needs Calendar and Mail together, so it belongs to neither epic and would
otherwise go unverified — the thing the whole project exists for, tested by nobody. It is
not a separate epic; it is the last story here: Claude, given only these tools, answers a
question about a specific project meeting by composing calendar and mail calls itself,
with no cross-service logic on our side.

### Epic 3: Disk connector, end to end

Claude can work with files: report quota, browse folders, find files by name or media
type across the whole disk, read metadata, download, upload, create folders, copy, move,
and trash — with `dry_run` on the bulk operations that assisted tidying depends on.

Disk reuses the OAuth flow from Epic 2 rather than rebuilding it. It comes last because
it is the only connector that delivers nothing the PRD's acceptance scenario needs.

**FRs covered:** FR3.1, FR3.2, FR3.3, FR3.4, FR3.5, FR3.6, FR3.7, FR3.8, FR3.9, FR3.10

### Ordering and dependencies

Epic 1 stands alone completely. Epic 2 depends on Epic 1 only for the shared core, and
adds OAuth.

**Epic 3 depends on Epic 2 for the OAuth flow, and this dependency is real rather than
formal.** Mail and Disk share an authentication mechanism entirely; Calendar shares none
of it. A cut along authentication rather than service would have grouped Mail and Disk
into one epic and removed the dependency — it was considered and rejected, because that
epic would carry twenty tools and dissolve the connector boundaries this structure exists
to preserve. The dependency is accepted deliberately and recorded here so it is not
mistaken for an accident: if Epic 2 is abandoned or deferred, Epic 3 cannot start without
first building FR4.1.

Within every epic, read stories precede write stories. The read-before-write ordering was
dropped at epic level, not abandoned — a read-only tool is immediately useful and cannot
damage anything while its behaviour is still being learned.

---

## Epic 1: Calendar connector, end to end

Claude can read and manage the calendar, and a real account can be connected and
confirmed working. This epic also lays the shared core, built to what Calendar actually
needs and no further.

### Story 1.1: Connect a calendar and list it

As the operator,
I want a working Calendar MCP server that authenticates with my app password and lists my calendars,
So that I have a real end-to-end path before any feature work begins.

**Acceptance Criteria:**

**Given** a clean checkout and no Python 3.10+ on PATH
**When** the documented setup command runs
**Then** `uv` provisions Python 3.13 without altering the system interpreter
**And** the workspace exposes `yandex-core`, `yandex-calendar-mcp`, and `yandex-mcp-cli` packages

**Given** no stored credentials
**When** `yandex-mcp setup calendar` runs
**Then** it explains that Yandex CalDAV rejects OAuth and an app password must be created in Yandex ID (FR4.2)
**And** the entered password is stored in the system keychain, falling back to a `0600` file, and never appears in arguments or logs (NFR7, AD-6)

**Given** a configured profile selected by `YANDEX_MCP_PROFILE` (FR4.4)
**When** the server starts over stdio and `calendar_list` is called
**Then** it returns the account's calendars as a `Page` carrying `complete` and `next_cursor` (FR1.1, AD-4)
**And** the tool is annotated `readOnlyHint: true` from the risk registry, and a tool missing a registry entry prevents server start (AD-9)
**And** all logging goes to stderr, leaving stdout to the protocol

**Given** a wrong or revoked app password
**When** any calendar tool is called
**Then** the failure is reported as an authentication error naming the cause, never as an empty result (NFR4, AD-5)

**Given** the packages as built
**When** imports are checked, by test or by lint
**Then** `yandex_core` imports no server package, no server imports another server, and `tools/` imports no protocol library (AD-1, AD-2)

**Given** blocking CalDAV calls
**When** the code is inspected
**Then** every tool function is `async def`, and every blocking call is reached only through `anyio.to_thread.run_sync` from inside `client/` (AD-3)

**Given** the registered tools
**When** their names are checked
**Then** each follows `<server>_<object>_<verb>`, with the object segment omitted only where the server itself is the object (AD-8)

### Story 1.2: Verify a configured account

As the operator,
I want a command that checks each configured service and tells me precisely what is wrong,
So that setup problems are diagnosed in one step instead of by trial and error.

**Acceptance Criteria:**

**Given** a configured profile
**When** `yandex-mcp verify` runs
**Then** it reports reachability per service with an actionable cause for each failure (FR4.3)
**And** a service that is not configured is reported as unconfigured rather than failing

**Given** a Yandex 360 account whose administrator has disabled app passwords or external clients
**When** verification runs
**Then** the report names organisation policy as the cause rather than showing a generic authentication error (FR4.5)

**Given** an unreachable network
**When** verification runs
**Then** each service reports a transport error, and the command still completes and reports on every service (NFR8)

### Story 1.3: Query events over a date range

As Claude,
I want events over a date range with recurring series expanded into concrete occurrences,
So that I can reason about what actually happens on given days rather than about recurrence rules.

**Acceptance Criteria:**

**Given** a calendar containing single events and recurring series
**When** `calendar_events_list` is called with required `start` and `end`
**Then** each returned occurrence carries its own start and end, the series UID, and a `recurrence_id` when it belongs to a series (FR1.2)
**And** all timestamps are timezone-aware ISO 8601 with explicit offsets, and naive datetimes are rejected at construction (AD-7)

**Given** a series with `EXDATE` cancellations and `RECURRENCE-ID` modifications
**When** the range covers them
**Then** cancelled instances are absent and modified instances appear in their modified form, expanded by `recurring-ical-events`

**Given** more matching occurrences than `limit`
**When** the tool returns
**Then** the `Page` reports `complete: false` and carries a cursor for the remainder (NFR1, NFR2)

**Given** a title filter
**When** it is applied
**Then** filtering happens in the tool layer over a date-bounded fetch, the CalDAV client accepts no text-match parameter (AD-12)
**And** a filter applied over a truncated fetch yields a result marked incomplete (NFR3)

### Story 1.4: Read one event in full

As Claude,
I want the full detail of a single event or one occurrence of a series,
So that I can answer questions about a specific meeting without listing a range.

**Acceptance Criteria:**

**Given** a UID for a standalone event
**When** `calendar_event_get` is called
**Then** it returns the event's full detail including attendees, description, location, and its ETag (FR1.3)

**Given** a UID and a `recurrence_id` for one occurrence of a series
**When** the tool is called
**Then** it returns that occurrence, with modifications applied if it carries an override

**Given** a UID that does not exist
**When** the tool is called
**Then** it raises a not-found error naming the UID, never an empty success (NFR4, AD-5)

### Story 1.5: Inspect busy time

As Claude,
I want busy intervals over a date range,
So that I can find free slots without reading the content of every meeting.

**Acceptance Criteria:**

**Given** one or more selected calendars and a date range
**When** `calendar_freebusy_query` is called
**Then** it returns busy intervals with timezone-aware boundaries, merged across the selected calendars (FR1.4)

**Given** a range covering recurring meetings
**When** the query runs
**Then** occurrences of those series appear as busy intervals

**Given** a range longer than the documented maximum
**When** the query runs
**Then** the result is truncated and says so rather than silently narrowing the range (NFR2, NFR3)

### Story 1.6: Create an event

As Claude,
I want to create calendar events,
So that scheduling can be completed rather than only proposed.

**Acceptance Criteria:**

**Given** a title, start, end, and optional attendees and description
**When** `calendar_event_create` is called
**Then** the event is created and its UID and ETag are returned (FR1.5)
**And** the tool is annotated `readOnlyHint: false, destructiveHint: false` from the risk registry, since creation is additive (NFR6, AD-9)

**Given** a start or end without a timezone offset
**When** the tool is called
**Then** it is rejected before any request is sent (AD-7)

**Given** an end earlier than its start
**When** the tool is called
**Then** it is rejected with a validation error naming the problem

### Story 1.7: Update an event with an explicit scope

As Claude,
I want event changes to state whether they affect one occurrence or the whole series,
So that a change to a single meeting can never silently rewrite a year of history.

**Acceptance Criteria:**

**Given** an occurrence of a recurring series
**When** `calendar_event_update` is called with `scope: occurrence`
**Then** only that occurrence changes, expressed as a `RECURRENCE-ID` override, and other occurrences are untouched (FR1.6, AD-11)

**Given** the same occurrence
**When** the tool is called with `scope: series`
**Then** the whole series changes

**Given** a call that omits `scope`
**When** the tool is invoked
**Then** it fails validation; there is no default (AD-11)
**And** the tool is annotated `destructiveHint: true` (NFR6, AD-9)

**Given** an event modified by someone else since its ETag was read
**When** the update is attempted
**Then** it raises a conflict, changes nothing, and reports that the event moved underneath it (AD-11, NFR5)

### Story 1.8: Delete an event with an explicit scope

As Claude,
I want deletions to state whether they remove one occurrence or the whole series,
So that cancelling one meeting cannot destroy a recurring series.

**Acceptance Criteria:**

**Given** an occurrence of a recurring series
**When** `calendar_event_delete` is called with `scope: occurrence`
**Then** only that occurrence is cancelled, expressed as an `EXDATE`, and the series survives (FR1.7, AD-11)

**Given** `scope: series`
**When** the tool is called
**Then** the whole series is removed

**Given** a call that omits `scope`
**When** the tool is invoked
**Then** it fails validation, and the tool is annotated `destructiveHint: true`

**Given** any deletion
**When** it completes
**Then** no permanent-delete call exists anywhere in the client layer to have been reached (AD-10)

---

## Epic 2: Mail connector, end to end

Claude can work the mailbox. The OAuth flow is built here, the shared core is corrected
against a second protocol, and the project's acceptance scenario is verified.

### Story 2.1: Authorise the mailbox and list its folders

As the operator,
I want to grant Mail access through OAuth and see my folders,
So that the mailbox is connected and proven reachable before any message work.

**Acceptance Criteria:**

**Given** no stored Mail credentials
**When** `yandex-mcp login mail` runs
**Then** it opens the browser to Yandex OAuth, receives the code on a transient local listener, exchanges it for tokens, and stores the refresh token in the keychain (FR4.1, NFR7)
**And** the requested scopes are `mail:imap_full` and `mail:smtp`, and no secret is passed as a command-line argument

**Given** an expired access token
**When** any mail tool is called
**Then** the token is refreshed automatically and the call proceeds, without prompting

**Given** a valid token
**When** `mail_folders_list` is called
**Then** it returns folders with message counts as a `Page` (FR2.1)
**And** IMAP authenticates via XOAUTH2, and `client/` is callable from a plain script with no `mcp` import present (AD-1)

**Given** an organisation that has disabled external clients
**When** authentication fails
**Then** the cause is reported as organisation policy, not as a bad password (FR4.5)

### Story 2.2: List message headers over a date range

As Claude,
I want message headers over a date range that I can filter,
So that I can locate relevant correspondence without pulling an entire mailbox into context.

**Acceptance Criteria:**

**Given** a mailbox spanning several years
**When** `mail_messages_list` is called without `start` and `end`
**Then** it fails validation; there is no all-history default (FR2.2)

**Given** a required date range
**When** the tool runs
**Then** it fetches using IMAP `SINCE` and `BEFORE` only, and returns UID, date, from, to, subject, flags, size, and attachment presence

**Given** subjects encoded as MIME encoded-words, including Cyrillic
**When** results are returned
**Then** subjects are decoded before matching and before display

**Given** a sender or subject filter
**When** it is applied
**Then** filtering happens in the tool layer over the date-bounded fetch, and the IMAP client accepts no text-match parameter (AD-12)
**And** a filter applied over a truncated fetch yields `complete: false` (NFR3)

**Given** more messages than `limit`
**When** the tool returns
**Then** the cursor encodes IMAP UID position opaquely, and the caller never parses it

### Story 2.3: Read a message body with honest truncation

As Claude,
I want message bodies that tell me when they have been cut,
So that I never treat a fragment of a message as the whole of it.

**Acceptance Criteria:**

**Given** a message within the character limit
**When** `mail_message_get` is called
**Then** it returns the plain-text body as a `Chunk` with `complete: true` (FR2.3, AD-4)

**Given** a message longer than the limit
**When** the tool is called
**Then** the returned `Chunk` carries `complete: false`, an explicit truncation marker in the text, and a cursor for the remainder (NFR2)
**And** calling again with that cursor returns the next segment

**Given** an HTML-only message
**When** the tool is called
**Then** a readable plain-text rendering is returned, and the fact that it was converted is stated

**Given** `strip_quotes: true`
**When** the tool is called
**Then** quoted history and signatures are removed and the response says that content was removed; the parameter is off by default so nothing is discarded silently (NFR3)

### Story 2.4: Inspect and download attachments

As Claude,
I want to see what a message carries and fetch a specific attachment,
So that I can work with documents without downloading everything blindly.

**Acceptance Criteria:**

**Given** a message with attachments
**When** `mail_attachments_list` is called
**Then** it returns filename, MIME type, and size per attachment without transferring their content (FR2.4)

**Given** an attachment identifier and a local path
**When** `mail_attachment_download` is called
**Then** the file is written to that path and the written path and byte count are returned (FR2.5)

**Given** a target path that already exists
**When** the download is attempted
**Then** it refuses unless an explicit overwrite flag is set

**Given** a filename from the message
**When** the path is resolved
**Then** path traversal outside the requested directory is rejected

### Story 2.5: Correct the shared core against a second protocol

As the operator,
I want the shared core reworked now that two protocols are in hand,
So that it reflects real commonality instead of the shape of whichever connector came first.

**Acceptance Criteria:**

**Given** cursor handling written for CalDAV and then for IMAP UIDs
**When** the core is reviewed
**Then** one cursor abstraction in `yandex_core.paging` serves both, encoding per-protocol state opaquely, with no protocol branch in the tool layer

**Given** `Chunk` introduced in Story 2.3 alongside `Page` from Epic 1
**When** the core is reviewed
**Then** both live in `yandex_core.results` with the same required `complete` and `next_cursor` contract (AD-4)

**Given** error mapping written twice, for CalDAV and for IMAP and SMTP
**When** the core is reviewed
**Then** `yandex_core.errors` covers both without either connector needing a case the other cannot express, and no protocol exception type crosses out of `client/` (AD-5)

**Given** the risk registry built when only Calendar existed
**When** mail's destructive operations are added
**Then** the registry expresses them without special-casing, and a tool absent from it still prevents server start (AD-9)

**Given** the corrected core
**When** the Calendar server's tests run
**Then** they pass unchanged, or their changes are deliberate and recorded

**Given** two connectors now exist
**When** dependency and async checks run across both
**Then** neither server imports the other, `yandex_core` imports neither, and no blocking call has leaked out of `client/` into `tools/` (AD-2, AD-3)

**Given** twenty-six tool names now span two connectors
**When** they are checked against the scheme
**Then** all conform to `<server>_<object>_<verb>` (AD-8)

### Story 2.6: Set message flags

As Claude,
I want to mark messages read, unread, or flagged,
So that triage results are recorded in the mailbox rather than only reported.

**Acceptance Criteria:**

**Given** one or more message UIDs in a folder
**When** `mail_flags_set` is called
**Then** the requested flags are set or cleared and the resulting flag state is returned (FR2.6)

**Given** a UID that no longer exists
**When** the tool is called
**Then** it reports which UIDs were not found rather than failing silently or reporting success for all (NFR3, NFR4)

**Given** the operation
**When** annotations are read
**Then** it is `readOnlyHint: false, destructiveHint: false`, since flag changes are reversible

### Story 2.7: Create drafts and replies

As Claude,
I want to prepare messages without sending them,
So that outgoing mail can be reviewed by a person before it leaves.

**Acceptance Criteria:**

**Given** a recipient, subject, and body
**When** `mail_draft_create` is called
**Then** a draft appears in the Drafts folder and its UID is returned (FR2.7)

**Given** the UID of a message being replied to
**When** the tool is called with it
**Then** the draft carries correct `In-Reply-To` and `References` headers and a subject prefixed once, not repeatedly

**Given** a draft is created
**When** it completes
**Then** nothing has been sent, and the tool is annotated `destructiveHint: false`

### Story 2.8: Send a message

As Claude,
I want to send mail when explicitly asked,
So that correspondence can be completed rather than only drafted.

**Acceptance Criteria:**

**Given** a complete message or the UID of an existing draft
**When** `mail_send` is called
**Then** it is sent over SMTP with XOAUTH2 and a copy is placed in Sent (FR2.8)

**Given** the operation is irreversible
**When** annotations are read
**Then** it is `destructiveHint: true`, so the client presents a confirmation, and no confirmation machinery of our own duplicates that (NFR6)

**Given** a send that fails midway
**When** the error is reported
**Then** it states whether the message was sent, never leaving that ambiguous (NFR4)

### Story 2.9: Move and trash messages in bulk

As Claude,
I want to reorganise or discard many messages at once, previewing first,
So that a mistaken bulk operation is visible before it happens rather than after.

**Acceptance Criteria:**

**Given** several message UIDs and a destination folder
**When** `mail_messages_move` is called with `dry_run: true`
**Then** it returns exactly the messages that would move, and nothing is moved (FR2.9, NFR6)

**Given** the same call with `dry_run: false`
**When** it runs
**Then** the messages move and the result lists what moved and what did not, with reasons

**Given** several UIDs
**When** `mail_messages_trash` is called
**Then** they are moved to Trash, recoverable by the user (FR2.10, AD-10)

**Given** the client layer
**When** it is inspected
**Then** it exposes no permanent-delete call at all, so no tool can reach one by accident (AD-10)

### Story 2.10: Answer a real cross-service question

As the operator,
I want to confirm that Claude can answer a question needing both connectors, composing the calls itself,
So that the primitives are proven sufficient without any cross-service logic on our side.

**Acceptance Criteria:**

**Given** the Calendar and Mail servers configured against a real account
**When** Claude is asked for the outcome of a specific project meeting, named as it appears in the calendar
**Then** it locates the meeting with `calendar_events_list` over a date window, takes its date, queries `mail_messages_list` around that date, and retrieves the relevant body with `mail_message_get`

**Given** that sequence
**When** the servers are inspected
**Then** no server contains logic joining calendar data to mail data, no shared project key exists, and no tool takes a project argument

**Given** no matching meeting in the window
**When** the question is asked
**Then** the tools report finding nothing rather than returning a plausible unrelated meeting (NFR3)

**Given** the run completes
**When** context usage is reviewed
**Then** no single tool call returned an unbounded result (NFR1)

---

## Epic 3: Disk connector, end to end

Claude can work with files on Yandex Disk. This epic reuses the OAuth flow built in Epic 2
and adds no new authentication mechanism.

### Story 3.1: Connect Disk and report space

As the operator,
I want Disk authorised and reporting my quota,
So that the connector is proven reachable before any file work.

**Acceptance Criteria:**

**Given** the OAuth flow built in Epic 2
**When** `yandex-mcp login disk` runs
**Then** it reuses that flow, requesting `cloud_api:disk.info`, `cloud_api:disk.read`, and `cloud_api:disk.write`, and stores the refresh token as Mail's is stored (FR4.1, NFR7)

**Given** a valid token
**When** `disk_info` is called
**Then** it returns total and used space (FR3.1)

**Given** `yandex-mcp verify`
**When** it runs
**Then** it now reports Disk alongside Calendar and Mail, each independently (FR4.3, NFR8)

### Story 3.2: Browse a folder

As Claude,
I want the contents of a folder,
So that I can navigate the disk the way a person would.

**Acceptance Criteria:**

**Given** a folder path
**When** `disk_files_list` is called
**Then** it returns entries with name, type, size, and modified time as a `Page` (FR3.2)

**Given** a folder with more entries than `limit`
**When** the tool returns
**Then** `complete` is false and a cursor is supplied (NFR1, NFR2)

**Given** a path that does not exist
**When** the tool is called
**Then** it raises a not-found error naming the path, never an empty listing (NFR4)

### Story 3.3: Find files across the whole disk

As Claude,
I want to find files by name pattern or type anywhere on the disk,
So that I can locate a document without knowing which folder holds it.

**Acceptance Criteria:**

**Given** the Disk API offers no search of any kind
**When** `disk_files_find` is called
**Then** it enumerates the flat file listing at `limit=1000` per request and filters locally on name pattern and `media_type` (FR3.3, AD-12)

**Given** a disk under a thousand files
**When** the search runs
**Then** enumeration completes in a single request and the result is marked `complete: true`

**Given** enumeration that was truncated or interrupted
**When** results are returned
**Then** they are explicitly marked as drawn from a partial scan, so an absent match is never mistaken for a proven absence (NFR3)

**Given** more matches than `limit`
**When** the tool returns
**Then** matches are paginated with a cursor, independent of the enumeration itself

### Story 3.4: Read metadata for a path

As Claude,
I want the details of one file or folder,
So that I can check size, type, or modification time without listing its parent.

**Acceptance Criteria:**

**Given** a path to a file or folder
**When** `disk_resource_get` is called
**Then** it returns name, type, size, MIME type, created and modified times, and a public link if one exists (FR3.4)
**And** timestamps are timezone-aware ISO 8601 with explicit offsets (AD-7)

**Given** a path that does not exist
**When** the tool is called
**Then** it raises a not-found error naming the path

### Story 3.5: Download a file

As Claude,
I want to fetch a file to local disk,
So that its contents can be read and worked with.

**Acceptance Criteria:**

**Given** a remote path and a local destination
**When** `disk_file_download` is called
**Then** the file is written there and the written path and byte count are returned (FR3.5)

**Given** a local path that already exists
**When** the download is attempted
**Then** it refuses unless an explicit overwrite flag is set

**Given** a download interrupted midway
**When** the error is reported
**Then** no partial file is left presenting itself as complete (NFR3)

### Story 3.6: Upload a file

As Claude,
I want to put a local file onto the disk,
So that work produced locally can be stored where it belongs.

**Acceptance Criteria:**

**Given** a local file and a remote path
**When** `disk_file_upload` is called
**Then** the file is uploaded and its resulting metadata is returned (FR3.6)

**Given** a remote path that already exists
**When** the upload is attempted without an explicit overwrite flag
**Then** it refuses and says the path is occupied (AD-11)

**Given** an upload with the overwrite flag set
**When** annotations are read
**Then** the tool is `destructiveHint: true`, since it can replace existing content (NFR6, AD-9)

### Story 3.7: Create folders and copy paths

As Claude,
I want to make folders and duplicate files,
So that the disk can be reorganised additively before anything is moved or removed.

**Acceptance Criteria:**

**Given** a path whose parent exists
**When** `disk_folder_create` is called
**Then** the folder is created and its metadata returned (FR3.7)

**Given** a path whose parent does not exist
**When** the tool is called
**Then** it reports the missing parent rather than creating intermediate folders silently (NFR5)

**Given** a source and destination path
**When** `disk_resource_copy` is called
**Then** the resource is copied, the original is untouched, and the tool is annotated `destructiveHint: false` (FR3.8)

**Given** a destination that already exists
**When** the copy is attempted
**Then** it refuses unless an explicit overwrite flag is set

### Story 3.8: Move and trash paths in bulk

As Claude,
I want to reorganise or discard many files at once, previewing first,
So that tidying an accumulated disk is safe to attempt.

**Acceptance Criteria:**

**Given** several source and destination pairs
**When** `disk_resources_move` is called with `dry_run: true`
**Then** it returns exactly the pairs that would be acted on, and nothing moves (FR3.9, NFR6)

**Given** the same call with `dry_run: false`
**When** it runs
**Then** paths are moved and the result lists what moved and what did not, with reasons per failure

**Given** a move where one destination is occupied
**When** the operation runs
**Then** that one is refused and reported, and the others still complete; the outcome is never reported as wholly successful (NFR3, NFR4)

**Given** several paths
**When** `disk_resources_trash` is called
**Then** they are moved to Trash, recoverable by the user, and `dry_run` is supported (FR3.10, AD-10)

**Given** the client layer
**When** it is inspected
**Then** it exposes no permanent-delete call at all (AD-10)
