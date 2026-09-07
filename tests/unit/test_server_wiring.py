"""The server end of the slice: registration, annotations, and one stdio-shaped call."""

from __future__ import annotations

import anyio
import caldav
import pytest
from caldav.lib import error as caldav_error
from conftest import FakeCalendar, FakePrincipal, install_fake_dav_client
from yandex_calendar_mcp import server as server_module
from yandex_core.config import Profile
from yandex_core.errors import AuthError, CredentialNotFound
from mcp.server.mcpserver.exceptions import ToolError

PROFILE = Profile(name="default", login="me@yandex.ru")


def build(monkeypatch, *, calendars=None, secret="hunter2-app-password", put=False):
    if put:
        # The shared fake is the only one that answers a PUT the way the server
        # does, guard header and all.
        install_fake_dav_client(monkeypatch, calendars=calendars, puts=[])
        if secret is not None:
            monkeypatch.setenv("YANDEX_MCP_CALENDAR_DEFAULT_PASSWORD", secret)
        return server_module.build_calendar_server(PROFILE)

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


WRITING_TOOLS = {"calendar_event_create", "calendar_event_update"}


def test_the_registered_tools_are_the_four_reads_and_two_writes(monkeypatch):
    server = build(monkeypatch)
    tools = anyio.run(server.list_tools)
    assert sorted(tool.name for tool in tools) == [
        "calendar_event_create",
        "calendar_event_get",
        "calendar_event_update",
        "calendar_events_list",
        "calendar_freebusy_query",
        "calendar_list",
    ]
    by_name = {tool.name: tool for tool in tools}
    reads = [tool for name, tool in by_name.items() if name not in WRITING_TOOLS]
    assert all(tool.annotations.read_only_hint is True for tool in reads)
    assert all(tool.annotations.destructive_hint is False for tool in reads)
    # A write must say so: a caller that gates writes on the annotation is told
    # nothing by a hint that lies in the safe-looking direction.
    create = by_name["calendar_event_create"]
    assert create.annotations.read_only_hint is False
    # And creating must not overclaim either: it removes nothing.
    assert create.annotations.destructive_hint is False
    # Changing an event overwrites what was there, and says so.
    update = by_name["calendar_event_update"]
    assert update.annotations.read_only_hint is False
    assert update.annotations.destructive_hint is True


def test_changing_an_event_declares_scope_and_etag_as_required(monkeypatch):
    """Both are refusals the schema itself should make, before a call is made."""
    server = build(monkeypatch)
    (tool,) = [
        tool
        for tool in anyio.run(server.list_tools)
        if tool.name == "calendar_event_update"
    ]
    required = set(tool.input_schema["required"])
    assert {"uid", "scope", "etag"} <= required
    scope = tool.input_schema["properties"]["scope"]["description"]
    assert "occurrence" in scope and "series" in scope
    assert "no default" in scope.lower()


#: The tools that return a collection, and so must say whether it is whole.
LISTING_TOOLS = {"calendar_list", "calendar_events_list", "calendar_freebusy_query"}


def test_page_is_the_declared_output_schema_of_every_listing_tool(monkeypatch):
    server = build(monkeypatch)
    listed = set()
    for tool in anyio.run(server.list_tools):
        if tool.name not in LISTING_TOOLS:
            continue
        listed.add(tool.name)
        required = set(tool.output_schema["required"])
        assert {"items", "complete", "next_cursor"} <= required
    assert listed == LISTING_TOOLS, "a listing tool vanished; this test went vacuous"


def test_reading_one_event_declares_the_etag_and_says_it_may_be_absent(monkeypatch):
    """Story 1.7 needs the ETag; a caller must be told it can legitimately be null."""
    server = build(monkeypatch)
    (tool,) = [
        tool
        for tool in anyio.run(server.list_tools)
        if tool.name == "calendar_event_get"
    ]
    assert "uid" in set(tool.input_schema["required"])
    schema = tool.output_schema
    assert "etag" in schema["properties"]
    assert "null" in str(schema["properties"]["etag"]).lower()

    # The type union alone says only "this may be null", which every optional
    # field says. What this test exists to pin is the *explanation*: that a
    # null is never an invented version, and that `etag_note` says which of the
    # two reasons applies -- the server sent none, or it could not be read.
    described = schema["properties"]["etag"]["description"].lower()
    assert "never invented" in described or "is never invented" in described
    assert "etag_note" in described
    note = schema["properties"]["etag_note"]["description"].lower()
    assert "supplied" in note, "the note must cover an ETag the server never sent"
    assert "could not be read" in note, "and one this server failed to read"


def test_calling_the_event_get_tool_returns_one_event_in_full(monkeypatch):
    server = build(
        monkeypatch,
        calendars=[
            FakeCalendar(
                "Personal", "https://caldav.yandex.ru/c/personal/", [EVENT_DOCUMENT]
            )
        ],
    )
    result = anyio.run(
        lambda: server.call_tool("calendar_event_get", {"uid": "standup-1"})
    )
    payload = result.structuredContent if hasattr(result, "structuredContent") else None
    if payload is None:
        payload = result.structured_content
    assert payload["uid"] == "standup-1"
    assert payload["summary"] == "Standup"
    assert payload["etag"]
    assert payload["scope"] == "series"


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


def test_the_instructions_claim_exactly_the_writes_this_server_can_do(monkeypatch):
    """Two writes exist now; the instructions must not imply the third."""
    server = build(monkeypatch)
    tools = anyio.run(server.list_tools)
    writes = sorted(
        tool.name for tool in tools if not tool.annotations.read_only_hint
    )
    assert writes == ["calendar_event_create", "calendar_event_update"]
    text = server_module.INSTRUCTIONS.lower()
    assert "create" in text
    assert "change" in text
    assert "cannot delete" in text
    # The one rule a change must not be described without.
    assert "scope" in text and "etag" in text


def test_creating_through_the_server_reports_what_was_stored(monkeypatch):
    """The stdio-shaped call, end to end, against a calendar that accepts a write."""
    calendar = FakeCalendar("Personal", "https://caldav.yandex.ru/c/personal/")
    server = build(monkeypatch, calendars=[calendar], put=True)
    result = anyio.run(
        lambda: server.call_tool(
            "calendar_event_create",
            {
                "calendar_url": "https://caldav.yandex.ru/c/personal/",
                "summary": "Design review",
                "start": "2026-06-08T09:00:00+03:00",
                "end": "2026-06-08T10:00:00+03:00",
            },
        )
    )
    payload = result.structuredContent if hasattr(result, "structuredContent") else None
    if payload is None:
        payload = result.structured_content
    assert payload["created"] is True
    assert payload["uid"]
    assert payload["etag"]
    assert payload["stored"]["summary"] == "Design review"
    assert payload["differs_from_request"] is False


def test_the_instructions_admit_a_page_can_be_irrecoverably_short():
    """The one condition no cursor fixes must be stated, not left to be found."""
    text = server_module.INSTRUCTIONS.lower()
    assert "could not be read" in text
    assert "no cursor can retrieve them" in text


def test_calling_the_freebusy_tool_returns_intervals_and_no_titles(monkeypatch):
    """The busy answer must never carry what the meeting was about."""
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
            "calendar_freebusy_query",
            {"start": "2026-06-01T00:00:00+03:00", "end": "2026-06-30T00:00:00+03:00"},
        )
    )
    payload = result.structuredContent if hasattr(result, "structuredContent") else None
    if payload is None:
        payload = result.structured_content
    assert payload["complete"] is True
    assert len(payload["items"]) == 3
    assert {item["kind"] for item in payload["items"]} == {"busy"}
    assert "standup" not in str(payload).lower()


def test_changing_through_the_server_reports_what_the_server_holds(monkeypatch):
    """The stdio-shaped call, end to end, including the scope and the ETag."""
    calendar = FakeCalendar(
        "Personal", "https://caldav.yandex.ru/c/personal/", [EVENT_DOCUMENT]
    )
    server = build(monkeypatch, calendars=[calendar], put=True)
    result = anyio.run(
        lambda: server.call_tool(
            "calendar_event_update",
            {
                "uid": "standup-1",
                "scope": "series",
                "etag": "etag-standup-1",
                "summary": "Daily standup",
            },
        )
    )
    payload = result.structuredContent if hasattr(result, "structuredContent") else None
    if payload is None:
        payload = result.structured_content
    assert payload["changed"] is True
    assert payload["stored"]["summary"] == "Daily standup"
    assert payload["etag"] and payload["etag"] != "etag-standup-1"


def test_changing_through_the_server_refuses_a_missing_scope(monkeypatch):
    """The refusal a caller meets most often must be a refusal, not a guess."""
    calendar = FakeCalendar(
        "Personal", "https://caldav.yandex.ru/c/personal/", [EVENT_DOCUMENT]
    )
    server = build(monkeypatch, calendars=[calendar], put=True)
    with pytest.raises(ToolError):
        anyio.run(
            lambda: server.call_tool(
                "calendar_event_update",
                {"uid": "standup-1", "etag": "etag-standup-1", "summary": "Daily"},
            )
        )


#: Words that would advertise a capability this server does not have. `delete`
#: is handled separately, because the instructions have to be able to say the
#: server *cannot* do it. `update` is checked separately too: this server now
#: has a tool by that name, and the word may appear only as part of it. `edit`
#: stays on the list: gaining `calendar_event_update` is a reason to allow the
#: tool's own name, not a reason to allow every other word for changing things
#: -- and the instructions have to name the two scopes and the precondition,
#: which "edit an event" quietly promises to do without either.
OVERCLAIMING_WORDS = (
    "manage",
    "edit",
    "modify",
    "reschedule",
    "remove",
    "move",
    "cancel",
    "attendee",
    "read-write",
)


def test_the_instructions_never_advertise_a_capability_this_server_lacks():
    """The negative guard, so a future edit cannot overclaim update or delete.

    An instruction block is read by a model deciding what to attempt. One that
    says this server can change or remove an event sends it to try, and the
    failure lands on a caller who was told the meeting would be moved.
    """
    text = server_module.INSTRUCTIONS.lower()
    for word in OVERCLAIMING_WORDS:
        assert word not in text, (
            f"the instructions say {word!r}; this server reads, creates and "
            "changes, and does none of those"
        )
    # `update` may appear only as the name of the tool that does it.
    assert text.count("update") == text.count("calendar_event_update")
    # `delete` may appear only in the sentence that denies it.
    assert text.count("delete") == text.count("cannot delete")
    assert text.count("cannot delete") == 1
    assert text.count("invite") == text.count("invites nobody")
    assert text.count("invites nobody") == 1


def test_the_instructions_name_no_tool_this_server_does_not_register():
    """A named tool is a promise a caller will try to keep."""
    import re

    from yandex_core.risk import registered_tools

    known = set(registered_tools()) | {"calendar_url"}
    named = set(re.findall(r"calendar_[a-z_]+", server_module.INSTRUCTIONS))
    assert named, "no tool is named at all; this guard would pass on anything"
    assert named <= known, f"the instructions name {sorted(named - known)}"
