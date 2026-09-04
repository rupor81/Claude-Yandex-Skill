"""A tool with no declared risk must not be able to register at all."""

from __future__ import annotations

import pytest
from yandex_core.app import build_server, register_tool
from yandex_core.errors import ProtocolError
from yandex_core.risk import RiskClass, annotations_for, is_registered


def test_calendar_list_is_read_only():
    annotations = annotations_for("calendar_list")
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False


def test_hints_are_snake_case_in_python_and_camel_case_on_the_wire():
    annotations = annotations_for("calendar_list")
    assert not hasattr(annotations, "readOnlyHint")
    assert annotations.model_dump(by_alias=True)["readOnlyHint"] is True


def test_unregistered_tool_has_no_annotations():
    assert not is_registered("calendar_event_delete")
    with pytest.raises(ProtocolError) as caught:
        annotations_for("calendar_event_delete")
    assert "calendar_event_delete" in str(caught.value)


def test_registering_an_unregistered_tool_fails_at_startup():
    server = build_server(name="test-server")

    async def calendar_event_delete() -> None:
        """A tool nobody declared."""

    with pytest.raises(ProtocolError) as caught:
        register_tool(server, calendar_event_delete)
    assert "calendar_event_delete" in str(caught.value)


def test_write_and_destructive_classes_are_distinguishable(monkeypatch):
    from yandex_core import risk

    monkeypatch.setitem(risk.RISK_REGISTRY, "probe_write", RiskClass.WRITE)
    monkeypatch.setitem(risk.RISK_REGISTRY, "probe_destructive", RiskClass.DESTRUCTIVE)

    write = annotations_for("probe_write")
    destructive = annotations_for("probe_destructive")

    assert (write.read_only_hint, write.destructive_hint) == (False, False)
    assert (destructive.read_only_hint, destructive.destructive_hint) == (False, True)


def test_a_synchronous_tool_is_refused_at_registration(monkeypatch):
    """Every tool is `async def`; a blocking one would stall the event loop."""
    from yandex_core import risk

    monkeypatch.setitem(risk.RISK_REGISTRY, "probe_sync", RiskClass.READ)
    server = build_server(name="test-server")

    def probe_sync() -> None:
        """A tool that would block the loop."""

    with pytest.raises(ProtocolError) as caught:
        register_tool(server, probe_sync)
    assert "probe_sync" in str(caught.value)
    assert "async" in str(caught.value)


def test_a_duplicate_tool_name_is_refused(monkeypatch):
    """A second registration would silently take over an approved name."""
    from yandex_core import risk

    monkeypatch.setitem(risk.RISK_REGISTRY, "probe_dup", RiskClass.READ)
    server = build_server(name="test-server")

    async def probe_dup() -> None:
        """First."""

    async def other() -> None:
        """Second, under the same name."""

    register_tool(server, probe_dup)
    with pytest.raises(ProtocolError) as caught:
        register_tool(server, other, name="probe_dup")
    assert "probe_dup" in str(caught.value)
