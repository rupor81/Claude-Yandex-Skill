"""``yandex-mcp`` -- the operator-facing setup and verification commands.

Setup exists because one step genuinely cannot be automated: Yandex CalDAV
rejects OAuth bearer tokens, so the credential has to be an app password created
by hand in Yandex ID.  This command explains that, then stores what the operator
pastes in.

Verify answers the question setup leaves open -- does the stored credential
actually work? -- by making one real call per service.  It is non-interactive,
writes nothing, and its exit code is stable: 0 unless a service actually failed.
The printed lines are for a human to read, not for a script to parse.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from yandex_core.config import (
    DEFAULT_CALDAV_URL,
    Profile,
    load_profile,
    write_profile,
)
from yandex_core.credentials import delete_secret, store_secret
from yandex_core.errors import YandexError

from .verify import render_results, run_checks

__all__ = ["build_parser", "main"]

MAIL_APP_PASSWORD_EXPLANATION = """\
Yandex Mail is connected the way mail programs connect to it: with an app
password. No application needs to be registered.

Two things to do in Yandex first. Yandex answers both mistakes with the same
error, so do both:

  1. Create an app password for *Mail* (Почта) at
     https://id.yandex.ru/security/app-passwords
     Its type matters: a password created for Calendar is refused by IMAP.
  2. Switch IMAP access on in Yandex Mail: Settings -> Mail programs, allow
     access from the imap.yandex.ru server.

On a Yandex 360 account an administrator can forbid mail programs entirely. If
so, the server will say it is organisation policy rather than a wrong password.

The password is stored in your system keychain (falling back to a 0600 file
under the config directory). It never appears in this repository, in tool
arguments, or in logs.
"""

SETUP_MAIL_DESCRIPTION = (
    "Store the Yandex Mail app password. Mail programs connect to Yandex this "
    "way; no registered application is needed."
)

APP_PASSWORD_EXPLANATION = """\
Yandex CalDAV does not accept OAuth access tokens: a bearer token that works for
every other Yandex API is rejected by the calendar endpoint. The only credential
it accepts is an app password, and app passwords can only be created by hand.

Create one before continuing:

  1. Open https://id.yandex.ru/security/app-passwords
  2. Create a password for "Calendar (CalDAV)".
  3. Copy the value Yandex shows once -- it is not shown again.

On a Yandex 360 account an administrator can disable app passwords entirely. If
that is the case the page will refuse to create one, and the calendar server will
report organisation policy rather than a wrong password.

The value is stored in your system keychain (falling back to a 0600 file under
the config directory). It never appears in this repository, in tool arguments,
or in logs.
"""

VERIFY_DESCRIPTION = (
    "Check each configured service by making one real, minimal call to it, and "
    "report a line each: reachable, unconfigured, or failed with the cause named."
)

VERIFY_EPILOG = """\
Verification makes a real network call per service, because a configuration-only
check would report success for exactly the credential Yandex rejects.

Every service is attempted and reported, even after another has failed. A service
that is unconfigured, or not yet built, is reported as such and does not make the
command fail; one that is broken, timed out, or misconfigured does.

The exit code is the part a script should depend on: 0 unless a service actually
failed. The lines themselves are written for a human to read -- they are aligned
columns whose values contain spaces, and their wording is free to change.

Nothing is written, repaired, or prompted for, and no secret appears in the
output in any state.
"""

SETUP_CALENDAR_DESCRIPTION = (
    "Store the Yandex calendar app password and profile. Yandex CalDAV rejects "
    "OAuth tokens, so an app password must be created by hand in Yandex ID first."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yandex-mcp",
        description="Setup and maintenance for the Yandex MCP connectors.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser(
        "setup",
        help="Configure a service.",
        description="Configure one Yandex service for use by its MCP server.",
    )
    services = setup.add_subparsers(dest="service", required=True)

    calendar = services.add_parser(
        "calendar",
        help="Store the calendar app password and profile.",
        description=SETUP_CALENDAR_DESCRIPTION,
        epilog=APP_PASSWORD_EXPLANATION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    calendar.add_argument(
        "--profile",
        default="default",
        help="Profile name to write (default: default).",
    )
    calendar.add_argument(
        "--login",
        help="Yandex login or full email address. Prompted for if omitted.",
    )
    calendar.add_argument(
        "--caldav-url",
        default=DEFAULT_CALDAV_URL,
        help=f"CalDAV endpoint (default: {DEFAULT_CALDAV_URL}).",
    )
    calendar.set_defaults(handler=setup_calendar)

    mail = services.add_parser(
        "mail",
        help="Store the mail app password.",
        description=SETUP_MAIL_DESCRIPTION,
        epilog=MAIL_APP_PASSWORD_EXPLANATION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mail.add_argument(
        "--profile",
        default="default",
        help="Profile to write (default: default).",
    )
    mail.add_argument(
        "--login",
        help=(
            "Yandex login or full email address. Taken from the profile if it "
            "already has one, prompted for otherwise."
        ),
    )
    mail.set_defaults(handler=setup_mail)

    verify = commands.add_parser(
        "verify",
        help="Check that each configured service actually works.",
        description=VERIFY_DESCRIPTION,
        epilog=VERIFY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verify.add_argument(
        "--profile",
        default=None,
        help=(
            "Profile to verify (default: the profile selected by "
            "YANDEX_MCP_PROFILE, or the configured default)."
        ),
    )
    verify.set_defaults(handler=verify_services)

    return parser


def verify_services(args: argparse.Namespace) -> int:
    """Report one line per service; exit non-zero only if one actually failed.

    Every cause is already handled inside the checks, so this never depends on
    ``main`` catching a ``YandexError`` -- a raising check would cost the report
    for the services after it.
    """
    results = run_checks(args.profile)
    for line in render_results(results):
        print(line)
    # Flushing here rather than at interpreter exit is what lets a closed pipe
    # (`yandex-mcp verify | head -1`) be handled as an answer instead of an
    # "Exception ignored" traceback after the exit code has already been set.
    sys.stdout.flush()
    return 1 if any(result.is_failure for result in results) else 0


def setup_calendar(args: argparse.Namespace) -> int:
    """Explain the app-password requirement, then store what was entered."""
    print(APP_PASSWORD_EXPLANATION)

    login = args.login or input("Yandex login or email: ").strip()
    if not login:
        print("A login is required.", file=sys.stderr)
        return 2

    password = getpass.getpass("App password (input hidden): ").strip()
    if not password:
        print("An app password is required. Nothing was stored.", file=sys.stderr)
        return 2

    profile = Profile(name=args.profile, login=login, caldav_url=args.caldav_url)
    location = store_secret("calendar", args.profile, password)
    try:
        path = write_profile(profile)
    except BaseException:
        # A stored secret with no profile naming it is an orphan nothing will
        # ever read or clean up, so it does not outlive the failure.
        try:
            delete_secret("calendar", args.profile)
        except Exception as cleanup_failure:  # noqa: BLE001 - reported, not hidden
            print(
                f"warning: the app password could not be removed after the "
                f"profile write failed ({cleanup_failure}); remove it by hand.",
                file=sys.stderr,
            )
        raise

    # Deliberately reports where, never what.
    print(f"\nApp password stored in: {location}")
    print(f"Profile {args.profile!r} written to: {path}")
    print(f"Select it at server start with YANDEX_MCP_PROFILE={args.profile}.")
    return 0


def setup_mail(args: argparse.Namespace) -> int:
    """Explain the two steps in Yandex, then store the mail app password.

    An existing profile keeps its login and everything else in it: setting up
    Mail must never cost the operator a working Calendar.
    """
    print(MAIL_APP_PASSWORD_EXPLANATION)

    try:
        existing: Profile | None = load_profile(args.profile)
    except YandexError:
        existing = None

    login = (args.login or (existing.login if existing else "") or "").strip()
    if not login:
        login = input("Yandex login or email: ").strip()
    if not login:
        print("A login is required. Nothing was stored.", file=sys.stderr)
        return 2

    password = getpass.getpass("Mail app password (input hidden): ").strip()
    if not password:
        print("An app password is required. Nothing was stored.", file=sys.stderr)
        return 2

    profile = (
        existing.model_copy(update={"login": login})
        if existing is not None
        else Profile(name=args.profile, login=login)
    )
    location = store_secret("mail", args.profile, password)
    try:
        path = write_profile(profile, make_default=existing is None)
    except BaseException:
        try:
            delete_secret("mail", args.profile)
        except Exception as cleanup_failure:  # noqa: BLE001 - reported, not hidden
            print(
                f"warning: the app password could not be removed after the "
                f"profile write failed ({cleanup_failure}); remove it by hand.",
                file=sys.stderr,
            )
        raise

    # Deliberately reports where, never what.
    print(f"\nMail app password stored in: {location}")
    print(f"Profile {args.profile!r} written to: {path}")
    print("Check it with: yandex-mcp verify")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except YandexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # The reader went away -- `| head -1` is the usual cause. That is not a
        # service failure, and the report is as complete as the reader wanted.
        _discard_remaining_output()
        return 0
    except (EOFError, KeyboardInterrupt):
        # Ctrl-C or a closed stdin at a prompt is an answer, not a crash.
        print("\nCancelled. Nothing was stored.", file=sys.stderr)
        return 130


def _discard_remaining_output() -> None:
    """Point stdout at the void so interpreter shutdown cannot flush into a
    closed pipe and print a traceback after we have chosen an exit code."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        # stdout is not a real file descriptor (a capture object, a pipe stand-in).
        # There is then nothing that could flush into the closed pipe either.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
