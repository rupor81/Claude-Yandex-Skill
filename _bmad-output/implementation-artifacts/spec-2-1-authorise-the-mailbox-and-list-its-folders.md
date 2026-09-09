---
title: 'Story 2.1 — Authorise the mailbox and list its folders'
type: 'feature'
created: '2026-09-09'
status: 'ready-for-dev'
review_loop_iteration: 0
baseline_commit: '514b7e058d1edf490ec9e0f42dd0849616c48646'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The mailbox is unreachable. Unlike CalDAV, Yandex IMAP will not take a
password the operator can simply create by hand — it wants an OAuth token, which
expires, and which nothing in this project yet knows how to obtain or renew.

**Approach:** `yandex-mcp login mail` runs the Authorization Code flow with PKCE and
stores the refresh token. Every mail tool obtains a live access token from that refresh
token without prompting. `mail_folders_list` proves the whole chain end to end by
returning the account's real folders — the vertical slice story 1.1 was for Calendar.

## Boundaries & Constraints

**Always:**
- **Test-first.** Every matrix row and acceptance criterion begins as a failing test named
  for the harm it prevents; the report states how each failed before the code existed.
- The connector is a **public client**: PKCE protects the exchange and there is no
  application secret. Measured — Yandex accepts `code_verifier` in place of one. Nothing
  here invents a secret to store.
- `code_verifier` is generated per login, never reused, never logged, and never written
  to disk. `state` is generated per login and the callback's `state` is compared to it.
- The refresh token goes to the system keychain through `yandex_core.credentials`, the
  only module permitted to touch credentials (AD-6). The access token is held in memory
  for the life of a call and is never persisted.
- No secret is a command-line argument. The authorization code is read from a prompt, and
  the prompt does not echo it.
- The `client_id` is not a secret — it travels in the authorization URL by design — and
  lives in the profile's config file, not the keychain.
- Requested scopes are exactly `mail:imap_full` and `mail:smtp`, named in the URL the
  operator is shown, so they can see what they are granting before they grant it.
- Folders are returned as a `Page` with `complete` and `next_cursor` (AD-4). The listing
  is ordered deterministically, because this server offers no `SORT` — measured.
- `client/` imports no `mcp`, `tools/` imports no `imap_tools` (AD-1). The IMAP library is
  blocking and is wrapped exactly once, in `client/`, with `anyio.to_thread.run_sync`
  (AD-3).
- An organisation that has disabled external clients is reported as organisation policy,
  never as a bad token (FR4.5).

**Ask First:**
- Any scope beyond `mail:imap_full` and `mail:smtp`. Each one is something the operator
  is handing over, and a connector that asks for more than it uses is not trustworthy.
- Storing the access token, rather than only the refresh token.

**Never:**
- Printing, logging, or including any token, code, or verifier in an error message.
- Accepting an authorization response whose `state` does not match the one issued.
- Reporting an empty folder list for an authentication failure. An unreachable mailbox is
  an error; zero folders is not an answer this server may invent.
- Fetching `STATUS` for folders the caller did not ask for. Counts cost one request each —
  measured — so the page's own size is the bound.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| First login | No stored credentials, a `client_id` in config | Prints the authorization URL naming both scopes; reads a pasted code; exchanges it with `code_verifier`; stores the refresh token | N/A |
| Login without a `client_id` | Nothing in config | Refused before any network call, naming the registration page and the two scopes to request | Validation error |
| Code pasted with surrounding whitespace or a full URL | Operator pastes what the browser gave them | Accepted; the code is extracted | N/A |
| Wrong or expired code | Yandex answers `invalid_grant` | Reported as a code that is wrong or already used, with what to do; nothing stored | Never stored |
| `state` mismatch | Response carries a different `state` | Refused, nothing exchanged | Never exchanged |
| Expired access token | Any mail tool called | Refreshed once from the refresh token and the call proceeds, silently | N/A |
| Revoked refresh token | Refresh answers an error | Reported as needing `yandex-mcp login mail` again, naming the profile | Never a bare protocol error |
| No credentials at all | Any mail tool called | `NotConfigured`, naming the command that fixes it | Never an empty page |
| Folders listed | A valid token | A `Page` of folders with names, hierarchy delimiter, flags and counts | N/A |
| Cyrillic folder names | A real Russian mailbox | Names are readable text, not modified UTF-7 | N/A |
| More folders than `limit` | A large mailbox | `complete: false` and a cursor; only the returned folders cost a `STATUS` | N/A |
| A folder that cannot be selected | `\Noselect` in its flags | Listed, with counts absent and said to be absent | Never reported as zero |
| `STATUS` fails for one folder | One folder errors, others do not | That folder is listed with counts absent and the reason stated; the page is still returned | Never drops the folder |
| Organisation disabled external clients | Auth fails with that cause | Reported as organisation policy | Not "bad password" |
| Network unreachable | IMAP host down | Transport error naming the host | Never an empty page |
| `verify` run with mail configured | `yandex-mcp verify` | Reports mail reachable or names the cause, independently of calendar | Never fails the command for an unconfigured service |

</frozen-after-approval>

## Code Map

- `packages/yandex-core/src/yandex_core/oauth.py` -- new: the Authorization Code flow with
  PKCE, and refresh. Shared, because epic 3 needs the same flow for Disk (AD-6 keeps
  credential access here)
- `packages/yandex-core/src/yandex_core/config.py` -- extend: `client_id` and the mail
  hosts on `Profile`
- `packages/yandex-core/src/yandex_core/risk.py` -- register `mail_folders_list` as read
- `packages/yandex-mail-mcp/` -- new package, mirroring `yandex-calendar-mcp`:
  `client/imap_client.py`, `tools/folders.py`, `server.py`
- `packages/yandex-mcp-cli/src/yandex_mcp_cli/main.py` -- add `login mail`
- `packages/yandex-mcp-cli/src/yandex_mcp_cli/verify.py` -- make `check_mail` real
- `tests/unit/test_oauth.py`, `tests/unit/test_mail_folders_list.py` -- new;
  `tests/live/test_mail_live.py` -- new, opt-in like the calendar one

## Tasks & Acceptance

**Execution:**
- [ ] Failing tests for every matrix row, before any implementation
- [ ] `core/oauth.py` -- PKCE authorization URL, exchange, refresh, with `state` checked
- [ ] `core/config.py` -- `client_id` and mail hosts on the profile
- [ ] `yandex-mail-mcp` package skeleton, wired like the calendar server
- [ ] `client/imap_client.py` -- XOAUTH2 connect, folder list, bounded `STATUS`
- [ ] `tools/folders.py` -- `mail_folders_list` returning a `Page`
- [ ] `cli` -- `login mail`, and `verify` covering mail
- [ ] `tests/live` -- authorise a real mailbox and list its real folders

**Acceptance Criteria:**
- Given no stored Mail credentials, when `yandex-mcp login mail` runs, then it prints an
  authorization URL naming `mail:imap_full` and `mail:smtp`, exchanges the pasted code
  using `code_verifier`, and stores the refresh token in the keychain — with no secret in
  any command-line argument.
- Given a response whose `state` differs from the one issued, when login runs, then the
  code is not exchanged and nothing is stored.
- Given an expired access token, when any mail tool is called, then the token is refreshed
  and the call proceeds without prompting.
- Given a revoked refresh token, when a mail tool is called, then the error names the
  profile and the command that repairs it, and is not a bare protocol error.
- Given a valid token, when `mail_folders_list` is called, then it returns folders with
  message counts as a `Page`, with Cyrillic names as readable text.
- Given a mailbox with more folders than `limit`, when the tool is called, then
  `complete` is false, a cursor is returned, and no folder outside the page was asked for
  its counts.
- Given a `\Noselect` folder, when the page includes it, then it is listed with counts
  absent and said to be absent — never zero.
- Given an organisation that has disabled external clients, when authentication fails,
  then the cause is reported as organisation policy.
- Given `client/imap_client.py`, when it is imported from a plain script, then no `mcp`
  import is present anywhere in its import graph.
- Given the tool's annotations, when they are read, then `mail_folders_list` declares
  itself read-only.

## Spec Change Log

- **Finding (measured, 2026-09-09):** FR4.1 and this story's original acceptance criteria
  specify a transient local listener. The Yandex OAuth registration form **refuses**
  `http://localhost:8765/callback` as a redirect URI, and the documentation for
  API-access applications states the redirect is fixed at
  `https://oauth.yandex.ru/verification_code` and not editable.
  **Amendment:** the operator pastes the code Yandex displays, into a non-echoing prompt.
  PKCE, the keychain, automatic renewal and the no-secret-in-arguments rule are unchanged.
  **Avoids:** building a listener against a redirect this platform will not deliver to,
  and discovering it only when a real operator tries to log in. As a side effect the flow
  now works on a machine with no browser, which the listener never would have.

## Design Notes

**Why the OAuth flow lives in `yandex_core` and not in the mail package.** Disk needs the
identical flow in epic 3, differing only in scopes. Epic 1's rule against speculative
generalisation applies to shapes nobody has seen twice; this one is named in the plan for
two consumers, and putting it in the mail package would mean epic 3 either imports across
servers — which AD-2 forbids — or copies it.

**Why counts are bounded by the page.** `folder.status()` is one request per folder,
measured. A mailbox with fifty folders would otherwise pay fifty requests to answer one
call, and epic 1 established that this account's request budget is scarce and shared.
Taking `STATUS` only for the folders a page returns makes the cost proportional to what
was asked for, and `Page` already carries the honesty about what was left out.

**Why a `\Noselect` folder is listed with counts absent rather than zero.** It is a
container in the hierarchy, not a mailbox; asking it for a count is meaningless, and
answering zero would be this server inventing a fact. NFR3 in its usual form.

**What is deliberately not here.** No message access of any kind — that is story 2.2
onward. No SMTP connection: the send scope is requested at login because re-authorising
later would mean a second trip through the browser for the operator, but nothing in this
story uses it.

## Verification

**Commands:**
- `env -u PYTHONPATH uv run --no-sync pytest tests/unit -q` -- expected: all pass, no network
- `env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run --no-sync pytest tests/live -q` -- expected: lists the real mailbox's real folders, reads nothing else
- `uv run --no-sync python scripts/check_bookkeeping.py` -- expected: records agree
- a stdio `tools/list` against the mail server -- expected: one tool, read-only

## Suggested Review Order

To be filled by the implementation report.
