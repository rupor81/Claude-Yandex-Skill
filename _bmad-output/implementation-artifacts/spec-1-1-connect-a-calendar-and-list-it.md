---
title: 'Story 1.1 — Connect a calendar and list it'
type: 'feature'
created: '2026-09-04'
status: 'done'
review_loop_iteration: 0
baseline_commit: '91432f3b2daa54fd1317f40b6a50276ffa2fbd43'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The repository holds only planning documents. Nothing connects Claude to a
Yandex calendar, and none of the shared machinery later stories depend on exists.

**Approach:** One vertical slice, not a scaffolding pass — a `uv` workspace on a pinned
Python, the smallest useful `yandex_core`, a CLI that walks the operator through creating
the app password Yandex requires, a blocking CalDAV client, and a single `calendar_list`
tool over stdio. One tool exercises every layer, so nothing speculative gets built.

## Boundaries & Constraints

**Always:**
- Layers: `server.py` (transport only) → `tools/` (contracts, annotations, pagination) →
  `client/` (CalDAV only). `tools/` imports no protocol library; `client/` imports no
  `mcp` and runs from a plain script. `yandex_core` imports no server package.
- Tools are `async def`; the blocking CalDAV call is wrapped once, in `client/`, via
  `anyio.to_thread.run_sync`.
- Listings return `Page` with required `complete` and `next_cursor` — never a bare list.
- Only `yandex_core.credentials` touches the keychain, fallback file, or environment.
  Secrets never enter arguments, logs, or errors.
- Annotations come from the risk registry; a tool absent from it prevents server start.
- Logging to stderr only — stdout carries the protocol.
- `MCPServer` constructed with keyword arguments; `DAVClient` constructed directly.

**Ask First:**
- Any dependency beyond `mcp`, `caldav`, `keyring`, `pydantic`, `pytest`.
- Any credential path other than keychain with a `0600` fallback.
- Anything needing network access inside `tests/unit`.

**Never:**
- `Chunk`, OAuth, or any calendar tool besides `calendar_list` — later stories own these.
- Generalising the core for protocols not yet present.
- Turning a failure into an empty successful result.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Happy path | Valid profile and app password | `Page` of calendars with name and URL, `complete: true`, `next_cursor: null` | N/A |
| Truncation | More calendars than `limit` | At most `limit` items, `complete: false`, opaque cursor set | N/A |
| Bad password | Revoked or wrong app password | `AuthError` naming the credential | Map CalDAV `AuthorizationError` (401/403) |
| Org policy | Yandex 360, app passwords disabled | `PermissionError` naming organisation policy | Distinguished from `AuthError` by response |
| Network down | Host unreachable | `TransportError` naming the network | Catch the HTTP library's exceptions; caldav does not wrap them |
| No credential | Nothing stored for the profile | Actionable error pointing at `yandex-mcp setup calendar` | Raised before any request |
| Unregistered tool | Tool missing from the risk registry | Startup fails, naming the tool | Fail at registration time |

</frozen-after-approval>

## Code Map

Greenfield — every path is created by this story.

- `pyproject.toml` -- uv workspace root, members under `packages/`
- `.python-version` -- pins 3.13; system `python3` is 3.9.6 and stays untouched
- `packages/yandex-core/src/yandex_core/config.py` -- profile model, TOML from `~/.config/yandex-mcp/config.toml`, selected by `YANDEX_MCP_PROFILE`
- `.../yandex_core/credentials.py` -- sole secret reader: `keyring`, `0600` fallback, redaction helper
- `.../yandex_core/errors.py` -- `YandexError` base; `AuthError`, `PermissionError`, `NotFound`, `Conflict`, `RateLimited`, `TransportError`, `ProtocolError`
- `.../yandex_core/results.py` -- `Page[T]`, a Pydantic generic; `complete` and `next_cursor` required
- `.../yandex_core/paging.py` -- opaque base64 cursor encode/decode
- `.../yandex_core/risk.py` -- risk classes, registry, `annotations_for(name)` returning `ToolAnnotations`, registration guard
- `.../yandex_core/app.py` -- `build_server(...)` factory; the caller picks the transport
- `packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py` -- `DAVClient`, principal, calendar enumeration; translates `caldav.lib.error.*` and HTTP-library exceptions into `yandex_core.errors`
- `.../yandex_calendar_mcp/tools/calendars.py` -- `calendar_list`, async, returns `Page`
- `.../yandex_calendar_mcp/server.py` -- builds the app, registers tools, runs stdio
- `packages/yandex-mcp-cli/src/yandex_mcp_cli/main.py` -- `yandex-mcp setup calendar`
- `tests/unit/` -- fakes, no network; `tests/live/` -- skipped unless `YANDEX_MCP_LIVE_TESTS=1`

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml`, `.python-version` -- create the workspace, pin Python 3.13 -- everything else needs a resolvable environment
- [x] `yandex_core/errors.py`, `results.py`, `paging.py` -- error taxonomy, `Page`, opaque cursors -- the contracts every tool returns through
- [x] `yandex_core/config.py`, `credentials.py` -- profiles and the single secret reader
- [x] `yandex_core/risk.py`, `app.py` -- risk registry with annotation derivation and startup guard; transport-free server factory
- [x] `yandex_calendar_mcp/client/caldav_client.py` -- blocking CalDAV access, exceptions translated at this boundary
- [x] `yandex_calendar_mcp/tools/calendars.py`, `server.py` -- `calendar_list` and the stdio entry point
- [x] `yandex_mcp_cli/main.py` -- `setup calendar`, explaining why an app password is required
- [x] `tests/unit/` -- cover every I/O matrix row against a fake CalDAV client
- [x] `tests/live/test_calendar_live.py` -- one real `calendar_list` call, skipped without the env flag

**Acceptance Criteria:**
- Given a clean checkout, when setup runs, then `uv` provisions Python 3.13 and `uv run pytest tests/unit` passes with no network.
- Given no stored credential, when `yandex-mcp setup calendar` runs, then it states that Yandex CalDAV rejects OAuth and an app password must be created in Yandex ID, and stores the entered value in the keychain.
- Given a configured profile, when the server runs over stdio and `calendar_list` is called, then it returns a `Page` of calendars and the tool reports `read_only_hint: true`.
- Given a tool with no risk-registry entry, when the server starts, then startup fails naming that tool.
- Given any failure, when it surfaces, then no secret appears in the message and the result is never an empty success.
- Given static inspection, then `tools/` imports no protocol library, `client/` imports no `mcp`, and `yandex_core` imports no server package.

## Spec Change Log

- **Finding (implementation):** MCP v2 treats an unrecognised exception as a server-side
  crash and withholds its text from the model, so every actionable `YandexError` would
  have reached the caller as bare "Error executing tool calendar_list" — silently
  defeating the honest-errors requirement.
  **Amendment:** `register_tool` in `yandex_core.app` re-raises a `YandexError` as the
  SDK's `ToolError` with its message intact. Placed in the core so `tools/` still imports
  no protocol library.
  **Avoids:** a connector that reports every failure as an opaque crash while its tests
  pass.
  **KEEP:** the wrap stays in the core, applied once at registration — never repeated per
  tool, and never moved into `tools/`.

## Design Notes

Three facts verified against tagged sources, each of which silently breaks code copied
from older examples:

- `MCPServer`'s positional order is `name, title, description, instructions, ...`. A
  second positional argument lands in `title`. Pass keywords.
- `ToolAnnotations` fields are snake_case in Python (`read_only_hint`) and serialise to
  camelCase; attribute access with the camelCase spelling fails.
- `caldav` does not wrap transport failures. `AuthorizationError` and `NotFoundError` come
  from `caldav.lib.error`, but connection and timeout errors arrive from the underlying
  HTTP library and must be caught explicitly or they escape `client/`.

Use `DAVClient` directly, not `get_davclient()`: the factory reads `CALDAV_*` environment
variables and a config file and can return `None` — non-deterministic, and it would read
credentials outside `yandex_core.credentials`.

A returned Pydantic model becomes the tool's output schema directly, so `Page` needs no adapter.

## Verification

**Commands:**
- `uv sync` -- expected: resolves on Python 3.13 without the system interpreter
- `uv run pytest tests/unit -q` -- expected: all pass, no network
- `uv run python -c "import yandex_calendar_mcp.client.caldav_client"` -- expected: imports with `mcp` absent
- `uv run yandex-mcp setup calendar --help` -- expected: explains the app-password requirement
- `YANDEX_MCP_LIVE_TESTS=1 uv run pytest tests/live -q` -- expected: lists real calendars once a password is stored

## Suggested Review Order

**Where the invariants are enforced**

- Registration is the choke point: annotations, async check, and the error wrap all land here.
  [`app.py:117`](../../../packages/yandex-core/src/yandex_core/app.py#L117)

- One table decides every tool's declared risk; an unlisted tool cannot start.
  [`risk.py:75`](../../../packages/yandex-core/src/yandex_core/risk.py#L75)

- Completeness is a required field, so no listing can omit the question.
  [`results.py:18`](../../../packages/yandex-core/src/yandex_core/results.py#L18)

- Cursors carry their issuing tool, so one tool cannot honour another's cursor.
  [`paging.py:29`](../../../packages/yandex-core/src/yandex_core/paging.py#L29)

**The protocol boundary**

- Every CalDAV and HTTP exception is translated here; none escapes untyped.
  [`caldav_client.py:102`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L102)

- Org policy versus wrong password; says so plainly when it cannot tell.
  [`caldav_client.py:199`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py#L199)

- The only tool: validation, paging, and completeness live above the protocol.
  [`calendars.py:52`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/tools/calendars.py#L52)

**Credentials and configuration**

- Sole secret reader: environment, then keychain, then an owner-only file.
  [`credentials.py:65`](../../../packages/yandex-core/src/yandex_core/credentials.py#L65)

- Read-modify-write with escaping, so setup cannot corrupt an existing config.
  [`config.py:112`](../../../packages/yandex-core/src/yandex_core/config.py#L112)

**Entry points**

- Startup failures print an actionable line instead of a traceback.
  [`server.py:59`](../../../packages/yandex-calendar-mcp/src/yandex_calendar_mcp/server.py#L59)

- Explains why an app password is unavoidable, then stores it without echoing.
  [`main.py:92`](../../../packages/yandex-mcp-cli/src/yandex_mcp_cli/main.py#L92)
