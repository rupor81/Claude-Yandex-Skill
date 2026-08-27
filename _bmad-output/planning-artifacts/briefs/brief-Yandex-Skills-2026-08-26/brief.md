---
title: Yandex MCP Connectors for Claude
status: draft
created: 2026-08-26
updated: 2026-08-27
---

# Yandex MCP Connectors for Claude

## What this is

Three MCP connectors that give Claude access to Yandex Disk, Yandex Mail, and Yandex
Calendar — the Yandex counterpart to the Google connector set.

The deliverable is the connectors themselves. What gets built on top of them is not
part of this project: Claude composes tool calls at runtime, and useful work emerges
from having primitives that behave correctly. The job here is to make those primitives
correct and quiet.

## Who this is for

A single practitioner working across several concurrent client projects. This is personal
instrumentation, not a product. It needs no onboarding flow, no multi-tenancy, and no
support story — but it will hold live credentials to real correspondence, so it has to
be trustworthy.

## The quality bar

"Correct and quiet" is the whole specification, so it is worth stating plainly. A
connector meets the bar when:

- **Output is bounded and predictable.** Every listing paginates and truncates by a
  documented rule. No tool call can flood the context window because a folder happened
  to hold four thousand files.
- **Errors are honest.** A failure says what failed and why — an expired token, a
  disabled app password, an organisation policy — rather than surfacing a raw protocol
  error or, worse, an empty result that looks like "nothing found".
- **Results do not silently under-return.** If a query cannot be answered completely,
  the tool says so instead of returning a partial set that reads as complete.
- **Behaviour is deterministic.** The same call with the same arguments returns the same
  thing, and nothing happens that the caller did not ask for.
- **Nothing surprising is destroyed.** Destructive operations are declared as such
  through MCP tool annotations, so the client shows them for what they are.

## Scope

**Disk.** Browse folders, read metadata, download and upload file content, create
folders, move, copy, rename, and trash. Locating a file by name is a client-side filter
over an enumerated listing, because the API offers nothing else.

**Mail.** List folders, fetch message headers over a date range, retrieve message bodies
and attachments, manage flags, create drafts, reply, and send.

**Calendar.** List calendars, query events over a date range, read event detail, create,
update, and delete events, and expand recurring series into concrete occurrences.

**Out of scope.** A hosted remote connector; other users; a local search index; semantic
search; any cross-service logic that joins data from more than one connector.

## Build order

1. **Calendar and Mail, read paths.** Enough to answer a real question end to end.
2. **Mail and Calendar, write paths.** Drafts, replies, sending, event changes.
3. **Disk.** Browsing and file operations.

Read paths come first throughout, because a connector that reads correctly is
immediately useful and cannot damage anything while its behaviour is still being learned.

## Platform constraints

Yandex has no unified REST API. The three services differ in protocol, in authentication,
and in what can be asked of them.

| Service | Protocol | Authentication | Server-side search |
|---|---|---|---|
| Disk | REST, `cloud-api.yandex.net/v1/disk` | OAuth (`cloud_api:disk.*`) | **None at all** |
| Mail | IMAP / SMTP | OAuth via XOAUTH2 (`mail:imap_full`, `mail:smtp`) | Unreliable with Cyrillic |
| Calendar | CalDAV, `caldav.yandex.ru` | **App password only** — OAuth is not accepted | Date ranges only |

Two consequences matter for the connectors:

**Calendar setup cannot be automated.** The user must create an app password in Yandex ID
by hand. This is a Yandex limitation with no workaround, so it becomes an explicit,
explained setup step.

**Filtering happens client-side, anchored on dates.** Disk exposes only a flat, paginated,
alphabetical file list with no name or content filtering. Mail's text search misreports
on Cyrillic. Date ranges are the one filter both IMAP and CalDAV handle dependably, so
tools take date windows and filter the rest locally.

## Architecture in brief

A Python monorepo. A shared `yandex_core` package owns only what all three genuinely
share — account profiles, OAuth, secret storage, and the error taxonomy. Three
independent MCP servers own their protocols. The three protocols share no data model,
and nothing beyond those common concerns is unified.

Servers run locally over stdio for Claude Code and Claude Desktop. The core stays
decoupled from transport so the same logic can later be served over HTTP with OAuth
without a rewrite.

Both personal Yandex ID and Yandex 360 domain accounts are supported through switchable
profiles.

Safety rests on the MCP protocol rather than on machinery of our own: tools carry
`readOnlyHint` and `destructiveHint` annotations and the client handles approval. A
`dry_run` argument exists only on bulk operations, where seeing the list before acting
is the point.

## Acceptance

The connectors are good enough when Claude can answer a question none of them can answer
alone — for example, *"find the results of the meeting with R-Pharm about Accord"* —
composing calendar and mail calls by itself, with no cross-service logic on our side.
This is a test of the primitives, not a feature to be built.

Alongside it:

1. Setup, including the manual app-password step, is completable in one sitting from
   written instructions.
2. No tool call floods the context window.
3. Credentials never reach logs, arguments, or the repository.

## Key risks

**Cyrillic IMAP search is reported broken.** The design already routes around it by
filtering locally, so the risk is contained — but it needs an empirical check rather than
an assumption in either direction.

**Recurring events are expensive.** CalDAV stores a series as one record plus exceptions;
expanding it into real dates is the client's job. If meetings are mostly weekly syncs,
this is a substantial share of the Calendar work.

**Disk enumeration cost scales with file count.** With no search endpoint, finding a file
by name means listing everything. Ten thousand files is tolerable; a hundred thousand
needs a different approach.

**Yandex 360 administrators can disable app passwords**, making the Calendar connector
unusable on a corporate account. Detectable at setup, not fixable in code.

## Open questions

1. Roughly how many files are on Disk, and how large is the mailbox? *Determines whether
   Disk enumeration is tolerable as designed.*
2. Are calendar meetings mostly one-off or recurring? *Sizes the Calendar work.*
3. What default date window should date-ranged tools use when the caller gives none?
