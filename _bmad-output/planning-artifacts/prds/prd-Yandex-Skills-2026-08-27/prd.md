---
title: Yandex MCP Connectors for Claude — PRD
status: draft
created: 2026-08-27
updated: 2026-08-27
---

# Yandex MCP Connectors for Claude — PRD

## Overview

Three MCP servers that give Claude access to Yandex Disk, Yandex Mail, and Yandex
Calendar. Each server runs locally over stdio and speaks its service's native protocol.

The connectors are the deliverable. Claude composes them at runtime; the servers hold no
knowledge of how they will be combined, and no logic spans more than one service.

## Goals

- Give Claude complete, honest access to the three services for one practitioner.
- Behave predictably enough to be trusted with write access to real correspondence.
- Keep every response bounded so tool output never displaces reasoning context.

## Non-goals

- A hosted or multi-user connector.
- Any cross-service logic, shared project key, local search index, or semantic search.
- Yandex services beyond these three.
- A bespoke confirmation layer duplicating what MCP clients already provide.

## Quality bar

These are the cross-cutting requirements. They bind every tool in every server.

**NFR1 — Bounded output.** Every listing tool accepts `limit` and returns at most that
many items, with a documented default. No call can return an unbounded set.

**NFR2 — Explicit truncation.** When a result is cut short — by `limit`, by a character
cap, or by any other rule — the response states so and carries a cursor for the
remainder. Silence about incompleteness is a defect.

**NFR3 — No silent under-return.** A query that cannot be answered completely reports
that fact rather than returning a partial set indistinguishable from a complete one.
This governs client-side filtering in particular: if the underlying enumeration was
itself truncated, the filtered result is explicitly marked as drawn from a partial scan.

**NFR4 — Honest errors.** Failures name the cause in terms the caller can act on:
expired token, missing app password, organisation policy, rate limit, network. Raw
protocol errors are wrapped, never surfaced bare, and never converted into an empty
result.

**NFR5 — Determinism.** Identical arguments produce identical results, and nothing
happens that the caller did not request.

**NFR6 — Declared destructiveness.** Tools carry MCP `readOnlyHint` and
`destructiveHint` annotations truthfully. Approval is the client's responsibility.
`dry_run` exists only on bulk operations, where it returns the exact set that would be
affected without touching it.

**NFR7 — Credential hygiene.** Secrets live in the system keychain, falling back to a
`0600` file under `~/.config/yandex-mcp/`. They never appear in arguments, logs, error
messages, or the repository.

**NFR8 — Independent failure.** Each server starts, fails, and is configured on its own.
An unusable Calendar credential does not affect Disk or Mail.

## Common behaviours

**Pagination.** Listing tools take `limit` and `cursor` and return `next_cursor` plus a
`truncated` flag. Cursors are opaque to the caller.

**Account profiles.** The active profile is selected by the `YANDEX_MCP_PROFILE`
environment variable at server start, not per call. Profiles are declared in
`~/.config/yandex-mcp/config.toml` and cover both personal Yandex ID and Yandex 360
domain accounts.

**Tool names.** All tools follow `<server>_<object>_<verb>`, with the object segment
omitted only where the server itself is the object (`disk_info`, `calendar_list`,
`mail_send`). Governed by AD-8 in the architecture spine.

**Dates.** All timestamps are ISO 8601 with explicit offsets. Date-ranged tools take
`start` and `end`.

## FR1 — Calendar connector

CalDAV against `caldav.yandex.ru`, authenticated with an app password.

Meetings are predominantly recurring, so expanding series is core behaviour rather than
an edge case.

| ID | Tool | Purpose | Annotation |
|---|---|---|---|
| FR1.1 | `calendar_list` | List available calendars | read-only |
| FR1.2 | `calendar_events_list` | Events over a date range, recurring series expanded into concrete occurrences | read-only |
| FR1.3 | `calendar_event_get` | Full detail for one event or one occurrence | read-only |
| FR1.4 | `calendar_freebusy_query` | Busy intervals over a date range across selected calendars | read-only |
| FR1.5 | `calendar_event_create` | Create an event | write |
| FR1.6 | `calendar_event_update` | Modify an event | destructive |
| FR1.7 | `calendar_event_delete` | Remove an event | destructive |

**FR1.2** requires `start` and `end`. Each returned occurrence carries its own start and
end, the series UID, and a `recurrence_id` when it belongs to a series, so callers can
distinguish an instance from a standalone event. Cancelled instances (`EXDATE`) are
omitted; modified instances (`RECURRENCE-ID`) are returned in their modified form.

**FR1.6 and FR1.7** take a mandatory `scope` of `occurrence` or `series`, with no
default. This prevents a request aimed at one cancelled meeting from destroying a
year-long weekly series. `this-and-following` is deliberately deferred.

**FR1.6** uses ETags for optimistic concurrency and fails loudly on conflict rather than
overwriting.

## FR2 — Mail connector

IMAP and SMTP against `imap.yandex.ru` and `smtp.yandex.ru`, authenticated with OAuth via
XOAUTH2.

The mailbox spans several years, so no tool defaults to scanning all history.

| ID | Tool | Purpose | Annotation |
|---|---|---|---|
| FR2.1 | `mail_folders_list` | List folders with message counts | read-only |
| FR2.2 | `mail_messages_list` | Message headers over a date range | read-only |
| FR2.3 | `mail_message_get` | Message body | read-only |
| FR2.4 | `mail_attachments_list` | Attachment metadata for a message | read-only |
| FR2.5 | `mail_attachment_download` | Save one attachment to a local path | write (local) |
| FR2.6 | `mail_flags_set` | Set or clear read and flagged states | write |
| FR2.7 | `mail_draft_create` | Create a draft, optionally as a reply | write |
| FR2.8 | `mail_send` | Send a message | destructive |
| FR2.9 | `mail_messages_move` | Move messages between folders | destructive |
| FR2.10 | `mail_messages_trash` | Move messages to Trash | destructive |

**FR2.2** requires an explicit `start` and `end`; there is no all-history default.
Date filtering uses IMAP `SINCE` and `BEFORE`, which are dependable. Text criteria are
applied client-side over the fetched headers, because Yandex's server-side text search
misreports on Cyrillic — subjects are decoded from MIME encoded-words before matching.
Per NFR3, a filtered result drawn from a truncated fetch is marked as such.

**FR2.3** returns the plain-text body by default, truncated at a character limit with an
explicit marker and a cursor for the remainder. `[ASSUMPTION]` An optional
`strip_quotes` parameter removes quoted history and signatures; it is off by default,
because silently discarding quoted text would violate NFR3.

**FR2.8** never deletes and never modifies existing messages, but is irreversible and is
annotated accordingly.

**FR2.9 and FR2.10** accept multiple message identifiers and support `dry_run`.

**Deletion is always to Trash.** No tool performs a permanent delete.

## FR3 — Disk connector

REST against `cloud-api.yandex.net/v1/disk`, authenticated with OAuth.

The API offers no search of any kind. At the target scale — under a thousand files — the
entire disk enumerates in a single request, so search is a complete local filter rather
than a crawl.

| ID | Tool | Purpose | Annotation |
|---|---|---|---|
| FR3.1 | `disk_info` | Total and used space | read-only |
| FR3.2 | `disk_files_list` | Contents of one folder | read-only |
| FR3.3 | `disk_files_find` | Find files by name pattern or media type across the whole disk | read-only |
| FR3.4 | `disk_resource_get` | Metadata for one path | read-only |
| FR3.5 | `disk_file_download` | Download a file to a local path | write (local) |
| FR3.6 | `disk_file_upload` | Upload a local file | write |
| FR3.7 | `disk_folder_create` | Create a folder | write |
| FR3.8 | `disk_resource_copy` | Copy a path | write |
| FR3.9 | `disk_resources_move` | Move or rename paths | destructive |
| FR3.10 | `disk_resources_trash` | Move paths to Trash | destructive |

**FR3.3** enumerates via the flat file listing and filters locally on name pattern and
`media_type`. If enumeration is truncated, the result says so per NFR3.

**FR3.9** accepts multiple paths and supports `dry_run`, which returns the exact list of
source and destination pairs without moving anything. This is the tool that assisted
tidying of an unsorted disk will use, and previewing before acting is its main safeguard.

**Deletion is always to Trash.** No tool performs a permanent delete.

## FR4 — Setup and authentication

| ID | Requirement |
|---|---|
| FR4.1 | A setup command runs the OAuth Authorization Code flow for Disk and Mail against a transient local listener, stores the refresh token, and renews it automatically. |
| FR4.2 | A setup command accepts the Calendar app password, explaining why it is needed and how to create it in Yandex ID. |
| FR4.3 | A verification command checks each configured service and reports per-service reachability with actionable failure causes. |
| FR4.4 | Profiles for personal and Yandex 360 accounts are created and switched without editing code. |
| FR4.5 | When a Yandex 360 administrator has disabled app passwords or external clients, the failure is reported as such rather than as a generic authentication error. |

Calendar setup cannot be automated: Yandex CalDAV does not accept OAuth tokens, so the
app password is created by hand. This is an explained step, not a silent prerequisite.

## Build order

1. **Calendar and Mail, read paths.** FR1.1–FR1.4, FR2.1–FR2.4, and the FR4 setup needed
   to reach them. Complete when a real question is answerable end to end.
2. **Mail and Calendar, write paths.** FR1.5–FR1.7, FR2.5–FR2.10.
3. **Disk.** FR3 entire.

Read paths precede write paths throughout: a read-only connector is immediately useful
and cannot damage anything while its behaviour is still being learned.

## Acceptance

The connectors are sufficient when Claude, given only these tools, can answer a question
none of them answers alone — for example, locating the outcome of a specific project
meeting by querying the calendar for the meeting date and then the mailbox around it —
composing the calls itself, with no cross-service logic in any server.

Alongside that:

1. Setup, including the manual app-password step, completes in one sitting from written
   instructions.
2. No tool call floods the context window.
3. Credentials never reach logs, arguments, or the repository.

## Open items

| # | Item | Owner | Revisit |
|---|---|---|---|
| 1 | Default date window for date-ranged tools when the caller supplies none | Author | During phase 1 implementation, from observed usage |
| 2 | Whether `strip_quotes` should default on after real use `[ASSUMPTION]` | Author | After phase 1 |
| 3 | Empirical confirmation that Cyrillic IMAP text search is unreliable | Author | First mailbox test |
