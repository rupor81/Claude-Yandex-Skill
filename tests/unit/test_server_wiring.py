"""The server end of the slice: registration, annotations, and one stdio-shaped call."""

from __future__ import annotations

import anyio
import caldav
import pytest
from caldav.lib import error as caldav_error
from yandex_calendar_mcp import server as server_module
from yandex_core.config import Profile
from mcp.server.mcpserver.exceptions import ToolError

PROFILE = Profile(name="default", login="me@yandex.ru")


class FakeCalendar:
    def __init__(self, name, url):
        self.name = name
        self.url = url


class FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


def build(monkeypatch, *, calendars=None, secret="hunter2-app-password"):
    class FakeDAVClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def principal(self):
            return FakePrincipal(calendars or [])

    monkeypatch.setattr(caldav, "DAVClient", FakeDAVClient)
    if secret is not None:
        monkeypatch.setenv("YANDEX_MCP_CALENDAR_DEFAULT_PASSWORD", secret)
    return server_module.build_calendar_server(PROFILE)


def test_only_calendar_list_is_registered_and_it_is_read_only(monkeypatch):
    server = build(monkeypatch)
    tools = anyio.run(server.list_tools)
    assert [tool.name for tool in tools] == ["calendar_list"]
    assert tools[0].annotations.read_only_hint is True


def test_page_is_the_declared_output_schema(monkeypatch):
    server = build(monkeypatch)
    (tool,) = anyio.run(server.list_tools)
    required = set(tool.output_schema["required"])
    assert {"items", "complete", "next_cursor"} <= required


def test_calling_the_tool_returns_a_page_of_calendars(monkeypatch):
    server = build(
        monkeypatch,
        calendars=[FakeCalendar("Personal", "https://caldav.yandex.ru/c/personal/")],
    )
    result = anyio.run(lambda: server.call_tool("calendar_list", {}))
    payload = result.structuredContent if hasattr(result, "structuredContent") else None
    if payload is None:
        payload = result.structured_content
    assert payload["complete"] is True
    assert payload["next_cursor"] is None
    assert payload["items"][0]["name"] == "Personal"


def test_a_missing_credential_surfaces_as_an_actionable_error(monkeypatch):
    """The SDK hides an unrecognised exception's text; the taxonomy must not be hidden."""
    server = build(monkeypatch, secret=None)
    with pytest.raises(ToolError) as caught:
        anyio.run(lambda: server.call_tool("calendar_list", {}))
    message = str(caught.value)
    assert "AuthError" in message
    assert "yandex-mcp setup calendar" in message


def test_a_wrong_app_password_never_reaches_the_caller_as_text(monkeypatch):
    secret = "hunter2-app-password"

    class FailingDAVClient:
        def __init__(self, **kwargs):
            raise caldav_error.AuthorizationError(url="https://caldav.yandex.ru", reason="Unauthorized")

    monkeypatch.setenv("YANDEX_MCP_CALENDAR_DEFAULT_PASSWORD", secret)
    monkeypatch.setattr(caldav, "DAVClient", FailingDAVClient)
    server = server_module.build_calendar_server(PROFILE)

    with pytest.raises(ToolError) as caught:
        anyio.run(lambda: server.call_tool("calendar_list", {}))
    message = str(caught.value)
    assert "AuthError" in message
    assert secret not in message


def test_the_instructions_do_not_claim_writes_this_server_cannot_do(monkeypatch):
    """A read-only server must not advertise itself as able to manage a calendar."""
    server = build(monkeypatch)
    tools = anyio.run(server.list_tools)
    assert all(tool.annotations.read_only_hint for tool in tools)
    assert "manage" not in server_module.INSTRUCTIONS.lower()
    assert "read-only" in server_module.INSTRUCTIONS.lower()
