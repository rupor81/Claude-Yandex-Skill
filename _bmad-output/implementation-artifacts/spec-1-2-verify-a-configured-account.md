---
title: 'Story 1.2 — Verify a configured account'
type: 'feature'
created: '2026-09-05'
status: 'done'
review_loop_iteration: 0
baseline_commit: '6d756f5dfe3b63a5d446cee77e282b932fe97d90'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-1-1-connect-a-calendar-and-list-it.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Setup stores a credential without ever using it, so a wrong app password, a
disabled organisation policy, or an unreachable host only surfaces later, from inside an
MCP client, as a failing tool call. The operator has no way to ask "is this working?"

**Approach:** A `yandex-mcp verify` command that attempts a real, minimal call per
configured service and reports one line each: reachable, unconfigured, or failed with the
cause named. It never stops at the first failure — every service is reported.

## Boundaries & Constraints

**Always:**
- Verification makes a real network call per service; a configuration-only check would
  pass for exactly the credentials that fail in use.
- Every service is attempted and reported, even after another has failed.
- Failures are named by cause using the existing error taxonomy — a wrong credential, an
  organisation policy, an unreachable host, and a rate limit are distinguishable.
- A service that is absent, uninstalled, or unconfigured is reported as such, and is not
  a failure.
- No secret appears in output, in any state.
- Connector clients are imported lazily inside the check, so a broken or missing
  connector cannot prevent the others from being reported.

**Ask First:**
- Adding a dependency, or any entry-point or plugin registration mechanism.
- Any output format other than human-readable lines (JSON, machine-parseable).

**Never:**
- Mail or Disk checks beyond reporting them as not yet available — later epics own them.
- Writing, repairing, or migrating configuration; verify is read-only.
- Prompting for anything. The command is non-interactive and safe to script.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Reachable | Valid profile and app password | Calendar reported reachable, with how many calendars answered | Exit 0 |
| No profile | No config file at all | Every service reported unconfigured, pointing at `setup` | Exit 0 — not a failure |
| No credential | Profile exists, nothing stored | Calendar unconfigured, naming the setup command | Exit 0 |
| Wrong password | Stored password rejected | Calendar failed: credential wrong or revoked | Exit non-zero |
| Org policy | Yandex 360 forbids app passwords | Calendar failed: organisation policy named, not a bad password | Exit non-zero |
| Cause undecidable | Rejected without a usable status or phrase | Failure states plainly that the cause could not be distinguished | Exit non-zero |
| Network down | Host unreachable | Calendar failed: transport, and the command still completes | Exit non-zero |
| Not yet built | Mail and Disk | Reported as not yet available, distinct from unconfigured | Does not affect exit code |
| Connector missing | Calendar package absent or failing to import | Reported as unavailable with the reason; other services still reported | Exit non-zero |

</frozen-after-approval>

## Code Map

- `packages/yandex-mcp-cli/src/yandex_mcp_cli/verify.py` -- new: one check per service, each returning a result record rather than raising; lazy guarded import of the calendar client
- `packages/yandex-mcp-cli/src/yandex_mcp_cli/main.py:52` -- `build_parser`: add the `verify` subcommand with an optional `--profile`
- `packages/yandex-mcp-cli/src/yandex_mcp_cli/main.py:130` -- `main`: dispatch to verify; it already maps `YandexError` to exit 1, and verify must not rely on that — it catches its own
- `packages/yandex-calendar-mcp/src/yandex_calendar_mcp/client/caldav_client.py:102` -- reuse: `list_calendars` is the minimal real call, and its translation boundary already produces the distinctions verify reports
- `packages/yandex-core/src/yandex_core/errors.py` -- reuse: `AuthError`, `PolicyError`, `TransportError`, `RateLimited`, `ProtocolError` are the causes to name
- `packages/yandex-core/src/yandex_core/config.py` -- reuse: `load_profile` raises `ProtocolError` when unconfigured; verify translates that into the unconfigured state rather than a failure
- `tests/unit/test_cli_verify.py` -- new: every matrix row, against fakes

## Tasks & Acceptance

**Execution:**
- [x] `verify.py` -- result record and the per-service check contract; a check returns a state, never raises
- [x] `verify.py` -- calendar check: lazy import, load profile, resolve credential, one real `list_calendars` call, map each error type to a named cause
- [x] `verify.py` -- placeholders for mail and disk reporting "not yet available"
- [x] `main.py` -- `verify` subcommand, rendering, and exit-code rules
- [x] `tests/unit/test_cli_verify.py` -- one test per matrix row

**Acceptance Criteria:**
- Given a configured, working profile, when `yandex-mcp verify` runs, then calendar is reported reachable and the exit code is 0.
- Given no configuration, when it runs, then every service reads as unconfigured, the output names `yandex-mcp setup calendar`, and the exit code is 0.
- Given a rejected credential, when it runs, then the calendar line names the credential as the cause and the exit code is non-zero.
- Given an organisation that forbids app passwords, when it runs, then the line names organisation policy rather than a wrong password.
- Given an unreachable host, when it runs, then the calendar line names transport and every other service is still reported.
- Given a calendar package that cannot be imported, when it runs, then that is reported and the remaining services are still reported.
- Given any run in any state, when the output is inspected, then no secret appears in it.

## Spec Change Log

- **Finding (implementation):** `load_profile` and `get_secret` are the two ways
  "not set up yet" surfaces, and they raise at different steps -- `ProtocolError`
  for an absent config, `AuthError` for an absent credential. `get_secret` also
  raises `AuthError` when the fallback file is world-readable, which is a real
  problem rather than an absent credential.
  **Amendment:** verify reports both as *unconfigured* and carries the
  exception's own message through, so the chmod case still tells the operator
  exactly what to fix even though its state label reads "unconfigured".
  **Avoids:** a string-matched discriminator, or a new exception subclass in the
  core, either of which would cost more than the misfiling is worth today.
  **KEEP:** the message is never rewritten -- verify only ever appends the setup
  hint when it is missing.

## Design Notes

A configuration-only check would report success for precisely the case being diagnosed —
a stored password Yandex rejects. Hence a real call.

Every distinction verify reports already exists at the CalDAV translation boundary from
story 1.1, including its deliberate "cannot distinguish 401 from 403" state. Verify names
that state rather than collapsing it: guessing between a wrong password and an
organisation policy sends the operator down the wrong path.

The calendar client is imported inside the check, so a missing connector costs one
reported line rather than the whole command. Entry-point registration would decouple this
further; with one connector that is speculative, and it is revisited when Mail arrives.

`load_profile` raising for an absent config is the unconfigured signal, not an error to
propagate.

## Verification

**Commands:**
- `env -u PYTHONPATH uv run pytest tests/unit -q` -- expected: all pass, no network
- `env -u PYTHONPATH uv run yandex-mcp verify --help` -- expected: documents the subcommand
- `env -u PYTHONPATH YANDEX_MCP_CONFIG_DIR=$(mktemp -d) uv run yandex-mcp verify` -- expected: reports unconfigured, exits 0

## Suggested Review Order

**The distinction the command exists to make**

- Genuine absence versus a broken configuration; only the first is calm.
  [`errors.py:70`](../../../packages/yandex-core/src/yandex_core/errors.py#L70)

- The same split for credentials: nothing stored versus stored badly.
  [`errors.py:80`](../../../packages/yandex-core/src/yandex_core/errors.py#L80)

- Where those states become a reported line, a cause, and an exit code.
  [`verify.py:197`](../../../packages/yandex-mcp-cli/src/yandex_mcp_cli/verify.py#L197)

**Advice that fits the fault**

- The hint names the service and profile, and is withheld where it would mislead.
  [`verify.py:130`](../../../packages/yandex-mcp-cli/src/yandex_mcp_cli/verify.py#L130)

**Reporting every service, whatever happens**

- A raising check still becomes one line and cannot silence the checks after it.
  [`run_checks:308`](../../../packages/yandex-mcp-cli/src/yandex_mcp_cli/verify.py#L308)

- Rendering collapses whitespace and redacts, so a line stays one line and carries no secret.
  [`render_results:327`](../../../packages/yandex-mcp-cli/src/yandex_mcp_cli/verify.py#L327)

- Exit code is the contract; a closed pipe is not a service failure.
  [`main.py:138`](../../../packages/yandex-mcp-cli/src/yandex_mcp_cli/main.py#L138)
