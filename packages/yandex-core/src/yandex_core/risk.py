"""One table of tool risk, and the annotations derived from it.

Annotations are never written inline at a tool definition, because a hint that
lies is worse than no hint at all.  Each tool declares a risk class here once;
:func:`annotations_for` turns it into MCP annotations, and a tool that never made
it into the table cannot be registered at all.

Note the spelling: ``ToolAnnotations`` fields are snake_case in Python and only
serialise to camelCase, so ``read_only_hint`` is the attribute and
``readOnlyHint`` is the wire form.
"""

from __future__ import annotations

from enum import Enum

from mcp.types import ToolAnnotations

from .errors import ProtocolError

__all__ = [
    "RiskClass",
    "RISK_REGISTRY",
    "annotations_for",
    "is_registered",
    "registered_tools",
]


class RiskClass(Enum):
    """What a tool is permitted to do to the account behind it."""

    READ = "read"
    """Observes only; repeating it changes nothing."""

    WRITE = "write"
    """Creates something new; does not remove or overwrite existing data."""

    DESTRUCTIVE = "destructive"
    """Overwrites or removes data that already exists."""


#: Every tool this project may register, and nothing else.
RISK_REGISTRY: dict[str, RiskClass] = {
    "calendar_list": RiskClass.READ,
    "calendar_events_list": RiskClass.READ,
    "calendar_event_get": RiskClass.READ,
    "calendar_freebusy_query": RiskClass.READ,
    "calendar_event_create": RiskClass.WRITE,
}

_ANNOTATIONS: dict[RiskClass, dict[str, bool]] = {
    RiskClass.READ: {
        "read_only_hint": True,
        "destructive_hint": False,
        "idempotent_hint": True,
    },
    RiskClass.WRITE: {
        "read_only_hint": False,
        "destructive_hint": False,
        "idempotent_hint": False,
    },
    RiskClass.DESTRUCTIVE: {
        "read_only_hint": False,
        "destructive_hint": True,
        "idempotent_hint": False,
    },
}


def is_registered(name: str) -> bool:
    return name in RISK_REGISTRY


def registered_tools() -> list[str]:
    return sorted(RISK_REGISTRY)


def annotations_for(name: str) -> ToolAnnotations:
    """Derive MCP annotations for a registered tool.

    Raises:
        ProtocolError: if the tool has no entry, naming the tool. Registration
            calls this, so an unannotated tool fails the server at start-up.
    """
    try:
        risk = RISK_REGISTRY[name]
    except KeyError:
        raise ProtocolError(
            f"Tool {name!r} has no entry in the risk registry; add one to "
            "yandex_core.risk.RISK_REGISTRY before registering it."
        ) from None

    return ToolAnnotations(
        **_ANNOTATIONS[risk],
        open_world_hint=True,
    )
