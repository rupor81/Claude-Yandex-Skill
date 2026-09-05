"""The server end of the slice: registration, annotations, and one stdio-shaped call."""

from __future__ import annotations

import anyio
import caldav
import pytest
from caldav.lib import error as caldav_error
from conftest import FakeCalendar, FakePrincipal
from yandex_calendar_mcp import server as server_module
from yandex_core.config import Profile
from yandex_core.errors import AuthError, CredentialNotFound
from mcp.server.mcpserver.exceptions import ToolError

PROFILE = Profile(name="default", login="me@yandex.ru")


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

        def calendar(self, url=None):
            return (calendars or [])[0]

    monkeypatch.setattr(caldav, "DAVClient", FakeDAVClient)
    if secret is not None:
        monkeypatch.setenv("YANDEX_MCP_CALENDAR_DEFAULT_PASSWORD", secret)
    return server_module.build_calendar_server(PROFILE)


def test_the_registered_tools_are_the_read_only_ones(monkeypatch):
    server = build(monkeypatch)
    tools = anyio.run(server.list_tools)
    assert sorted(tool.name for tool in tools) == [
        "calendar_events_list",
        "calendar_list",
    ]
    assert all(tool.annotations.read_only_hint is True for tool in tools)
    assert all(tool.annotations.destructive_hint is False for tool in tools)


def test_page_is_the_declared_output_schema(monkeypatch):
    server = build(monkeypatch)
    for tool in anyio.run(server.list_tools):
        required = set(tool.output_schema["required"])
        assert {"items", "complete", "next_cursor"} <= required


def test_the_event_range_is_required_by_the_declared_input_schema(monkeypatch):
    server = build(monkeypatch)
    (tool,) = [
        tool
        for tool in anyio.run(server.list_tools)
        if tool.name == "calendar_events_list"
    ]
    assert {"start", "end"} <= set(tool.input_schema["required"])


EVENT_DOCUMENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:standup-1\r\nSUMMARY:Standup\r\n"
    "DTSTART;TZID=Europe/Moscow:20260608T090000\r\n"
    "DTEND;TZID=Europe/Moscow:20260608T091500\r\n"
    "RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_calling_the_event_tool_returns_expanded_occurrences(monkeypatch):
    server = build(
        monkeypatch,
        calendars=[
            FakeCalendar(
                "Personal", "https://caldav.yandex.ru/c/personal/", [EVENT_DOCUMENT]
            )
        ],
    )
    result = anyio.run(
        lambda: server.call_tool(
            "calendar_events_list",
            {"start": "2026-06-01T00:00:00+03:00", "end": "2026-06-30T00:00:00+03:00"},
        )
    )
    payload = result.structuredContent if hasattr(result, "structuredContent") else None
    if payload is None:
        payload = result.structured_content
    assert payload["complete"] is True
    assert payload["unreadable"] == 0
    assert len(payload["items"]) == 3
    assert {item["uid"] for item in payload["items"]} == {"standup-1"}
    # Expanded, not a rule: every occurrence has its own start and its own id.
    assert len({item["recurrence_id"] for item in payload["items"]}) == 3
    assert "rrule" not in str(payload).lower()


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
    # The name of the concrete taxonomy class -- an AuthError subclass for the
    # "nothing stored" case -- is what tells the caller which family this is.
    assert "CredentialNotFound" in message
    assert issubclass(CredentialNotFound, AuthError)
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


def test_the_instructions_admit_a_page_can_be_irrecoverably_short():
    """The one condition no cursor fixes must be stated, not left to be found."""
    text = server_module.INSTRUCTIONS.lower()
    assert "could not be read" in text
    assert "no cursor can retrieve them" in text
