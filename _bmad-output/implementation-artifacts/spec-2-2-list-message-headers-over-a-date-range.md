---
title: 'Story 2.2 — List message headers over a date range'
type: 'feature'
created: '2026-09-25'
status: 'done'
review_loop_iteration: 0
baseline_commit: 'eb7b9a5'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-2-1-authorise-the-mailbox-and-list-its-folders.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The mailbox can be reached but nothing in it can be read. To find the
correspondence behind a meeting, Claude needs the headers of messages in a period --
who, when, what about -- without pulling a mailbox of thousands into its context.

**Approach:** `mail_messages_list` takes a required `start` and `end`, finds the
messages in that period with IMAP `SINCE`/`BEFORE`, and returns their headers newest
first. Optional sender and subject filters are applied here, over decoded headers.
Because reading headers costs ~40 ms per message cold -- measured -- one call reads a
bounded portion of the range, and says so: `complete: false` and a cursor until the
whole range has been read.

## Boundaries & Constraints

**Always:**
- **Test-first.** Every matrix row and acceptance criterion begins as a failing test
  named for the harm it prevents; the report states how each failed before the code
  existed.
- `start` and `end` are required, timezone-aware ISO 8601, `start < end` (AD-7). There
  is no all-history default (FR2.2).
- The server is asked for dates only: `UID SEARCH SINCE d1 BEFORE d2`, day-granular,
  widened to whole days that cover the range. Exact instants are filtered here against
  `INTERNALDATE` -- the date the server searched by, and the one reported -- so a
  result never appears to fall outside its own range.
- The IMAP client accepts no text-match parameter (AD-12). Sender and subject filters
  are case-insensitive substring matches over *decoded* values, in `tools/`.
- One call reads headers for at most `SCAN_BUDGET` messages. The page is `complete`
  only when the whole range has been read and nothing was held back by `limit`.
- The cursor names the last UID read and the folder's `UIDVALIDITY`. A folder whose
  `UIDVALIDITY` changed is refused -- its UIDs no longer mean what the cursor meant.
- The folder is opened read-only and headers are read with `BODY.PEEK`: listing must
  never mark a message read.
- Attachment presence comes from `BODYSTRUCTURE`, fetched only for the messages
  returned -- it is the most expensive item per message, measured.
- Each item carries: UID, date, from, to, subject, flags, size, attachment presence.

**Ask First:**
- Using server-side `SEARCH SUBJECT`/`FROM` as a prefilter. Measured to over-return
  (morphology) and not to under-return on five words; not yet enough evidence to rely
  on for NFR3.
- Any field beyond the list above, especially anything from the body.

**Never:**
- Returning a filtered page as `complete` when part of the range was not read.
- Reporting attachment presence as `false` when it could not be determined.
- Printing, logging, or returning a message body.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| No range | `start` or `end` missing | Refused before any request | Validation error |
| Naive or inverted range | No timezone, or `end <= start` | Refused before any request | Validation error |
| Small range | Fewer messages than `limit` and budget | All of them, newest first, `complete: true` | N/A |
| More than `limit` | Unfiltered | `limit` newest, `complete: false`, cursor | N/A |
| Range larger than the scan budget, filtered | Filter matches few | Matches from the portion read; `complete: false` and a cursor even if no match was found | Never `complete` early |
| Continue with cursor | Cursor from the previous page | Next portion, older than the last UID read; no message twice | N/A |
| Cursor after the folder was renumbered | `UIDVALIDITY` changed | Refused; start again | Never a silently wrong page |
| Cursor from another tool or another query | Different tool or range | Refused | Validation error |
| Encoded-word subject, Cyrillic | `=?utf-8?B?...?=` | Decoded text | N/A |
| Undecodable header | Broken charset | Best-effort text with replacement characters, never an exception for the whole page | N/A |
| Instant at the range edge | `INTERNALDATE == end` | Excluded (`end` is exclusive) | N/A |
| Attachment presence unknown | `BODYSTRUCTURE` fails or cannot be parsed | `null`, with a reason | Never `false` |
| Unknown folder | Name not in the mailbox | Not-found naming `mail_folders_list` | Never an empty page |
| Empty range | No messages | Empty page, `complete: true` | N/A |

</frozen-after-approval>

## Code Map

- `packages/yandex-mail-mcp/src/yandex_mail_mcp/client/imap_client.py` -- add `list_messages`: SEARCH, bounded header FETCH, BODYSTRUCTURE for the returned messages, one connection
- `packages/yandex-mail-mcp/src/yandex_mail_mcp/client/headers.py` -- new: parse FETCH responses, decode encoded-words, parse BODYSTRUCTURE for attachment presence
- `packages/yandex-mail-mcp/src/yandex_mail_mcp/tools/messages.py` -- new: `mail_messages_list`, validation, filtering, window, cursor
- `packages/yandex-core/src/yandex_core/risk.py` -- register as read
- `tests/unit/test_mail_headers.py`, `tests/unit/test_mail_messages_list.py`; `tests/live/test_mail_live.py` -- extend

## Tasks & Acceptance

**Execution:**
- [x] Failing tests for every matrix row, before any implementation
- [x] `client/headers.py` -- FETCH parsing, header decoding, BODYSTRUCTURE attachment detection
- [x] `client/imap_client.py` -- `list_messages` on one read-only connection
- [x] `tools/messages.py` -- the tool, its filters, window and cursor
- [x] `core/risk.py` -- register read
- [x] `tests/live` -- a real range, a real filter, paging to the end

**Acceptance Criteria:**
- Given no `start` or `end`, when the tool is called, then it is refused before any request.
- Given a range, when the tool runs, then the server is sent `SINCE`/`BEFORE` and nothing text-shaped.
- Given Cyrillic encoded-word subjects, when results are returned, then they are decoded and filters match the decoded text.
- Given a filter over a range larger than the scan budget, when the tool returns, then `complete` is false and a cursor continues exactly where reading stopped.
- Given a cursor, when the folder's `UIDVALIDITY` has changed, then the call is refused.
- Given a listing, when it runs, then no message is marked read.
- Given a message whose structure cannot be read, then its attachment presence is `null` with a reason.

## Spec Change Log

- **Finding (implementation):** a subject written as raw UTF-8 bytes -- no
  encoded-words -- came back as a row of `�`. compat32's `items()` sanitises 8-bit
  header bytes into replacement characters before any decoding runs. Fixed by
  reading `raw_items()`. The real 30-day sample held no such header, so the check
  against the operator's mailbox passed; a fake built on how older mailers write
  headers found it.
  **Avoids:** a subject silently destroyed, which a caller filtering on it would
  never find and never know it had missed.
- **Finding (mutation):** the day-window widening survived a mutation, because the
  midnight test used a zone that errs in the safe direction. The dangerous
  direction -- a message stored in a zone *behind* the query's -- now has its own
  test.
- **Also:** `checked_instant` moved to `yandex_core.instants`, the first piece of
  story 2.5; Calendar imports it from there.

## Verification (measured)

Unit: 856 pass. 16 mutations, 16 caught. Against the real mailbox beforehand,
read-only: 275/275 real BODYSTRUCTUREs parse, and attachment presence agrees with an
independent full-MIME classification on 80/80 messages; 275/275 headers decode with
no replacement characters.

Live, 2026-09-25: a real week returns 50 messages with 41 remaining, every date
inside its range, every structure readable, and **the number of unread messages
unchanged before and after**. A month filtered on sender pages to its end in three
calls, 265 messages, none twice.

## Design Notes

**Why newest first.** The question this tool serves is "what was said about this
meeting", which is almost always recent. UIDs ascend with arrival, measured, so newest
first is descending UID, and the cursor is "continue below this UID".

**Why the date is `INTERNALDATE`.** `SEARCH SINCE/BEFORE` compares `INTERNALDATE`. A
tool that searched by one date and reported another could return a message dated
outside the range the caller asked for. The two differ by at most 4 h here, measured,
but the rule is structural, not statistical.

**Why a scan budget rather than a range limit.** The cost is per message read, not per
day: a quiet year is cheaper than a busy week. Bounding what is read keeps every call
within an MCP client's patience, and the cursor makes the rest reachable.
