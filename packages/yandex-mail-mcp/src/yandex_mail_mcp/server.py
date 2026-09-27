"""The Yandex mail MCP server: transport and wiring, nothing else.

Every decision of substance lives a layer down. This module builds the
application, resolves the profile once at start-up, registers the tools through the risk
registry, and hands the process to stdio.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from yandex_core.app import build_server, configure_logging, register_tool
from yandex_core.config import Profile, load_profile
from yandex_core.credentials import get_secret
from yandex_core.errors import YandexError

from .client.imap_client import IMAPMailClient
from .tools.folders import build_mail_folders_list
from .tools.message import build_mail_message_get
from .tools.messages import build_mail_messages_list

__all__ = ["SERVICE", "build_mail_server", "main"]

#: The service name under which the mail app password is stored. Its own slot,
#: not the calendar's: Yandex scopes app passwords by type, and one created for
#: Calendar is refused by IMAP -- measured.
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
            password_provider=lambda: _password(resolved),
        )

    server = build_server(name="yandex-mail-mcp", instructions=INSTRUCTIONS)
    register_tool(server, build_mail_folders_list(client_provider))
    register_tool(server, build_mail_messages_list(client_provider))
    register_tool(server, build_mail_message_get(client_provider))
    return server


async def _password(profile: Profile) -> str:
    """The mail app password, read per call through `credentials` (AD-6).

    Lazily, so a missing password surfaces as an actionable tool error naming
    `yandex-mcp setup mail`, rather than a start-up crash with no context.
    """
    return get_secret(SERVICE, profile.name)


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
