"""Building an MCP application without choosing a transport for it.

``build_server`` returns a configured ``MCPServer``; the caller decides whether it
speaks stdio or anything else.  ``register_tool`` is the only sanctioned way to
attach a tool, because it is where the risk registry is consulted.

``MCPServer``'s positional order is ``name, title, description, instructions``, so
a stray second positional argument silently becomes the title.  Everything below
is passed by keyword.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .credentials import REDACTED, SECRET_FIELD_NAMES, redact_mapping
from .errors import ProtocolError, YandexError
from .risk import annotations_for

__all__ = ["build_server", "register_tool", "configure_logging", "LOG_LEVEL_ENV_VAR"]

LOG_LEVEL_ENV_VAR = "YANDEX_MCP_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"

#: Fallback bookkeeping for servers whose tool manager cannot be inspected.
_REGISTERED: dict[int, set[str]] = {}


class _RedactingFilter(logging.Filter):
    """Strip secret-named values out of log arguments, whatever their shape.

    ``record.args`` is a mapping for ``%(name)s``-style formatting and a tuple
    for ``%s``-style formatting; both occur.  The tuple form carries no field
    name of its own, so the message template is what identifies it: a template
    that mentions a secret field name has its positional arguments redacted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.args = _redact_args(record.args, template=str(record.msg))
        return True


def _redact_args(args: Any, *, template: str) -> Any:
    """Redact secret material in a log record's arguments, recursively."""
    if isinstance(args, Mapping):
        return redact_mapping(args)
    if isinstance(args, (tuple, list)):
        redacted = [_redact_args(item, template=template) for item in args]
        if _template_names_a_secret(template):
            redacted = [
                REDACTED if not isinstance(item, (Mapping, tuple, list)) else item
                for item in redacted
            ]
        return tuple(redacted) if isinstance(args, tuple) else redacted
    return args


def _template_names_a_secret(template: str) -> bool:
    """Whether a message template mentions a field whose value must not be shown."""
    words = set(re.split(r"[^A-Za-z0-9_]+", template.lower()))
    return bool(words & SECRET_FIELD_NAMES)


def configure_logging(level: str | None = None) -> None:
    """Send logs to stderr only -- stdout carries the MCP protocol on stdio.

    The level comes from the argument, then ``YANDEX_MCP_LOG_LEVEL``, then INFO.
    An unrecognised name falls back to INFO with a warning rather than raising:
    a mistyped environment variable must not stop a server from starting.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_RedactingFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(_resolve_level(level))


def _resolve_level(level: str | None) -> int:
    requested = level or os.environ.get(LOG_LEVEL_ENV_VAR) or DEFAULT_LOG_LEVEL
    resolved = logging.getLevelNamesMapping().get(str(requested).strip().upper())
    if resolved is None:
        logging.getLogger(__name__).warning(
            "Unrecognised log level %r; using %s.", requested, DEFAULT_LOG_LEVEL
        )
        return logging.INFO
    return resolved


def build_server(
    *,
    name: str,
    version: str = "0.1.0",
    instructions: str | None = None,
    **kwargs: Any,
) -> MCPServer:
    """Construct an MCP application, transport unchosen."""
    return MCPServer(
        name=name,
        version=version,
        instructions=instructions,
        **kwargs,
    )


def register_tool(
    server: MCPServer,
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
) -> str:
    """Attach a tool, annotated from the risk registry.

    Raises:
        ProtocolError: if the tool is absent from the risk registry, is not a
            coroutine function, or duplicates a name already registered.
    """
    tool_name = name or fn.__name__
    if not inspect.iscoroutinefunction(fn):
        raise ProtocolError(
            f"Tool {tool_name!r} is not an async function; every tool in this "
            "project must be `async def` so that no blocking call reaches the "
            "event loop."
        )
    if tool_name in _registered_names(server):
        raise ProtocolError(
            f"Tool {tool_name!r} is already registered on this server; a second "
            "registration would silently replace the first."
        )
    annotations = annotations_for(tool_name)
    _REGISTERED.setdefault(id(server), set()).add(tool_name)
    server.add_tool(
        _surface_yandex_errors(fn),
        name=tool_name,
        description=description or (fn.__doc__ or "").strip() or None,
        annotations=annotations,
    )
    return tool_name


def _registered_names(server: MCPServer) -> set[str]:
    """Names already attached to this server, however they were attached.

    The SDK replaces a same-named tool silently, which would let a second
    registration take over a name whose risk class was approved for the first.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is not None:
        try:
            return {tool.name for tool in manager.list_tools()}
        except Exception:  # noqa: BLE001 - fall back on our own bookkeeping
            pass
    return set(_REGISTERED.get(id(server), ()))


def _surface_yandex_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Let a `YandexError` message reach the caller instead of being swallowed.

    The MCP SDK treats an unrecognised exception as a server-side crash and
    deliberately withholds its text from the model, which would turn every
    actionable failure into "Error executing tool". `ToolError` is the SDK's
    "raised deliberately" channel, so the taxonomy is re-raised through it here --
    once, in the core, so that `tools/` still imports no protocol library.

    Messages in the taxonomy never contain a secret; see `credentials`.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except YandexError as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper
