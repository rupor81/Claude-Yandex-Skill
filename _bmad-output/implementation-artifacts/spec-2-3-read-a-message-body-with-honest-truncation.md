---
title: 'Story 2.3 — Read a message body with honest truncation'
type: 'feature'
created: '2026-09-27'
status: 'done'
review_loop_iteration: 0
baseline_commit: '4c38da6'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-2-2-list-message-headers-over-a-date-range.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Headers say a message exists; they do not say what it says. The
operator's scenario -- the notes from a project meeting, sent by mail -- is in the
body, and 60% of the operator's mail has no plain text at all, only HTML.

**Approach:** `mail_message_get` reads one message's text part -- and only that
part, never its attachments -- renders HTML to readable text when that is all there
is, and returns it as a `Chunk`: bounded by a character limit, with an explicit
marker and a cursor when cut. Quoted history can be stripped on request, and the
answer says when it was.

## Boundaries & Constraints

**Always:**
- **Test-first.** Every matrix row and acceptance criterion begins as a failing test
  named for the harm it prevents.
- Only the text part is fetched, located through `BODYSTRUCTURE` and read with
  `BODY.PEEK[section]`. Measured: a 28 MB message yields 14 KB of text in 43 ms,
  against 2.4 s for the whole message.
- The folder is EXAMINEd and the part PEEKed: reading never marks a message read.
- Part choice: the first non-attachment `text/plain` with content; otherwise the
  first `text/html`, rendered. A zero-size `text/plain` -- seen in real mail, ahead
  of the real one -- is skipped, never returned as an empty message.
- Transfer encoding and charset come from `BODYSTRUCTURE`. utf-8, koi8-r and
  windows-1251 are all in the operator's mailbox, measured.
- A cut result carries `complete: false`, a marker in the text naming the range of
  characters shown and the total, and a cursor that resumes at the next character.
- `strip_quotes` is off by default; when on and something was removed, the answer
  says how much.
- `Chunk` joins `Page` in `yandex_core.results` (AD-4).

**Ask First:**
- Returning HTML itself, rather than its rendering.
- Any attachment content -- that is story 2.4.

**Never:**
- Fetching the whole message to read its text.
- Returning a fragment without the marker, or an empty body as complete when a part
  could not be read or decoded.
- Removing quoted text unless asked.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Short plain message | Under the limit | Whole text, `complete: true`, format `plain` | N/A |
| Long message | Over the limit | Text cut at a word boundary, marker, `complete: false`, cursor | N/A |
| Continue | Cursor | The next segment, starting where the last ended, nothing repeated or skipped | N/A |
| HTML only | No plain part | Readable text: paragraphs, lists, table cells, link targets; format `html`, and a note that it was converted | N/A |
| Empty plain part first | Zero-size `text/plain` ahead of the real one | The real text | Never an empty body |
| Plain part blank, HTML present | Whitespace-only plain | The HTML rendering | N/A |
| koi8-r / windows-1251 / base64 / QP | As measured | Correctly decoded text | N/A |
| Unknown charset | A charset Python lacks | Text decoded as UTF-8 with replacement, and a note | Never an exception |
| No text part at all | Only attachments | Empty text, `complete: true`, a note saying there is no text | Never presented as a blank letter |
| `strip_quotes` | Quoted history present | History removed; note with the count of characters removed | Off by default |
| Unknown UID | Not in the folder | Not-found naming `mail_messages_list` | N/A |
| Cursor after renumbering | UIDVALIDITY changed | Refused | N/A |
| Cursor for another message | Different uid, folder or `strip_quotes` | Refused | N/A |

</frozen-after-approval>

## Code Map

- `packages/yandex-core/src/yandex_core/results.py` -- add `Chunk`
- `packages/yandex-mail-mcp/src/yandex_mail_mcp/client/body.py` -- new: text-part choice from BODYSTRUCTURE, transfer and charset decoding, HTML rendering, quote stripping
- `packages/yandex-mail-mcp/src/yandex_mail_mcp/client/imap_client.py` -- add `read_text`
- `packages/yandex-mail-mcp/src/yandex_mail_mcp/tools/message.py` -- new: `mail_message_get`
- `packages/yandex-core/src/yandex_core/risk.py` -- register read

## Verification (measured)

Against the real mailbox first, read-only: all 254 messages in 30 days yield a text
part; 159 rendered from HTML leak no tags, entities or CSS; on the last 40 the
rendered words agree with the standard library's own body within 5% for every one.
Three messages carry U+FFFD -- and the standard library produces the same two
characters in each: the sender's bytes are invalid, not this decoding.

Unit: 902 pass; 17 mutations, 17 caught on first pass.

Live, 2026-09-27: ten real messages read, plain and HTML, with the unread count
unchanged at 123. A 10 142-character HTML message read in 11 segments of 1 000
reassembles exactly into the same text read whole.

## Design Notes

**Why a character limit of 15 000 by default.** Measured over 120 real messages:
plain text p50 1.9k characters, p90 11k, max 35k; rendered HTML p90 13k. The
default returns nine messages in ten whole; the cursor reaches the rest.

**Why no dependency for HTML.** `html2text` is GPL-3 and this project is MIT; a
converter on the standard library's parser is small and does exactly what is needed:
blocks become line breaks, list items get a dash, table cells a separator, links
keep their target, and scripts, styles and head never leak into the text.
