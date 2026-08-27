---
title: Addendum — Yandex MCP Connectors PRD
status: draft
created: 2026-08-27
updated: 2026-08-27
---

# Addendum

Rationale and mechanism behind the PRD's requirements. Protocol detail verified during
the brief is not repeated here — see
`planning-artifacts/briefs/brief-Yandex-Skills-2026-08-26/addendum.md`.

## Why these tools and not more

Twenty-six tools across three servers, roughly seven to ten each. The count is
deliberate. In MCP every exposed tool occupies context and adds a way for the model to
choose wrongly, so coverage is not free. Each tool here earns its place by being a
distinct operation a caller would actually ask for — not by mirroring an API surface.

Operations left out on purpose: publishing public share links on Disk, server-side mail
filters and rules, calendar sharing and delegation, and permanent deletion anywhere.
Each is either rare in the intended use or dangerous enough to be worth an explicit trip
to the web interface.

## Why `scope` has no default

Most meetings in the target calendar are recurring. In CalDAV a series is a single
`VEVENT` carrying an `RRULE`, so "delete the event" is genuinely ambiguous at the
protocol level: it can mean adding one `EXDATE` or removing the whole record.

Any default would be wrong some of the time, and one direction is catastrophic — a
model intending to cancel a single meeting would silently erase a year of history.
Requiring `scope` on every mutation forces the distinction to be made explicitly by
whoever calls the tool. The cost is one extra argument; the alternative is an
unrecoverable mistake that looks like success.

`this-and-following` is a legitimate third case, expressed by splitting the series into
two records. It is deferred because it roughly doubles the mutation logic and is rarely
needed.

## Why filtering happens client-side

Yandex offers no server-side filter worth trusting. Disk has none at all. Mail's text
search is reported to misreport on Cyrillic when criteria are combined, and subjects
arrive MIME-encoded, so matching them correctly requires decoding on the client
regardless. CalDAV `text-match` may work but cannot be verified to never under-return.

Under-returning is the specific danger. A filter that silently omits matches produces an
answer that looks complete and is not, and the model has no way to detect this. Fetching
by date — the one filter both IMAP and CalDAV handle dependably — and filtering locally
trades a little bandwidth for a guarantee about completeness.

This is why NFR3 is written as a hard requirement rather than a nicety: it is the
property the whole filtering strategy exists to preserve.

## Why Disk stopped being a problem

The absence of any search endpoint looked like the project's most serious constraint
until the target scale was established. Under a thousand files, `limit=1000` returns the
entire disk in one request. Local filtering over that set is both instant and provably
complete.

This holds only at this scale. Past roughly ten thousand files the enumeration becomes
slow enough to need caching and progress reporting, and past a hundred thousand it needs
a different design entirely. The PRD's approach is correct for the stated volume and
should be revisited if that volume changes by an order of magnitude.

## Why deletion is always to Trash

Yandex provides a trash on both Disk and Mail. Routing every removal through it makes
every destructive operation recoverable by the user without involving the connectors at
all. Permanent deletion offers no benefit here that would justify making a model's
mistake unrecoverable.

## Pagination

Cursors are opaque strings rather than numeric offsets, because the three services
paginate differently — Disk by `offset`, IMAP by UID ranges, CalDAV not at all beyond
narrowing the date range. An opaque cursor lets each server encode what it needs while
callers see one consistent contract.

Every truncated response carries both `next_cursor` and an explicit `truncated` flag.
The flag is redundant with the cursor's presence and is included anyway, because a model
reading a response should not have to infer incompleteness from an absent field.

## Transport

stdio only for now. The MCP application is built by a factory in `yandex_core` so the
same tool implementations can later be served over streamable HTTP with OAuth, with the
entry points choosing transport. Nothing in the tool layer references the transport.
