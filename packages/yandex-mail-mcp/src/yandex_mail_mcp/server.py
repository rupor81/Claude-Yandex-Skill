"""The Yandex mail MCP server: transport and wiring, nothing else.

Every decision of substance lives a layer down. This module builds the
application, resolves the profile once at start-up, turns the stored refresh
token into a live access token on demand, registers the tools through the risk
registry, and hands the process to stdio.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from yandex_core.app import build_server, configure_logging, register_tool
from yandex_core.config import Profile, load_profile
from yandex_core.credentials import get_secret
from yandex_core.errors import ProtocolError, YandexError
from yandex_core.oauth import refresh_access_token

from .client.imap_client import IMAPMailClient
from .tools.folders import build_mail_folders_list

__all__ = ["SERVICE", "build_mail_server", "main"]

#: The service name under which the *refresh* token is stored. The access token
#: is never stored: it expires within the hour, and a stale one on disk is a
#: credential that looks usable and is not.
SERVICE = "mail"

INSTRUCTIONS = (
    "Read a Yandex mailbox over IMAP. Its one tool lists the mailbox's folders "
    "with their message and unread counts, so a later call can name a folder "
    "exactly as the server spells it. Folder names are hierarchical and this "
    "server separates the levels with `|`, not `/` -- build a child's name from "
    "the `delimiter` the listing reports rather than guessing it. A folder that "
    "reports null counts is not an empty folder: `counts_note` says why the "
    "counts are absent, and a container in the hierarchy has none of its own. "
    "The listing is bounded: when `complete` is false, pass `next_cursor` back "
    "verbatim to get the rest."
)

logger = logging.getLogger(__name__)


def build_mail_server(profile: Profile | None = None) -> MCPServer:
    """Build the mail application with no transport chosen yet.

    Raises:
        ProtocolError: if a tool is missing from the risk registry, naming it.
    """
    resolved = profile or load_profile()

    async def client_provider() -> IMAPMailClient:
        return IMAPMailClient(
            host=resolved.imap_host,
            port=resolved.imap_port,
            login=resolved.login,
            access_token_provider=lambda: _access_token(resolved),
        )

    server = build_server(name="yandex-mail-mcp", instructions=INSTRUCTIONS)
    register_tool(server, build_mail_folders_list(client_provider))
    return server


async def _access_token(profile: Profile) -> str:
    """A live access token, renewed silently from the stored refresh token.

    Read lazily, per call, for two reasons. A missing authorisation surfaces as
    an actionable tool error rather than a start-up crash with no context; and
    an access token lasts about an hour, so one fetched at start-up would go
    stale under a long-running server and fail with a message about credentials
    that are in fact perfectly good.
    """
    if not profile.oauth_client_id:
        raise ProtocolError(
            f"Profile {profile.name!r} has no OAuth `client_id`, so this "
            "mailbox cannot be authorised. Register an application at "
            "https://oauth.yandex.ru with the rights `mail:imap_full` and "
            "`mail:smtp`, then run `yandex-mcp login mail`."
        )
    # Read inside `credentials`, which is the only module allowed to (AD-6).
    stored = get_secret(SERVICE, profile.name)
    tokens = refresh_access_token(
        client_id=profile.oauth_client_id,
        refresh_token=stored,
        profile=profile.name,
    )
    return tokens.access_token


def main() -> int:
    """Entry point: stdio transport, logs on stderr only.

    A missing or malformed configuration is an operator problem with a fix, so it
    is reported as one line on stderr rather than as a traceback.
    """
    configure_logging()
    try:
        server = build_mail_server()
        logger.info("yandex-mail-mcp starting on stdio")
        server.run(transport="stdio")
    except YandexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.info("yandex-mail-mcp stopped")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
