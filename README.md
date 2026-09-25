# Yandex MCP connectors

MCP servers that give Claude access to a Yandex account: Calendar today, Mail and
Disk to follow. Each connector is a local stdio MCP server; the parts that are not
transport-specific live in a shared core, so the same tools can later be served over
HTTP with OAuth without being rewritten.

**Status:** the Calendar connector is complete — seven tools, 12 live tests against a
real account. The Mail connector is under way: it is authorised and lists its folders.
Disk is planned.

---

## What it does

| Tool | Risk | What it answers |
|---|---|---|
| `calendar_list` | read | Which calendars this account has |
| `calendar_events_list` | read | Every occurrence in a date range, recurrence expanded |
| `calendar_event_get` | read | One event in full, series or single occurrence |
| `calendar_freebusy_query` | read | Which spans in a range are busy |
| `calendar_event_create` | write | Adds an event, and reports what the server stored |
| `calendar_event_update` | destructive | Changes an event, conditionally, with an explicit scope |
| `calendar_event_delete` | destructive | Removes an event, with an explicit scope |
| `mail_folders_list` | read | Which folders the mailbox has, and how much is in them |

Two rules shape every one of them:

**Nothing is under-returned in silence.** A result that could not be completed says so
in the result itself. Every list carries `complete` and `next_cursor`; a bounded search
that hits its bound answers "not decided", never "none".

**A change to a recurring event requires an explicit scope.** `occurrence` changes one
instance, `series` changes them all. There is no default, because the two are not
recoverable from each other and guessing is catastrophic in one direction.

## Requirements

- Python 3.13 (pinned in `.python-version`)
- [`uv`](https://docs.astral.sh/uv/)
- A Yandex account
- An **app password** for CalDAV, and a registered **OAuth application** for Mail

Yandex CalDAV rejects OAuth bearer tokens — a token that works for every other Yandex
API is refused by the calendar endpoint. The only credential it accepts is an app
password, and app passwords can only be created by hand at
<https://id.yandex.ru/security/app-passwords>. On a Yandex 360 account an administrator
can disable them entirely; the server then reports organisation policy rather than a
wrong password.

Mail is the opposite: IMAP will not take an app password, so it needs OAuth — and that
needs an application you register once at <https://oauth.yandex.ru>. Register the kind
that is **for API access**: its redirect address is fixed at
`https://oauth.yandex.ru/verification_code`, which is the one this flow uses. Give it the
rights `mail:imap_full` and `mail:smtp`. The connector is a **public client**: it proves itself
with PKCE, so there is no application secret to store or to leak.

For now the application is registered for API access, whose redirect Yandex fixes to a
page that displays the authorization code; you paste it back. That is a choice, not a
platform limit — see the correction in story 2.1's change log.

## Install

```bash
uv sync --no-editable
```

**`--no-editable` is not optional on macOS.** See [Install modes](#install-modes) below.

## Configure

```bash
uv run yandex-mcp setup calendar
```

The command explains how to create the app password, then reads it from a hidden
prompt. It is stored in the system keychain, falling back to a `0600` file under the
config directory. It never appears in this repository, in tool arguments, or in logs.

For Mail, authorise instead of setting up:

```bash
uv run yandex-mcp login mail
```

The first time, it explains how to register the application and asks for its ClientID at
a prompt — then remembers it, so you are asked once. (`--client-id` exists for scripts;
at a prompt there is no placeholder for a shell to misread.)

It then prints a URL naming exactly the rights it asks for, so you can read them before
you grant them. Approve it, and paste back the code Yandex shows (the whole address of that
page works too). The refresh token goes to the keychain; the access token is not stored
at all, and is renewed silently whenever a mail tool runs.

Then check that it actually works — one real call per service:

```bash
uv run yandex-mcp verify
```

Exit code is 0 unless a service actually failed. A service that is unconfigured, or
not yet built, is reported as such and does not fail the command.

### Configuration

Profiles live in `~/.config/yandex-mcp/config.toml` and support both personal Yandex ID
and Yandex 360 domain accounts.

| Variable | Effect |
|---|---|
| `YANDEX_MCP_PROFILE` | Which profile to use; otherwise the file's default |
| `YANDEX_MCP_CONFIG_DIR` | Where the config and fallback secret file live |
| `YANDEX_MCP_CALENDAR_<PROFILE>_PASSWORD` | Overrides the stored calendar app password |
| `YANDEX_MCP_MAIL_<PROFILE>_PASSWORD` | Overrides the stored mail refresh token |

## Wire it into a client

```json
{
  "mcpServers": {
    "yandex-calendar": {
      "command": "/absolute/path/to/.venv/bin/yandex-calendar-mcp"
    },
    "yandex-mail": {
      "command": "/absolute/path/to/.venv/bin/yandex-mail-mcp"
    }
  }
}
```

Add `"env": {"YANDEX_MCP_PROFILE": "work"}` to pin a profile.

## Install modes

Editable installs do not work reliably on macOS here, and the failure is silent enough
to have cost this project a false verification.

Something on macOS re-applies the `UF_HIDDEN` flag to the `.pth` files under `.venv`,
within seconds and repeatedly. Python 3.13's `site.addpackage` explicitly checks that
flag and skips hidden `.pth` files, so the workspace packages never get onto the path
and every console script fails with `ModuleNotFoundError`. No `chflags` remedy holds —
the flag returns between two shell prompts.

The split this project settled on:

| Purpose | Command | Why |
|---|---|---|
| Running the connectors | `uv sync --no-editable` | Removes the `.pth` mechanism entirely; survives a deliberately hidden `.pth` |
| Running the tests | any sync | `pyproject.toml` sets pytest's `pythonpath`, so the suite imports from source regardless |
| Editing the source | re-run `uv sync --no-editable` | Non-editable means source edits do **not** take effect until the next sync |

If a console script reports `ModuleNotFoundError` for a package that is plainly
installed, this is why. Re-run the sync; do not reach for `PYTHONPATH`, which hides the
problem rather than fixing it and produces verification that proves nothing.

## Tests

```bash
env -u PYTHONPATH uv run --no-sync pytest tests/unit -q
```

The `env -u PYTHONPATH` is deliberate. A suite that passes only because a variable
happens to be set in one shell is not evidence.

Live tests run against a real account, create and destroy their own throwaway calendar,
and verify by re-listing:

```bash
env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run --no-sync pytest tests/live -q
```

**Leave several minutes between full live runs.** Yandex's rate limit is per account
and does not reset between runs, so a second run in quick succession fails somewhere —
and never twice in the same place.

## Layout

```
packages/
  yandex-core/          contracts shared by every connector:
                        errors, Page/Chunk results, cursors, risk registry,
                        credentials, OAuth with PKCE, server construction
  yandex-calendar-mcp/  the calendar server
    client/             CalDAV; the only place that touches the network
    tools/              MCP tools; filtering and validation live here
  yandex-mail-mcp/      the mail server, same shape over IMAP
  yandex-mcp-cli/       yandex-mcp setup / login / verify
tests/unit/             no network
tests/live/             a real account, opt-in
```

Dependencies run one way: entrypoint → tools → client. The single async boundary is
`anyio.to_thread.run_sync`, inside `client/` only.

## Design record

Planning and implementation artifacts live under `_bmad-output/`. Two are worth reading
before changing anything:

- `planning-artifacts/architecture/.../ARCHITECTURE-SPINE.md` — the twelve invariants
- `implementation-artifacts/deferred-work.md` — every known limit, with its reasoning

Several documented behaviours of this platform were measured to be false, including that
`If-Match` is ignored on DELETE, that a successful conditional PUT answers 201, and that
`smtp.yandex.ru:587` — the conventional submission port — closes the connection outright
while 465 works. Each is recorded where the code that works around it lives. Measure
before you assume.

## License

MIT — see [LICENSE](LICENSE).
