# Epic 2 Context: Mail connector, end to end

<!-- Compiled from planning artifacts, epic 1's retrospective, and measurements taken
     against the live Yandex servers on 2026-09-09 before any story was specified. -->

## Goal

Deliver a working Mail MCP server: list folders, fetch headers over a date range, read
bodies with honest truncation, inspect and download attachments, set flags, draft, reply,
send, move and trash. The OAuth Authorization Code flow is built here, because Mail is the
first service that needs it — Calendar got by on an app password.

Two stories exist for reasons outside Mail. Story 2.5 revisits the shared core now that a
second protocol is in hand: epic 1 built it against CalDAV alone, and IMAP is the first
evidence of what is genuinely common. Story 2.10 is the acceptance check the PRD's own
scenario needs, and it belongs to neither epic on its own.

## Stories

- Story 2.1: Authorise the mailbox and list its folders
- Story 2.2: List message headers over a date range
- Story 2.3: Read a message body with honest truncation
- Story 2.4: Inspect and download attachments
- Story 2.5: Correct the shared core against a second protocol
- Story 2.6: Set message flags
- Story 2.7: Create drafts and replies
- Story 2.8: Send a message
- Story 2.9: Move and trash messages in bulk
- Story 2.10: Answer a real cross-service question

## Measured before specifying — 2026-09-09

Epic 1 established that this platform's documentation is not a substitute for measurement:
five documented behaviours were measured false, three of them unguessable. These were taken
against the live servers before story 2.1 was written. **Anything here marked measured is
evidence; anything marked unverified is not, and must be measured before it is relied on.**

| Measured | Consequence |
|---|---|
| `imap.yandex.ru:993` advertises `IMAP4rev1 CHILDREN UNSELECT LITERAL+ NAMESPACE XLIST UIDPLUS ENABLE ID AUTH=PLAIN AUTH=XOAUTH2 IDLE MOVE` | XOAUTH2 is real, not aspirational. `MOVE` is available for story 2.9; `UIDPLUS` gives new UIDs on append, for story 2.7 |
| That list carries **no `UTF8=ACCEPT`, no `ESEARCH`, no `SORT`, no `THREAD`, no `CONDSTORE`** | folder and mailbox names arrive as modified UTF-7; there is no server-side sort or bounded search. Ordering and bounding are ours, in `tools/` — which is what AD-9 already says |
| `smtp.yandex.ru:465` (implicit TLS) advertises `AUTH LOGIN PLAIN XOAUTH2` | the send path is viable on 465 |
| **`smtp.yandex.ru:587` closes the connection immediately** — no greeting, no STARTTLS | the conventional submission port is a dead end here. Use 465. A reader who "fixes" this to 587 because 587 is standard will produce a connector that cannot send |
| Yandex OAuth supports PKCE, and with `code_verifier` the client secret is not required | the connector is a **public client**: there is no application secret to store, and none should be invented. See "The redirect URI" below |
| ~~The registration form rejects `http://localhost:8765/callback` as a redirect URI~~ **Withdrawn 2026-09-25 -- never measured.** Only API-access applications have a fixed redirect; a web-service application takes a loopback address with a port. Yandex also documents a Device Flow | FR4.1's transient local listener **is** achievable. See the correction below |
| Scopes are `mail:imap_full` (read and delete), `mail:imap_ro` (read), `mail:smtp` (send) | the PRD's scope names are correct |
| `imap_tools` 1.15.0 exposes `MailBox.xoauth2(username, access_token, initial_folder='INBOX')` | a direct fit; no hand-rolled SASL |
| `imap_tools` decodes modified UTF-7 in `folder.list()` via `imap_tools.imap_utf7`. Verified by roundtrip on `Входящие`, `Отправленные`, `Спам`, `Удалённые`, `Черновики`, and a name containing the hierarchy delimiter | folder names need no decoding at our layer. **This corrects a wrong finding taken minutes earlier**: `imap_tools.utils` has `utf7_encode` and no `utf7_decode`, and generalising from that absence produced "the library cannot decode" — which reading `folder.list()` disproved. One module's contents are not the library's |
| `folder.status(folder, options)` returns `Dict[str, int]` — one request per folder | message counts are **N+1**. Story 2.1 bounds this by taking `STATUS` only for the folders a page actually returns |

**Still unverified, and load-bearing.** Cyrillic IMAP `SEARCH` is reported broken by many
users and has never been measured by this project. AD-9 already routes around it — fetch by
date, filter in `tools/` — so nothing depends on it working. Do not add a text-match
parameter to `client/` on the strength of a successful one-off test.

## How Mail signs in -- settled 2026-09-25, after two wrong turns

**Mail uses an app password, like Calendar.** No application is registered. The
operator asked why one was needed, and there was no good answer: the premise that
IMAP "will not take an app password" was never measured, and `imap.yandex.ru`
advertises `AUTH=PLAIN`, which is how an app password signs in.

Measured: the account's Calendar app password is refused by IMAP and SMTP, with
"invalid credentials or IMAP is disabled". Yandex scopes app passwords by type, and
IMAP is a switch in Mail's settings, so Mail needs a password of its own, of type
Mail, with IMAP switched on. `setup mail` says both; the refusal says both.

**Measured 2026-09-25, live:** a Mail-type app password with IMAP switched on signs
in over `LOGIN`; the mailbox lists 26 folders; the delimiter is `|`; Cyrillic names
arrive decoded. Story 2.1 is done on that evidence.

The OAuth work is not wasted: PKCE, the token exchange, refresh and the loopback
listener stay in `yandex_core` for **Disk**, which has no password route.

The two wrong turns, for whoever reads this next: first, a redirect-URI
"measurement" that was an interpretation of an ambiguous reply, which produced a
paste-the-code flow; then OAuth itself, which rested on an unchecked claim about
IMAP. Both were the same defect -- the first plausible explanation, recorded as
fact -- and both were caught by the operator asking "why?".

## The redirect URI -- a correction

This section first said the redirect was not ours to choose, and that the flow
therefore had to be "paste the code Yandex displays". **That was wrong, and it was
recorded as measured when it was not.** It came from one ambiguous reply to a
question about the registration form, interpreted as a refusal.

What Yandex's documentation actually establishes:

- The redirect is fixed at `https://oauth.yandex.ru/verification_code` **only** for
  applications registered for API access.
- An application registered as a **web service** takes a Redirect URI, and a
  loopback address with a port works if it matches exactly.
- A **Device Flow** exists: `POST /device/code` returns a short `user_code` and a
  `verification_url`, and the client polls `/token` with `grant_type=device_code`.

So FR4.1's original design -- open the browser, let the operator sign in however
Yandex offers (password, QR, Yandex ID), receive the code on a transient loopback
listener -- stands. It is also what the operator asked for, in those words, and it
is what is now built: `yandex_core.loopback` binds 127.0.0.1:8765, and the
application is registered as a web service with `http://localhost:8765/callback`.

Two things are still unmeasured and must be measured on the first real sign-in,
not inferred: that Yandex accepts that exact Redirect URI, and whether a refresh
needs the client secret despite PKCE.

**A remote connector** -- where Claude itself says "authorisation required" when
the connector is added -- was discussed on 2026-09-25 and deliberately deferred by
the operator until Mail and Calendar work. It needs a hosted HTTPS server acting as
an OAuth authorisation server in front of Yandex. Calendar would still need its app
password there: CalDAV does not take OAuth tokens. That claim dates from epic 1 and
will be re-verified live once a real token exists.

The error compounded: having concluded the redirect was fixed, the operator was
advised to register an API-access application, which is the one type where it
*is* fixed. The advice manufactured the constraint it was justified by.

## Practices carried from epic 1's retrospective

These are not style preferences. Each names a defect that actually shipped and was caught.

**Measure the live server before writing a spec that writes.** Five for five in epic 1; the
table above is this epic's first instalment.

**Test-first belongs in the spec's acceptance criteria, not in instructions to the
implementer.** The build skill forwards the spec as the single source of truth, so a
requirement stated anywhere else does not arrive. Epic 1 lost three stories to this.

**A fake models the *server's observed behaviour*, never the code under test.**
(Action item ai-1-6.) Two of eight stories in epic 1 shipped a fake that agreed with the
code because it was built from the code: one pre-set a value the code was supposed to
derive, the other normalised both sides of a comparison so a mis-encoded address matched
itself. Both tests passed. Both were worthless. If a fake's behaviour cannot be traced to
something the real server was seen doing, it is a mirror.

**Verify a cross-layer rule against the library's behaviour, not our call site.**
(Action item ai-1-7.) Epic 1 forbade blind retries of a write and enforced it correctly in
its own code — while `caldav` retried a PUT on 429 one layer below. A rule is only held if
the layer that actually issues the request holds it. For this epic that means reading what
`imap_tools` and `smtplib` do on failure before claiming anything about retries or
idempotence.

**Prove a test is load-bearing by breaking the code.** Mutation caught eight tests in epic 1
that passed against broken code, and one more on the work that closed it.

**A settled outcome beats an honest hedge.** Epic 1 ended by replacing "the write may or may
not have happened" with one read that says which. **In this epic that pattern does not
transfer intact**: a sent message cannot be read back the way a calendar object can. Story
2.8 owes an explicit answer for what a lost SMTP answer means, and it is the most dangerous
unknown in the epic — a blind retry sends the message twice, to real people.

## Cross-story dependencies

Story 2.1 builds OAuth and is a hard prerequisite for every other story here, and for all of
epic 3. Read stories (2.2–2.4) precede write stories (2.6–2.9). Story 2.5 needs at least one
read and one write path in place to have evidence to reason from. Story 2.10 needs the whole
epic plus epic 1.
