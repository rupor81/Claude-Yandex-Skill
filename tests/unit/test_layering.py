"""The layering is an acceptance criterion, so it is asserted, not just intended.

`tools/` imports no protocol library, `client/` imports no `mcp` and runs from a
plain script, and `yandex_core` imports no server package.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

import yandex_calendar_mcp
import yandex_core
import yandex_mail_mcp

CORE_ROOT = Path(yandex_core.__file__).parent
CALENDAR_ROOT = Path(yandex_calendar_mcp.__file__).parent
MAIL_ROOT = Path(yandex_mail_mcp.__file__).parent

SERVER_PACKAGES = {"yandex_calendar_mcp", "yandex_mail_mcp", "yandex_mcp_cli"}

#: AD-1 runs in two directions and they are not the same set. Until epic 2 only
#: one of them was checked: `PROTOCOL_PACKAGES` held `{"mcp"}` and was asserted
#: against both layers, so `tools/` was tested for the wrong thing entirely and a
#: `import caldav` in a tool module would have passed.
MCP_PACKAGES = {"mcp"}
PROTOCOL_LIBRARIES = {
    "caldav",
    "imap_tools",
    "imaplib",
    "smtplib",
    "httpx",
    "icalendar",
}


def imported_roots(path: Path) -> set[str]:
    """Top-level package names imported by one module, absolute imports only."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def modules_under(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


CORE_MODULES = modules_under(CORE_ROOT)
TOOL_MODULES = modules_under(CALENDAR_ROOT / "tools") + modules_under(
    MAIL_ROOT / "tools"
)
CLIENT_MODULES = modules_under(CALENDAR_ROOT / "client") + modules_under(
    MAIL_ROOT / "client"
)


@pytest.mark.parametrize(
    ("label", "modules"),
    [
        ("yandex_core", CORE_MODULES),
        ("tools", TOOL_MODULES),
        ("client", CLIENT_MODULES),
    ],
)
def test_every_layer_actually_has_modules_to_check(label, modules):
    """A renamed or emptied directory would make the layering tests pass on nothing."""
    assert modules, f"no modules found under {label}; the layering tests are vacuous"


@pytest.mark.parametrize("module", CORE_MODULES, ids=lambda p: p.name)
def test_core_imports_no_server_package(module):
    assert not (imported_roots(module) & SERVER_PACKAGES)


@pytest.mark.parametrize(
    "module", TOOL_MODULES, ids=lambda p: f"{p.parent.parent.name}/{p.name}"
)
def test_tools_import_no_protocol_library(module):
    """The half of AD-1 that went unchecked until epic 2."""
    assert not (imported_roots(module) & PROTOCOL_LIBRARIES)


@pytest.mark.parametrize(
    "module", TOOL_MODULES, ids=lambda p: f"{p.parent.parent.name}/{p.name}"
)
def test_tools_do_not_reach_for_mcp_either(module):
    """Tools declare contracts; the entrypoint owns the server object."""
    assert not (imported_roots(module) & MCP_PACKAGES)


@pytest.mark.parametrize(
    "module", CLIENT_MODULES, ids=lambda p: f"{p.parent.parent.name}/{p.name}"
)
def test_client_imports_no_mcp(module):
    assert not (imported_roots(module) & MCP_PACKAGES)


def test_client_runs_from_a_plain_script_with_mcp_unavailable():
    """Import the client in a subprocess where `mcp` cannot be imported at all."""
    program = """
import sys

class Blocker:
    def find_module(self, name, path=None):
        return None
    def find_spec(self, name, path=None, target=None):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError("mcp is deliberately unavailable")
        return None

sys.meta_path.insert(0, Blocker())
import yandex_calendar_mcp.client.caldav_client as client
import yandex_calendar_mcp.tools.calendars as tools
import yandex_mail_mcp.client.imap_client as mail_client
import yandex_mail_mcp.tools.folders as mail_tools
assert "mcp" not in sys.modules
print(client.CalDAVCalendarClient.__name__, tools.CalendarSummary.__name__)
print(mail_client.IMAPMailClient.__name__, mail_tools.FolderSummary.__name__)
"""
    # The packages are put on the child's path explicitly rather than left to
    # the editable-install .pth files, which this machine does not always honour.
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(CORE_ROOT.parent),
            str(CALENDAR_ROOT.parent),
            str(MAIL_ROOT.parent),
            *sys.path[:1],
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "CalDAVCalendarClient CalendarSummary" in result.stdout
    assert "IMAPMailClient FolderSummary" in result.stdout
