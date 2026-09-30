---
title: 'Story 2.4 — Inspect and download attachments'
type: 'feature'
created: '2026-09-30'
status: 'done'
review_loop_iteration: 0
baseline_commit: '7724b80'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The first real run in Claude found the meeting's outcome and stopped at
the one document that held the pilot's terms: a 10 MB PDF attached to a letter.

**Approach:** `mail_attachments_list` names what a message carries -- filename, type,
size -- without transferring content. `mail_attachment_download` saves one of them to
a local folder and returns the path and byte count, so a file tool can open it.

## Boundaries & Constraints

**Always:**
- Test-first.
- "Attachment" means what `has_attachments` counts (story 2.2): inline signature images
  are not listed. 165 of 431 named parts in 90 days are images, mostly those.
- Filenames are decoded: 156 of 431 are MIME encoded-words, measured.
- Listing fetches `BODYSTRUCTURE` only. Download fetches exactly one part, PEEKed, from
  an EXAMINEd folder: nothing is marked read.
- The file lands in `directory` (default `~/Downloads/Yandex Mail`) under the attachment's
  own name unless `filename` is given. Only a bare name is accepted: separators, `..`,
  control characters and hidden-file names are refused, and the resolved path must stay
  inside `directory`.
- An existing file is never replaced unless `overwrite` is true.
- Written to a temporary file in the same directory, then renamed: an interrupted
  download never leaves a truncated file under the real name.

**Never:**
- Transferring content to list attachments.
- Writing outside `directory`.

## I/O & Edge-Case Matrix

| Scenario | Expected |
|---|---|
| Message with a PDF and signature logos | One item: the PDF |
| Encoded-word filename | Decoded name |
| Part with no name | Listed as `attachment-<part>` with its type |
| Download | File written, path and exact byte count returned |
| Target exists, no `overwrite` | Refused, file untouched |
| Target exists, `overwrite` | Replaced |
| Name with `../` or `/` | Refused before any request |
| Name from the message carries separators | Reduced to its last component, never followed |
| Unknown part | Not-found naming `mail_attachments_list` |
| Part that is not an attachment (the body) | Refused |
| Unknown UID | Not-found |

</frozen-after-approval>

## Spec Change Log

- **Amendment:** the PRD lists `mail_attachment_download` as *write*. With `overwrite`
  it can replace an existing local file, and MCP defines `destructiveHint` as "may
  perform destructive updates" -- so it is registered destructive. A hint that
  understates is worse than none.

- **Finding (live):** the listed size was 2.6% high on the pilot PDF. The server wraps
  base64 at 76 characters plus CRLF and counts the line breaks, so 78 bytes on the wire
  carry 57 of file, not 4 carry 3. The fake now wraps as the server does; the estimate is
  off by 2 bytes in 320 000 on a real attachment.
- **Finding (mutation):** the "stays inside the directory" check looked redundant with
  plain names and survived. It is not: a symlink named like the file, with `overwrite`,
  would have written wherever it points. Now tested. So is base64 with stray characters,
  which lenient decoding would have "repaired" into a corrupt file.

## Verification

928 unit tests; 11 mutations, all caught after the two gaps above were closed. Live:
the 10.4 MB pilot presentation from the first real run downloads as a 7.57 MB PDF
starting `%PDF-`, its name decoded; a real attachment in the last two weeks downloads
with the unread count unchanged.

## Design Notes

**Measured 2026-09-30, read-only:** 431 named attachments in 90 days; all but two base64;
encoded size p50 35 KB, p90 0.9 MB, max 20.9 MB. The pilot presentation from the first
real run is an `application/pdf` of 10.4 MB, fetched alone in 1.1 s.
