# Yandex MCP connectors

MCP servers that give Claude access to a Yandex account: Calendar today, Mail and
Disk to follow. Each connector is a local stdio MCP server; the parts that are not
transport-specific live in a shared core, so the same tools can later be served over
HTTP with OAuth without being rewritten.

**Status:** the Calendar connector is complete — seven tools, 12 live tests against a
real account. The Mail connector reads: folders, message headers, and message text.
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
| `mail_messages_list` | read | Message headers in a date range, newest first, filterable by sender and subject |
| `mail_message_get` | read | One message's text, HTML rendered, cut into segments when long |

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
- An **app password** for Calendar, and another for Mail — Yandex scopes them by type

Yandex CalDAV rejects OAuth bearer tokens — a token that works for every other Yandex
API is refused by the calendar endpoint. The only credential it accepts is an app
password, and app passwords can only be created by hand at
<https://id.yandex.ru/security/app-passwords>. On a Yandex 360 account an administrator
can disable them entirely; the server then reports organisation policy rather than a
wrong password.

Mail connects the same way, the way mail programs connect to Yandex: with an app password.
No application needs to be registered. Two things to do in Yandex, because Yandex answers
both mistakes with one error:

- create the app password with the type **Mail** (Почта) — one made for Calendar is refused
  by IMAP (measured);
- switch IMAP access on in Yandex Mail: *Settings → Mail programs*.

## Install

```bash
uv sync
```

If this folder lives in `~/Documents` or on the Desktop with iCloud sync on, do this once
first — see [The environment](#the-environment) for why:

```bash
mkdir .venv.nosync && ln -s .venv.nosync .venv
```

## Configure

```bash
uv run yandex-mcp setup calendar
```

The command explains how to create the app password, then reads it from a hidden
prompt. It is stored in the system keychain, falling back to a `0600` file under the
config directory. It never appears in this repository, in tool arguments, or in logs.

And the same for Mail:

```bash
uv run yandex-mcp setup mail
```

It reuses the profile's login and stores the mail password in its own keychain slot, so a
working calendar is left exactly as it was.

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
| `YANDEX_MCP_MAIL_<PROFILE>_PASSWORD` | Overrides the stored mail app password |

## Install as Claude extensions

The simplest way. Build the two packages and double-click them:

```bash
npx -y @anthropic-ai/mcpb pack extensions/yandex-calendar dist/yandex-calendar.mcpb
npx -y @anthropic-ai/mcpb pack extensions/yandex-mail dist/yandex-mail.mcpb
open dist/yandex-calendar.mcpb dist/yandex-mail.mcpb
```

The install dialog asks for the Yandex login and the app password, and Claude keeps
them. Both fields may be left blank on a machine where `yandex-mcp setup` has already
stored them. The extensions run the servers from this project's `.venv`, so a code
change reaches them on the next restart of the extension, with no reinstall. The icons
are drawn by `extensions/make_icons.py` -- our own mark, not Yandex's logo.

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

## The environment

**iCloud hides files inside dot-folders in `~/Documents`,** and Python 3.13 skips hidden
`.pth` files — so an ordinary `uv sync` produced an environment whose console scripts failed
with `ModuleNotFoundError` for packages that were plainly installed.

Measured, not guessed: a file created in `~/Documents/<project>/.anything/` is flagged hidden
within seconds; the same file in a plain folder, in a dot-folder outside `~/Documents`, or in
a folder ending in `.nosync` is not. `.nosync` is iCloud's documented opt-out.

So the environment lives in `.venv.nosync`, and `.venv` is a symlink to it. Everything that
expects `.venv` — uv, your MCP client config — keeps working, editable installs work again,
and iCloud stops uploading a few hundred megabytes of dependencies as a side effect.

For most of epic 1 this was diagnosed only as "something re-applies the hidden flag", and
worked around with `uv sync --no-editable`. The workaround held until someone typed a plain
`uv run`, which silently rebuilt the environment editable and broke every connector at once.
The cause, once found, needed no workaround.

## Tests

```bash
env -u PYTHONPATH uv run pytest tests/unit -q
```

The `env -u PYTHONPATH` is deliberate. A suite that passes only because a variable
happens to be set in one shell is not evidence.

Live tests run against a real account, create and destroy their own throwaway calendar,
and verify by re-listing:

```bash
env -u PYTHONPATH YANDEX_MCP_LIVE_TESTS=1 uv run pytest tests/live -q
```

**Leave several minutes between full live runs.** Yandex's rate limit is per account
and does not reset between runs, so a second run in quick succession fails somewhere —
and never twice in the same place.

## Layout

```
packages/
  yandex-core/          contracts shared by every connector:
                        errors, Page/Chunk results, cursors, risk registry,
                        credentials, OAuth with PKCE (for Disk), server construction
  yandex-calendar-mcp/  the calendar server
    client/             CalDAV; the only place that touches the network
    tools/              MCP tools; filtering and validation live here
  yandex-mail-mcp/      the mail server, same shape over IMAP
  yandex-mcp-cli/       yandex-mcp setup / verify
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
